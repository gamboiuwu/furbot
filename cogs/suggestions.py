"""Suggestion-box automation.

Lifecycle of a suggestion (a thread/post in the suggestion forum):

  1. Created  — the bot confirms it's been received and notes the final poll
                lands in ~2 weeks.
  2. +2 weeks — the bot posts a final **Yes / No** poll in the thread, open for
                another 2 weeks.
  3. Poll ends — if the majority voted Yes, the bot pushes the suggestion to the
                staff to-do forum with as much of the original detail as it can
                carry, plus a deadline (~2 weeks out).
  4. Follow-up — if the staff to-do isn't marked done (its "Live" forum tag) by
                the deadline, the bot pings staff every 3 days until it is.

All durations are configurable via /config (suggestions_*). State is persisted
to the WebDAV store so it survives restarts.
"""

from __future__ import annotations

import datetime
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.suggestions")

# store keys
POLLS = "suggestion_polls"   # {str(thread_id): {announced, poll_id, poll_at, decided}}
TODOS = "suggestion_todos"   # {str(todo_thread_id): {src, src_name, at, deadline, last_ping}}

DAY = 86400
NEW_POLLS_PER_RUN = 5        # throttle so a backlog isn't polled all at once
HISTORY_LOOKBACK = 60        # days: don't retroactively poll very old threads


