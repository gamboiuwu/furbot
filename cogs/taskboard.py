"""Staff task-forum assistant.

Treats each thread in a forum channel as a task and shepherds it to completion
with simple, proven productivity practices:

  * Ownership   — staff claim/assign a task so it has one accountable owner.
  * Visibility  — a pinned, auto-updating "Task Board" thread lists every open
                  task with its owner and how long it's been idle.
  * Follow-through — owners are nudged in-thread after 3 days of inactivity,
                  then weekly, so nothing silently stalls.
  * Completion  — a ✅ reaction on a task's first post closes it (Done tag +
                  archive), logs it, and credits the staffer.

The bot needs Manage Threads, plus Send Messages / Add Reactions / Read History
in the forum.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff
from verification_actions import MemberActions

log = logging.getLogger("furbot.taskboard")

TASK_OWNERS = "task_owners"   # {thread_id: owner_id}
TASK_STATE = "task_state"     # {thread_id: {"last_nudge": epoch}}
TASK_BOARD = "task_board"     # {"thread_id": int, "message_id": int}


class TaskBoard(commands.Cog, MemberActions):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.task_loop.start()

    async def cog_unload(self) -> None:
        self.task_loop.cancel()

    def _s(self, key: str):
        return self.settings.get(key)

    def _forum(self) -> discord.ForumChannel | None:
        fid = self._s("task_forum_id")
        ch = self.bot.get_channel(fid) if fid else None
        return ch if isinstance(ch, discord.ForumChannel) else None

    def _in_task_thread(self, interaction: discord.Interaction) -> bool:
        ch = interaction.channel
        return isinstance(ch, discord.Thread) and ch.parent_id == self._s("task_forum_id")

    @staticmethod
    def _last_activity(thread: discord.Thread) -> float:
        if thread.last_message_id:
            return discord.utils.snowflake_time(thread.last_message_id).timestamp()
        return thread.created_at.timestamp()

    def _open_tasks(self, forum: discord.ForumChannel) -> list[discord.Thread]:
        board_id = self.store.get(TASK_BOARD, {}).get("thread_id")
        return [t for t in forum.threads if not t.archived and t.id != board_id]

    # ---- ownership -------------------------------------------------------

    async def _set_owner(self, thread: discord.Thread, member: discord.Member) -> None:
        await self.store.update(
            lambda d: (
                d.setdefault(TASK_OWNERS, {}).__setitem__(str(thread.id), member.id),
                d.setdefault(TASK_STATE, {}).__setitem__(str(thread.id), {"last_nudge": 0}),
            )
        )

    def _owner_id(self, thread: discord.Thread) -> int | None:
        """Stored owner, defaulting to the thread's creator."""
        return self.store.get(TASK_OWNERS, {}).get(str(thread.id)) or thread.owner_id

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """New task thread -> auto-claim it for whoever created it."""
        if not self._s("task_enabled") or thread.parent_id != self._s("task_forum_id"):
            return
        if thread.owner_id:
            await self.store.update(
                lambda d: (
                    d.setdefault(TASK_OWNERS, {}).__setitem__(str(thread.id), thread.owner_id),
                    d.setdefault(TASK_STATE, {}).__setitem__(str(thread.id), {"last_nudge": 0}),
                )
            )
        forum = self._forum()
        if forum is not None:
            await self._refresh_board(forum)

    # ---- completion ------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._s("task_enabled") or payload.guild_id is None:
            return
        # A reaction on a forum thread's starter post has message_id == thread id.
        if payload.message_id != payload.channel_id:
            return
        thread = self.bot.get_channel(payload.channel_id)
        if not isinstance(thread, discord.Thread) or thread.parent_id != self._s("task_forum_id"):
            return
        if str(payload.emoji) != self._s("task_done_emoji"):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        reactor = payload.member or guild.get_member(payload.user_id)
        if reactor is None or reactor.bot or not self._is_staff(reactor):
            return
        await self._complete_task(thread, reactor)

    async def _complete_task(self, thread: discord.Thread, by: discord.Member) -> None:
        forum = thread.parent
        try:
            await thread.send(f"✅ Marked done by {by.mention}. Nice work — archiving this task.")
            tags = list(thread.applied_tags)
            done_tag = None
            if isinstance(forum, discord.ForumChannel):
                done_tag = next((t for t in forum.available_tags if t.name.lower() == "done"), None)
            if done_tag and done_tag not in tags:
                tags.append(done_tag)
            await thread.edit(applied_tags=tags, archived=True, locked=True)
        except discord.Forbidden:
            await thread.send("I need the **Manage Threads** permission to close this task.")
            return
        except discord.HTTPException:
            log.exception("Failed to complete task thread %s", thread.id)
            return

        await self.store.update(
            lambda d: (
                d.get(TASK_OWNERS, {}).pop(str(thread.id), None),
                d.get(TASK_STATE, {}).pop(str(thread.id), None),
            )
        )
        await self.store.update(lambda d: self._bump_and_audit(d, "task_done", by, by))
        await self._log_action(f"✅ Task **{thread.name}** completed by **{by.display_name}**.")
        if isinstance(forum, discord.ForumChannel):
            await self._refresh_board(forum)

    # ---- nudges + board --------------------------------------------------

    @tasks.loop(hours=6)
    async def task_loop(self) -> None:
        if not self._s("task_enabled"):
            return
        forum = self._forum()
        if forum is None:
            return
        owners = dict(self.store.get(TASK_OWNERS, {}))
        state = dict(self.store.get(TASK_STATE, {}))
        now = time.time()
        first = self._s("task_nudge_first_days") * 86400
        repeat = self._s("task_nudge_repeat_days") * 86400
        changed = owners_changed = False
        for thread in self._open_tasks(forum):
            # Backfill the creator as owner for any task without one.
            if str(thread.id) not in owners and thread.owner_id:
                owners[str(thread.id)] = thread.owner_id
                owners_changed = True
            inactive = now - self._last_activity(thread)
            if inactive < first:
                continue
            st = state.get(str(thread.id), {})
            last_nudge = st.get("last_nudge", 0)
            due = (now - last_nudge) >= repeat if last_nudge else True
            if not due:
                continue
            await self._nudge(thread, owners.get(str(thread.id)) or thread.owner_id, inactive)
            state[str(thread.id)] = {"last_nudge": int(now)}
            changed = True
        if owners_changed:
            await self.store.set(TASK_OWNERS, owners)
        if changed:
            await self.store.set(TASK_STATE, state)
        await self._refresh_board(forum)

    @task_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _nudge(self, thread: discord.Thread, owner_id: int | None, inactive: float) -> None:
        days = max(1, int(inactive / 86400))
        if owner_id:
            text = (
                f"🔔 <@{owner_id}> — this task has been quiet for ~{days} days. What's the next step? "
                "React ✅ on the first post when it's done, or drop an update here."
            )
        else:
            text = (
                f"🔔 This task is **unclaimed** and has been open ~{days} days. A staff member, please "
                "`/task claim` it so it has an owner."
            )
        try:
            await thread.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.exception("Failed to nudge task thread %s", thread.id)

    async def _refresh_board(self, forum: discord.ForumChannel) -> None:
        now = time.time()
        lines = []
        for thread in sorted(self._open_tasks(forum), key=lambda t: t.created_at):
            oid = self._owner_id(thread)
            owner = f"<@{oid}>" if oid else "_unclaimed_"
            age = max(0, int((now - thread.created_at.timestamp()) / 86400))
            idle = max(0, int((now - self._last_activity(thread)) / 86400))
            lines.append(f"• [{thread.name}]({thread.jump_url}) — {owner} — open {age}d · idle {idle}d")
        body = "## 📋 Open Tasks\n" + ("\n".join(lines) if lines else "_No open tasks. Nice work!_ 🎉")
        body = (body[:1900] + "\n…") if len(body) > 1900 else body
        body += f"\n\n_Updated <t:{int(now)}:R>. React ✅ on a task's first post to close it._"

        board = self.store.get(TASK_BOARD, {})
        thread_id, message_id = board.get("thread_id"), board.get("message_id")
        board_thread = forum.get_thread(thread_id) if thread_id else None
        if board_thread is None and thread_id:
            try:
                board_thread = await self.bot.fetch_channel(thread_id)
            except discord.HTTPException:
                board_thread = None

        try:
            if board_thread is None:
                created = await forum.create_thread(name="📋 Task Board", content=body)
                try:
                    await created.thread.edit(pinned=True)
                except discord.HTTPException:
                    pass
                await self.store.set(TASK_BOARD, {"thread_id": created.thread.id, "message_id": created.message.id})
            else:
                msg = await board_thread.fetch_message(message_id or board_thread.id)
                await msg.edit(content=body)
        except discord.Forbidden:
            log.warning("Missing permission to maintain the task board (need Manage Threads / Send Messages).")
        except discord.HTTPException:
            log.exception("Failed to refresh task board")

    # ---- commands --------------------------------------------------------

    group = app_commands.Group(name="task", description="Manage staff forum tasks.")

    @group.command(name="claim", description="Claim the current task (run inside the task thread).")
    @is_staff()
    async def claim(self, interaction: discord.Interaction) -> None:
        if not self._in_task_thread(interaction) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run this inside a task thread in the forum.", ephemeral=True)
            return
        await self._set_owner(interaction.channel, interaction.user)
        await interaction.response.send_message(f"✅ {interaction.user.mention} is now the owner of this task.")
        await self._refresh_board(interaction.channel.parent)

    @group.command(name="assign", description="Assign the current task to a staff member.")
    @app_commands.describe(member="Who should own this task")
    @is_staff()
    async def assign(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self._in_task_thread(interaction):
            await interaction.response.send_message("Run this inside a task thread in the forum.", ephemeral=True)
            return
        await self._set_owner(interaction.channel, member)
        await interaction.response.send_message(f"📌 This task is now assigned to {member.mention}.")
        await self._refresh_board(interaction.channel.parent)

    @group.command(name="owner", description="Show who owns the current task.")
    @is_staff()
    async def owner(self, interaction: discord.Interaction) -> None:
        if not self._in_task_thread(interaction):
            await interaction.response.send_message("Run this inside a task thread in the forum.", ephemeral=True)
            return
        oid = self._owner_id(interaction.channel)
        msg = f"Owner: <@{oid}>" if oid else "This task has no owner. Use `/task claim` or `/task assign`."
        await interaction.response.send_message(msg, ephemeral=True)

    @group.command(name="board", description="Refresh the pinned task board now.")
    @is_staff()
    async def board_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        forum = self._forum()
        if forum is None:
            await interaction.followup.send("The task forum isn't configured or I can't see it.", ephemeral=True)
            return
        await self._refresh_board(forum)
        await interaction.followup.send("Task board refreshed.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Task command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TaskBoard(bot))