class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    # ---- small helpers ---------------------------------------------------

    def _s(self, key: str):
        return self.settings.get(key)

    def _staff_role_id(self) -> int:
        return int(self._s("suggestions_staff_role_id") or 0) or int(getattr(self.config, "staff_role_id", 0) or 0)

    @staticmethod
    def _age_days(dt: datetime.datetime | None) -> float:
        if dt is None:
            return 0.0
        now = discord.utils.utcnow()
        return (now - dt).total_seconds() / DAY

    def _sugg_channel(self):
        cid = int(self._s("suggestions_channel_id") or 0)
        ch = self.bot.get_channel(cid) if cid else None
        return ch

    async def _suggestion_threads(self) -> list[discord.Thread]:
        """Active + recently-archived threads in the suggestion channel."""
        ch = self._sugg_channel()
        if ch is None:
            return []
        out: list[discord.Thread] = list(getattr(ch, "threads", []) or [])
        seen = {t.id for t in out}
        try:
            if hasattr(ch, "archived_threads"):
                async for t in ch.archived_threads(limit=100):
                    if t.id not in seen:
                        out.append(t)
                        seen.add(t.id)
        except discord.HTTPException:
            log.debug("Could not list archived suggestion threads", exc_info=True)
        return out

    # ---- creation announcement ------------------------------------------

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        want = int(self._s("suggestions_channel_id") or 0)
        if not self._s("suggestions_enabled"):
            log.info("Suggestion thread %s created but suggestions_enabled is OFF.", thread.id)
            return
        if thread.parent_id != want:
            log.info("Thread %s created in channel %s, not the suggestion channel %s — ignoring.",
                     thread.id, thread.parent_id, want)
            return
        log.info("New suggestion thread %s (%r) — announcing.", thread.id, thread.name)
        announced = await self._announce(thread)

        def _mut(data: dict) -> None:
            data.setdefault(POLLS, {})[str(thread.id)] = {
                "announced": announced, "poll_id": 0, "poll_at": 0, "decided": False,
            }
        await self.store.update(_mut)

    async def _announce(self, thread: discord.Thread) -> bool:
        """Post the welcome/process message in a suggestion thread. Returns success."""
        active = int(self._s("suggestions_active_days") or 14)
        poll_days = int(self._s("suggestions_poll_days") or 14)
        deadline_days = int(self._s("suggestions_deadline_days") or 14)
        owner = thread.owner.mention if thread.owner else (f"<@{thread.owner_id}>" if thread.owner_id else "there")

        embed = discord.Embed(
            title="✅ Suggestion received — thank you!",
            description=(
                f"Hi {owner}, thanks for taking the time to share this! Here's exactly what "
                "happens next, so you know what to expect:"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"1️⃣ Open for discussion — {active} days",
            value=("This thread stays open so everyone can weigh in, ask questions, and help "
                   "refine the idea. Feel free to add any extra detail in the meantime."),
            inline=False,
        )
        embed.add_field(
            name=f"2️⃣ Final vote — {poll_days} days",
            value=(f"After **{active} days**, I'll post a **Yes / No** poll right here. It stays "
                   f"open for another **{poll_days} days** for the community to decide."),
            inline=False,
        )
        embed.add_field(
            name="3️⃣ If it passes",
            value=(f"If the majority votes **Yes**, I hand it to the staff to-do list with a "
                   f"**{deadline_days}-day** deadline — and I keep nudging staff until it's done."),
            inline=False,
        )
        embed.set_footer(text="You don't need to do anything else — I'll keep this thread updated. ^w^")

        # Surface a clear reason if we can't post (the usual culprit is perms).
        me = thread.guild.me if thread.guild else None
        if me is not None:
            perms = thread.permissions_for(me)
            if not (perms.send_messages_in_threads and perms.view_channel):
                log.warning(
                    "Missing permission to post in suggestion thread %s — need 'View Channel' "
                    "and 'Send Messages in Threads' in %s.", thread.id, thread.parent_id,
                )
                return False
        try:
            await thread.send(
                content=owner if thread.owner_id else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            return True
        except discord.HTTPException:
            log.exception("Could not post suggestion confirmation in %s", thread.id)
            return False

    # ---- main loop -------------------------------------------------------

    @tasks.loop(hours=1)
    async def loop(self) -> None:
        if not self._s("suggestions_enabled") or self._sugg_channel() is None:
            return
        try:
            await self._tick_polls()
            await self._tick_todos()
        except Exception:
            log.exception("Suggestions loop failed")

    @loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_polls(self) -> None:
        active = int(self._s("suggestions_active_days") or 14)
        poll_days = max(1, min(32, int(self._s("suggestions_poll_days") or 14)))
        polls = dict(self.store.get(POLLS, {}))
        threads = await self._suggestion_threads()
        created_this_run = 0
        changed = False

        for thread in threads:
            key = str(thread.id)
            rec = polls.get(key)
            age = self._age_days(thread.created_at)
            if rec is None:
                # First time we've seen it. Ancient threads are skipped (don't
                # resurrect a backlog); fresher ones still need announcing.
                rec = {"announced": False, "poll_id": 0, "poll_at": 0,
                       "decided": age > HISTORY_LOOKBACK}
                polls[key] = rec
                changed = True

            # Fallback announcement: if the create event was missed (or the bot
            # was offline at creation), announce threads still in the open phase.
            if not rec.get("announced") and not rec.get("decided"):
                if age < active:
                    if await self._announce(thread):
                        rec["announced"] = True
                else:
                    rec["announced"] = True  # too late to welcome; just mark it
                changed = True

            if rec.get("decided") or rec.get("poll_id"):
                continue
            if age < active:
                continue
            if created_this_run >= NEW_POLLS_PER_RUN:
                continue
            poll_id = await self._post_poll(thread, poll_days)
            if poll_id:
                rec["poll_id"] = poll_id
                rec["poll_at"] = int(time.time())
                polls[key] = rec
                changed = True
                created_this_run += 1

        # Tally any polls that have ended.
        for key, rec in list(polls.items()):
            if rec.get("decided") or not rec.get("poll_id"):
                continue
            ended = (time.time() - rec.get("poll_at", 0)) >= poll_days * DAY
            thread = self.bot.get_channel(int(key)) or next((t for t in threads if str(t.id) == key), None)
            if thread is None:
                continue
            try:
                msg = await thread.fetch_message(rec["poll_id"])
            except discord.HTTPException:
                rec["decided"] = True
                changed = True
                continue
            poll = getattr(msg, "poll", None)
            if poll is None:
                rec["decided"] = True
                changed = True
                continue
            if not ended and not poll.is_finalised():
                continue
            yes, no = self._tally(poll)
            rec["decided"] = True
            changed = True
            log.info("Suggestion %s poll ended: yes=%d no=%d", key, yes, no)
            if yes > no:
                await self._push_todo(thread, yes, no)

        if changed:
            await self.store.set(POLLS, polls)

    async def _post_poll(self, thread: discord.Thread, poll_days: int) -> int:
        try:
            poll = discord.Poll(
                question="Should we move forward with this suggestion?",
                duration=datetime.timedelta(days=poll_days),
            )
            poll.add_answer(text="Yes", emoji="✅")
            poll.add_answer(text="No", emoji="❌")
            msg = await thread.send(
                content=(f"🗳️ **Final vote!** This suggestion has been open for a while — please cast "
                         f"your vote. The poll closes in **{poll_days} days**. If the majority says "
                         "**Yes**, it heads to the staff to-do list. ^w^"),
                poll=poll,
            )
            return msg.id
        except discord.HTTPException:
            log.exception("Could not post poll in suggestion %s", thread.id)
            return 0

    @staticmethod
    def _tally(poll: "discord.Poll") -> tuple[int, int]:
        yes = no = 0
        for ans in poll.answers:
            t = (ans.text or "").strip().lower()
            if t == "yes":
                yes = ans.vote_count
            elif t == "no":
                no = ans.vote_count
        return yes, no

    # ---- push to staff to-do --------------------------------------------

    async def _push_todo(self, thread: discord.Thread, yes: int, no: int) -> None:
        todo_ch = self.bot.get_channel(int(self._s("suggestions_todo_channel_id") or 0))
        if todo_ch is None:
            log.error("To-do channel not found; cannot push suggestion %s", thread.id)
            return

        # Gather as much original detail as possible.
        author = "unknown"
        body = ""
        try:
            starter = thread.starter_message or await thread.fetch_message(thread.id)
            if starter:
                author = f"{starter.author.mention} (`{starter.author}`)"
                body = starter.content or ""
        except discord.HTTPException:
            log.debug("Could not fetch starter message for %s", thread.id, exc_info=True)

        deadline_days = int(self._s("suggestions_deadline_days") or 14)
        deadline = int(time.time()) + deadline_days * DAY
        live_tag = self._s("suggestions_live_tag") or "Live"

        embed = discord.Embed(
            title=f"📥 Approved suggestion: {thread.name}"[:256],
            description=(body[:3500] or "_(no description in the original post)_"),
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Original author", value=author, inline=False)
        embed.add_field(name="Vote", value=f"✅ {yes}  •  ❌ {no}", inline=True)
        embed.add_field(name="Source", value=f"[Jump to suggestion]({thread.jump_url})", inline=True)
        embed.add_field(
            name="Deadline",
            value=f"<t:{deadline}:F> (<t:{deadline}:R>)\nMark the **{live_tag}** tag when integrated.",
            inline=False,
        )
        embed.set_footer(text="Pushed from the suggestion box")

        todo_thread = None
        try:
            if isinstance(todo_ch, discord.ForumChannel):
                created = await todo_ch.create_thread(
                    name=f"{thread.name}"[:100], embed=embed,
                    reason="Approved suggestion pushed to to-do",
                )
                todo_thread = created.thread
            elif isinstance(todo_ch, (discord.TextChannel, discord.Thread)):
                sent = await todo_ch.send(embed=embed)
                # Make a thread to track it where possible.
                if isinstance(todo_ch, discord.TextChannel):
                    try:
                        todo_thread = await sent.create_thread(name=f"{thread.name}"[:100])
                    except discord.HTTPException:
                        todo_thread = None
        except discord.HTTPException:
            log.exception("Could not create to-do for suggestion %s", thread.id)
            return

        # Track the to-do for deadline follow-up (only if we have a thread to tag/ping).
        if todo_thread is not None:
            def _mut(data: dict) -> None:
                data.setdefault(TODOS, {})[str(todo_thread.id)] = {
                    "src": thread.id, "src_name": thread.name, "at": int(time.time()),
                    "deadline": deadline, "last_ping": 0,
                }
            await self.store.update(_mut)
        log.info("Pushed suggestion %s to to-do (%s)", thread.id, getattr(todo_thread, "id", "message"))

    # ---- deadline follow-up ---------------------------------------------

    @staticmethod
    def _has_live_tag(thread: discord.Thread, live_tag: str) -> bool | None:
        """True/False if the Live tag is applied; None if the forum has no such tag."""
        parent = thread.parent
        if not isinstance(parent, discord.ForumChannel):
            return None
        tag = next((t for t in parent.available_tags if t.name.lower() == live_tag.lower()), None)
        if tag is None:
            return None
        return tag.id in {t.id for t in thread.applied_tags}

    async def _tick_todos(self) -> None:
        todos = dict(self.store.get(TODOS, {}))
        if not todos:
            return
        live_tag = self._s("suggestions_live_tag") or "Live"
        ping_days = max(1, int(self._s("suggestions_ping_days") or 3))
        role_id = self._staff_role_id()
        now = time.time()
        changed = False

        for tid, rec in list(todos.items()):
            thread = self.bot.get_channel(int(tid))
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(int(tid))
                except discord.HTTPException:
                    continue
            if not isinstance(thread, discord.Thread):
                todos.pop(tid, None)
                changed = True
                continue

            live = self._has_live_tag(thread, live_tag)
            if live is True:
                todos.pop(tid, None)  # done — stop tracking
                changed = True
                continue
            if live is None:
                # No Live tag exists on the forum — can't track completion; skip pinging.
                continue
            if now < rec.get("deadline", 0):
                continue
            if now - rec.get("last_ping", 0) < ping_days * DAY:
                continue

            mention = f"<@&{role_id}> " if role_id else ""
            try:
                await thread.send(
                    f"{mention}⏰ This to-do is **past its deadline** and still isn't marked "
                    f"**{live_tag}**. Please integrate it (or update the tag). I'll keep checking "
                    f"in every {ping_days} days until it's done.",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                rec["last_ping"] = int(now)
                changed = True
            except discord.HTTPException:
                log.exception("Could not ping staff for overdue to-do %s", tid)

        if changed:
            await self.store.set(TODOS, todos)

    # ---- staff commands --------------------------------------------------

    group = app_commands.Group(name="suggestions", description="Suggestion-box automation (staff).")

    @group.command(name="status", description="(Staff) Show suggestion-box automation status.")
    @is_staff()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        polls = self.store.get(POLLS, {})
        todos = self.store.get(TODOS, {})
        active_polls = sum(1 for r in polls.values() if r.get("poll_id") and not r.get("decided"))
        ch = int(self._s("suggestions_channel_id") or 0)
        td = int(self._s("suggestions_todo_channel_id") or 0)
        role = self._staff_role_id()
        lines = [
            f"**Enabled:** {bool(self._s('suggestions_enabled'))}",
            f"**Suggestion channel:** {f'<#{ch}>' if ch else '_not set_'}",
            f"**To-do channel:** {f'<#{td}>' if td else '_not set_'}",
            f"**Overdue ping role:** {f'<@&{role}>' if role else '_none_'}",
            f"**Tracked threads:** {len(polls)} · **Live polls:** {active_polls}",
            f"**Open to-dos:** {len(todos)}",
            f"**Timings:** poll after {self._s('suggestions_active_days')}d, "
            f"open {self._s('suggestions_poll_days')}d, deadline {self._s('suggestions_deadline_days')}d, "
            f"ping every {self._s('suggestions_ping_days')}d · Live tag: `{self._s('suggestions_live_tag')}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @group.command(name="runnow", description="(Staff) Run the suggestion check immediately.")
    @is_staff()
    async def runnow_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._s("suggestions_enabled"):
            await interaction.followup.send("Suggestions automation is switched off (`suggestions_enabled`).", ephemeral=True)
            return
        await self._tick_polls()
        await self._tick_todos()
        await interaction.followup.send("✅ Ran the suggestion check.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Suggestions command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestions(bot))
