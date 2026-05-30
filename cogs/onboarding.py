"""Unverified-member onboarding: reminder → self-escalation → kick.

Nothing here acts on its own. A background sweep only posts a summary to the
log channel; staff click buttons to actually send reminders or kick. Members
who get a reminder can tap a button to escalate to a random moderator, who
gets Approve / Needs-more-info / Deny buttons right in their DMs.

All buttons are persistent across restarts:
  * the member and moderator DM buttons are `DynamicItem`s that encode the
    guild + user in their custom_id (DM interactions carry no guild context);
  * the log-channel batch buttons are a static persistent View that re-scans
    live state when clicked.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff
from verification_actions import STATS, VERIFICATIONS, WARN_DEADLINE, MemberActions, build_userinfo_embed

log = logging.getLogger("furbot.onboarding")

# Store keys.
REMINDED = "onboarding.reminded"      # {"guild:user": epoch_first_reminded}
ESCALATED = "onboarding.escalated"    # {"guild:user": {"at": epoch, "mod_id": int}}
SUMMARY_MSG = "onboarding.summary_msg"  # {"channel_id": int, "message_id": int}
# Members who posted in the verify channel and are awaiting a manual push.
VERIFY_WAITING = "verify_waiting"     # {user_id: {at, escalated_at, mod_id, followed_up}}
VERIFY_MESSAGES = "verify_messages"   # {user_id: [{"c": content, "t": epoch}, ...]} (cap 5)
BLOCKED_CALLOUT = "blocked_callout"   # {mod_id: last_called_epoch} (cooldown for the call-out)

STAFF_ONLY = "🔒 This action is for staff only."


# --------------------------------------------------------------------------
# Persistent DM buttons (DynamicItem)
# --------------------------------------------------------------------------

class WaitingButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ob:wait:v1:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="I'm waiting to get verified",
                emoji="✋",
                style=discord.ButtonStyle.primary,
                custom_id=f"ob:wait:v1:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Onboarding | None" = interaction.client.get_cog("Onboarding")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_waiting(interaction, self.guild_id, self.user_id, reason="waiting")


class PhoneReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ob:phone:v1:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="Can't verify my phone — request review",
                emoji="📱",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ob:phone:v1:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Onboarding | None" = interaction.client.get_cog("Onboarding")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_waiting(interaction, self.guild_id, self.user_id, reason="phone")


class ModActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ob:mod:v1:(?P<action>approve|warn|deny):(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    _SPEC = {
        "approve": ("✅", "Approve", discord.ButtonStyle.success),
        "warn": ("⚠️", "Needs more info", discord.ButtonStyle.secondary),
        "deny": ("👢", "Deny & kick", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, guild_id: int, user_id: int) -> None:
        self.action = action
        self.guild_id = guild_id
        self.user_id = user_id
        emoji, label, style = self._SPEC[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"ob:mod:v1:{action}:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Onboarding | None" = interaction.client.get_cog("Onboarding")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_mod_action(interaction, self.action, self.guild_id, self.user_id)


def build_mod_view(guild_id: int, user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("approve", "warn", "deny"):
        view.add_item(ModActionButton(action, guild_id, user_id))
    return view


def disabled_view(label: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, disabled=True))
    return view


# --------------------------------------------------------------------------
# Static persistent batch-confirm view (log channel)
# --------------------------------------------------------------------------

class BatchConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction) -> "Onboarding | None":
        cog: "Onboarding | None" = self.bot.get_cog("Onboarding")
        member = interaction.user
        if cog is None or not isinstance(member, discord.Member) or not cog._is_staff(member):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return None
        return cog

    @discord.ui.button(label="Send reminders", emoji="📨", style=discord.ButtonStyle.primary, custom_id="ob:batch:v1:remind")
    async def remind(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = await self._guard(interaction)
        if cog:
            await cog.run_batch(interaction, "remind")

    @discord.ui.button(label="Kick overdue", emoji="👢", style=discord.ButtonStyle.danger, custom_id="ob:batch:v1:kick")
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = await self._guard(interaction)
        if cog:
            await cog.run_batch(interaction, "kick")

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="ob:batch:v1:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = await self._guard(interaction)
        if cog:
            await interaction.response.defer()
            await cog._post_or_update_summary(interaction.guild or cog._guild())


# --------------------------------------------------------------------------
# Cog
# --------------------------------------------------------------------------

class Onboarding(commands.Cog, MemberActions):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.sweep.start()
        self.auto_loop.start()
        self.verify_pending_loop.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()
        self.auto_loop.cancel()
        self.verify_pending_loop.cancel()

    # ---- small helpers ---------------------------------------------------

    def _s(self, key: str):
        return self.settings.get(key)

    def _guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

    def _floofs_role(self, guild: discord.Guild) -> discord.Role | None:
        rid = self.config.floofs_role_id
        return guild.get_role(rid) if rid else None

    def _removal_dm(self, guild: discord.Guild) -> str:
        invite = self._s("invite_link")
        invite_part = f" here: {invite}" if invite else ""
        return (
            f"You've been removed from **{guild.name}** because your verification "
            f"wasn't completed. You're welcome to rejoin anytime and try again{invite_part}. "
            "If you keep running into problems, email us at **staff@nyfurs.org** and let us "
            "know what's going wrong."
        )

    def _verification_channel_mention(self) -> str:
        cid = self.config.verification_channel_id
        return f"<#{cid}>" if cid else "**#verification**"

    # ---- compute eligible members ---------------------------------------

    def _compute(self, guild: discord.Guild) -> tuple[list[discord.Member], list[discord.Member]]:
        """Returns (due_remind, due_kick).
        - due_remind: unverified, not yet notified, and past the reminder age.
        - due_kick:   unverified, already notified, and the grace period has passed.
        """
        role = self._floofs_role(guild)
        if role is None:
            return [], []
        reminder_h = self._s("onboarding_reminder_hours")
        grace_s = self._s("onboarding_grace_hours") * 3600
        reminded = self.store.get(REMINDED, {})
        # Members who posted in the verify channel are handled by staff, never
        # auto-reminded or auto-kicked — they made the effort.
        answered = self.store.get(VERIFY_WAITING, {})
        # Members a mod warned ("needs more info") get a fixed countdown to kick.
        warn_deadlines = self.store.get(WARN_DEADLINE, {})
        now = discord.utils.utcnow()
        now_ts = time.time()
        due_remind: list[discord.Member] = []
        due_kick: list[discord.Member] = []
        for m in guild.members:
            if m.bot or role in m.roles or m.joined_at is None:
                continue
            warn_at = warn_deadlines.get(str(m.id))
            if warn_at is not None:
                # Warned: kick once their countdown expires; until then, leave them be.
                if now_ts >= warn_at:
                    due_kick.append(m)
                continue
            if str(m.id) in answered:
                continue  # they answered → staff's discretion
            notified_at = reminded.get(f"{guild.id}:{m.id}")
            if notified_at is not None:
                if now_ts - notified_at >= grace_s:
                    due_kick.append(m)
            else:
                if (now - m.joined_at).total_seconds() / 3600 >= reminder_h:
                    due_remind.append(m)
        return due_remind, due_kick

    async def _prune(self, guild: discord.Guild) -> None:
        """Drop dedupe entries for members who left or already verified."""
        role = self._floofs_role(guild)

        def mut(d: dict) -> None:
            for key_name in (REMINDED, ESCALATED):
                mapping = d.get(key_name)
                if not mapping:
                    continue
                for k in list(mapping.keys()):
                    try:
                        _, uid = k.split(":")
                    except ValueError:
                        del mapping[k]
                        continue
                    m = guild.get_member(int(uid))
                    if m is None or (role and role in m.roles):
                        del mapping[k]
            # Warn deadlines and answered-tracking are keyed by bare user id.
            for key_name in (WARN_DEADLINE, VERIFY_WAITING):
                mapping = d.get(key_name)
                if not mapping:
                    continue
                for uid in list(mapping.keys()):
                    m = guild.get_member(int(uid))
                    if m is None or (role and role in m.roles):
                        del mapping[uid]

        await self.store.update(mut)

    # ---- summary ---------------------------------------------------------

    def _summary_embed(self, guild, due_remind, due_kick, note) -> discord.Embed:
        e = discord.Embed(title="🧹 Verification sweep", color=discord.Color.orange())
        mode = "🤖 AUTO" if self._s("onboarding_auto") else "🙋 manual (staff-confirm)"
        e.description = (
            f"Mode: **{mode}**\n"
            f"**{len(due_remind)}** member(s) due a reminder.\n"
            f"**{len(due_kick)}** member(s) notified over {self._s('onboarding_grace_hours')}h ago "
            "and can be removed."
        )

        def preview(members: list[discord.Member]) -> str:
            shown = "\n".join(f"• {m.mention} ({m})" for m in members[:10])
            if len(members) > 10:
                shown += f"\n…and {len(members) - 10} more"
            return shown or "—"

        if due_remind:
            e.add_field(name="Due a reminder", value=preview(due_remind), inline=False)
        if due_kick:
            e.add_field(name="Past the deadline", value=preview(due_kick), inline=False)
        if not self._s("onboarding_enabled"):
            e.add_field(name="⏸️ Disabled", value="Set `/config set onboarding_enabled true` to enable.", inline=False)
        if note:
            e.add_field(name="Last action", value=note, inline=False)
        e.set_footer(text=f"Up to {self._s('onboarding_batch_cap')} per click · staff only")
        return e

    async def _post_or_update_summary(self, guild: discord.Guild | None, note: str | None = None) -> None:
        if guild is None:
            return
        log_id = self.config.log_channel_id
        channel = self.bot.get_channel(log_id) if log_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        due_remind, due_kick = self._compute(guild)
        saved = self.store.get(SUMMARY_MSG, {})

        existing = None
        if saved.get("channel_id") == channel.id and saved.get("message_id"):
            try:
                existing = await channel.fetch_message(saved["message_id"])
            except discord.HTTPException:
                existing = None

        # Nothing to show and no message to update -> stay quiet.
        if not due_remind and not due_kick and existing is None and note is None:
            return

        embed = self._summary_embed(guild, due_remind, due_kick, note)
        view = BatchConfirmView(self.bot)
        if existing is not None:
            await existing.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            await self.store.set(SUMMARY_MSG, {"channel_id": channel.id, "message_id": msg.id})

    # ---- batch execution -------------------------------------------------

    async def run_batch(self, interaction: discord.Interaction, kind: str) -> None:
        await interaction.response.defer()
        guild = interaction.guild or self._guild()
        if guild is None:
            await interaction.followup.send("No server available.", ephemeral=True)
            return
        cap = self._s("onboarding_batch_cap")
        delay = self._s("onboarding_action_delay")
        actor = interaction.user
        due_remind, due_kick = self._compute(guild)

        if kind == "remind":
            targets = due_remind[:cap]
            sent = skipped = 0
            for m in targets:
                if await self._send_reminder(guild, m):
                    sent += 1
                else:
                    skipped += 1
                await asyncio.sleep(delay)
            note = f"📨 {actor.display_name} sent {sent} reminder(s)"
            if skipped:
                note += f", {skipped} skipped (DMs closed)"
            if len(due_remind) > len(targets):
                note += f". {len(due_remind) - len(targets)} still pending — click again"
            note += "."
        else:  # kick
            targets = due_kick[:cap]
            removed = failed = 0
            for m in targets:
                ok = await self._kick(m, by=actor, reason="Unverified past deadline", dm_text=self._removal_dm(guild))
                if ok:
                    removed += 1
                    await asyncio.sleep(delay)
                else:
                    # A failure is usually a missing Kick Members permission,
                    # which would affect everyone — stop early rather than spam.
                    failed += 1
                    break
            note = f"👢 {actor.display_name} removed {removed} member(s)"
            if failed:
                note += f", {failed} failed (check my Kick Members permission)"
            if len(due_kick) > len(targets):
                note += f". {len(due_kick) - len(targets)} still pending — click again"
            note += "."

        await self._log_action(note)
        await self._post_or_update_summary(guild, note=note)
        await interaction.followup.send(note, ephemeral=True)

    async def _send_reminder(self, guild: discord.Guild, member: discord.Member) -> bool:
        view = discord.ui.View(timeout=None)
        view.add_item(WaitingButton(guild.id, member.id))
        view.add_item(PhoneReviewButton(guild.id, member.id))
        text = (
            f"👋 Hi! Thanks for joining **{guild.name}**. It looks like you haven't been "
            f"verified yet. To get full access, please post in {self._verification_channel_mention()} "
            "and a moderator will approve you.\n"
            "📱 Some servers also need a **verified phone number** on your Discord account. To add one: "
            "**User Settings → My Account → Phone Number**, then enter the code Discord texts you.\n"
            f"⏰ **Heads up:** if you're not verified within about **{self._s('onboarding_grace_hours')} hours**, "
            "you'll be removed — but you're always welcome to rejoin and try again.\n"
            "Been waiting a while, or can't set up a phone? Tap a button below and I'll get a moderator."
        )
        notified = await self._try_dm(member, text, view=view)
        if not notified:
            # Closed DMs -> ping them in the verification channel instead.
            notified = await self._ping_unreachable(guild, member)
        if not notified:
            return False
        await self._mark_reminded(guild, member)
        return True

    async def _mark_reminded(self, guild: discord.Guild, member: discord.Member) -> None:
        key = f"{guild.id}:{member.id}"

        def mut(d: dict) -> None:
            d.setdefault(REMINDED, {})[key] = int(time.time())
            stats = d.setdefault(STATS, {})
            stats["reminded"] = stats.get("reminded", 0) + 1

        await self.store.update(mut)

    async def _ping_unreachable(self, guild: discord.Guild, member: discord.Member) -> bool:
        cid = self.config.verification_channel_id
        channel = self.bot.get_channel(cid) if cid else None
        if not isinstance(channel, discord.TextChannel):
            return False
        grace = self._s("onboarding_grace_hours")
        text = (
            f"{member.mention} — I couldn't DM you about verification (your DMs may be closed). "
            f"Please get verified here soon: you have about **{grace} hours** before you'll be removed "
            "from the server. Open your DMs to me, or follow the channel instructions."
        )
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            return True
        except discord.HTTPException:
            log.exception("Failed to ping unreachable member in verification channel")
            return False

    # ---- member "I'm waiting" button ------------------------------------

    async def handle_waiting(
        self, interaction: discord.Interaction, guild_id: int, user_id: int, *, reason: str = "waiting"
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await interaction.followup.send("That server is unavailable right now.", ephemeral=True)
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None
        if member is None:
            await interaction.followup.send("You don't seem to be in the server anymore.", ephemeral=True)
            return

        role = self._floofs_role(guild)
        if role and role in member.roles:
            await interaction.followup.send("You're already verified! 🎉", ephemeral=True)
            return

        key = f"{guild_id}:{user_id}"
        if key in self.store.get(ESCALATED, {}):
            await interaction.followup.send("A moderator has already been notified — hang tight!", ephemeral=True)
            return

        mod = await self._dm_random_mod(guild, member, reason)
        if mod is None:
            await self._escalate_to_log(guild, member, reason)

        await self.store.update(
            lambda d: d.setdefault(ESCALATED, {}).__setitem__(
                key, {"at": int(time.time()), "mod_id": mod.id if mod else 0, "reason": reason}
            )
        )
        if reason == "phone":
            ack = (
                "✅ Thanks! I've asked a moderator to **manually review** your account because of the "
                "phone-verification issue. Someone will follow up soon — hang tight!"
            )
        else:
            ack = (
                "✅ Thanks! I've let a moderator know you're waiting — someone will check on your "
                "verification soon. Hang tight!"
            )
        await interaction.followup.send(ack, ephemeral=True)

    def _escalation_embed(
        self, guild: discord.Guild, member: discord.Member, reason: str = "waiting",
        messages: list[dict] | None = None,
    ) -> discord.Embed:
        embed = build_userinfo_embed(
            member, floofs_role_id=self.config.floofs_role_id, title="🔔 Verification request",
            verified_by=self.store.get(VERIFICATIONS, {}).get(str(member.id)),
        )
        chan = self._verification_channel_mention()
        if reason == "phone":
            embed.description = (
                f"📱 **{member}** tapped **\"Can't verify my phone\"** in **{guild.name}** and is asking "
                f"for a **manual review**. Please check {chan} and decide below."
            )
        elif reason == "pending":
            embed.description = (
                f"⏳ **{member}** posted in {chan} and has been waiting **over "
                f"{self._s('verify_escalate_hours')} hours** to be verified in **{guild.name}**. "
                "Please review and push them through."
            )
        else:
            embed.description = (
                f"✋ **{member}** tapped **\"I'm waiting to get verified\"** in **{guild.name}** and asked "
                f"for a moderator. Their answers in {chan} (if any) are below."
            )
        if messages:
            for i, msg in enumerate(messages, 1):
                ts = f"<t:{int(msg.get('t', 0))}:R>" if msg.get("t") else ""
                content = (msg.get("c") or "(no text)").strip()
                embed.add_field(name=f"📝 Their message {i} · {ts}".strip(" ·"), value=content[:1024], inline=False)
        else:
            embed.add_field(
                name="📝 Their messages",
                value=f"They haven't posted in {chan} yet (they may have only tapped the button).",
                inline=False,
            )
        return embed

    async def _collect_messages(self, guild: discord.Guild, member: discord.Member) -> list[dict]:
        """The member's recent verification-channel messages as {"c","t"} dicts.
        Prefers messages captured when posted; falls back to a history scan."""
        stored = self.store.get(VERIFY_MESSAGES, {}).get(str(member.id))
        if stored:
            return stored[-5:]
        cid = self.config.verification_channel_id
        channel = self.bot.get_channel(cid) if cid else None
        if not isinstance(channel, discord.TextChannel):
            return []
        found: list[dict] = []
        try:
            async for msg in channel.history(limit=500):
                if msg.author.id == member.id and (msg.content or msg.attachments):
                    content = (msg.content or "").strip()
                    if msg.attachments:
                        content += ("\n" if content else "") + "📎 " + ", ".join(a.filename for a in msg.attachments)
                    found.append({"c": content[:1000], "t": int(msg.created_at.timestamp())})
                    if len(found) >= 5:
                        break
        except discord.HTTPException:
            return []
        found.reverse()
        return found

    async def _dm_random_mod(
        self, guild: discord.Guild, member: discord.Member, reason: str = "waiting"
    ) -> discord.Member | None:
        import random

        role = guild.get_role(self.config.staff_role_id) if self.config.staff_role_id else None
        if role is None:
            return None
        candidates = [m for m in role.members if not m.bot]
        random.shuffle(candidates)
        messages = await self._collect_messages(guild, member)
        embed = self._escalation_embed(guild, member, reason, messages)
        for mod in candidates[:5]:  # try a few in case some have DMs closed
            view = build_mod_view(guild.id, member.id)
            try:
                await mod.send(embed=embed, view=view)
                return mod
            except discord.Forbidden:
                await self._call_out_blocked(guild, mod)  # they blocked/closed DMs
                continue
            except discord.HTTPException:
                continue
        return None

    async def _call_out_blocked(self, guild: discord.Guild, mod: discord.Member) -> None:
        """If a staff member has the bot blocked/DMs closed, humorously call them
        out in the welcome channel (rate-limited to once per 24h per person)."""
        if not self._s("blocked_callout_enabled"):
            return
        channel = self.bot.get_channel(self._s("welcome_channel_id")) if self._s("welcome_channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            return
        last = self.store.get(BLOCKED_CALLOUT, {})
        if time.time() - last.get(str(mod.id), 0) < 86400:
            return
        text = (
            f"📢 {mod.mention} has their DMs closed (or *blocked me* 😤), so I can't send them "
            "verification pings. Everyone point and laugh 👉😹"
        )

        # Prefer an image stored on the WebDAV drive (attached as a file); fall
        # back to an external URL if no WebDAV file is configured/available.
        kwargs: dict = {"allowed_mentions": discord.AllowedMentions(users=True)}
        fname = self._s("blocked_image_file")
        webdav = getattr(self.store, "webdav", None)
        attached = False
        if fname and webdav is not None:
            try:
                data = await webdav.download(fname)
                if data:
                    kwargs["file"] = discord.File(io.BytesIO(data), filename=fname)
                    attached = True
            except Exception:
                log.exception("Failed to fetch blocked-callout image from WebDAV")
        if not attached:
            url = self._s("blocked_image_url")
            if url:
                text += f"\n{url}"

        try:
            await channel.send(text, **kwargs)
            await self.store.update(
                lambda d: d.setdefault(BLOCKED_CALLOUT, {}).__setitem__(str(mod.id), int(time.time()))
            )
        except discord.HTTPException:
            log.exception("Failed to post blocked-staff call-out")

    async def _escalate_to_log(
        self, guild: discord.Guild, member: discord.Member, reason: str = "waiting"
    ) -> None:
        log_id = self.config.log_channel_id
        channel = self.bot.get_channel(log_id) if log_id else None
        if not isinstance(channel, discord.TextChannel):
            return
        content = (
            f"<@&{self.config.staff_role_id}> verification help requested"
            if self.config.staff_role_id else "Verification help requested"
        )
        messages = await self._collect_messages(guild, member)
        await channel.send(
            content=content,
            embed=self._escalation_embed(guild, member, reason, messages),
            view=build_mod_view(guild.id, member.id),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    # ---- moderator Approve / Warn / Deny --------------------------------

    async def handle_mod_action(
        self, interaction: discord.Interaction, action: str, guild_id: int, user_id: int
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await interaction.response.send_message("That server is unavailable.", ephemeral=True)
            return

        # Staff guard: re-fetch the acting user as a Member (DMs give a User).
        actor = guild.get_member(interaction.user.id)
        if actor is None or not self._is_staff(actor):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None
        if member is None:
            await interaction.response.edit_message(view=disabled_view("User already left"))
            await interaction.followup.send("That user is no longer in the server.", ephemeral=True)
            return

        role = self._floofs_role(guild)
        if action == "approve":
            if role and role in member.roles:
                await interaction.response.edit_message(view=disabled_view("Already verified"))
                await interaction.followup.send("They're already verified.", ephemeral=True)
                return
            await self._grant_floofs(member, by=actor, reason="onboarding escalation")
            result, label = f"✅ You verified {member.display_name}.", f"✅ Approved by {actor.display_name}"
        elif action == "warn":
            await self._warn(member, by=actor, message=self._warn_more_info(guild))
            result, label = f"⚠️ Asked {member.display_name} for more info.", f"⚠️ More info requested by {actor.display_name}"
        else:  # deny
            ok = await self._kick(member, by=actor, reason=f"Verification denied by {actor}", dm_text=self._removal_dm(guild))
            if ok:
                result, label = f"👢 {member.display_name} was denied and removed.", f"👢 Denied by {actor.display_name}"
            else:
                result, label = "Couldn't remove them — check my Kick Members permission.", "⚠️ Deny failed"

        await interaction.response.edit_message(view=disabled_view(label))
        await interaction.followup.send(result, ephemeral=True)
        await self.store.update(lambda d: d.get(ESCALATED, {}).pop(f"{guild_id}:{user_id}", None))

    def _warn_more_info(self, guild: discord.Guild) -> str:
        return (
            f"⚠️ Hi! A moderator looked at your verification in **{guild.name}** and needs a bit "
            f"**more information** before approving you. Please go back to {self._verification_channel_mention()} "
            "and add more detail (follow the channel's instructions). Reply here if you're unsure what's needed."
        )

    # ---- background sweep ------------------------------------------------

    @tasks.loop(minutes=30)
    async def sweep(self) -> None:
        # Keep the loop interval in sync with the live setting.
        desired = self._s("onboarding_sweep_minutes")
        if desired and self.sweep.minutes != desired:
            self.sweep.change_interval(minutes=desired)
        if not self._s("onboarding_enabled"):
            return
        guild = self._guild()
        if guild is None:
            return
        await self._prune(guild)
        await self._post_or_update_summary(guild)

    @sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()
        guild = self._guild()
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                log.exception("Failed to chunk guild members for sweep")

    # ---- automated mode (no staff clicks) -------------------------------

    @tasks.loop(minutes=10)
    async def auto_loop(self) -> None:
        """When AUTO mode is on: each cycle, send a batch of reminders and kick
        anyone whose grace period has expired. Nothing here runs unless BOTH
        onboarding_enabled and onboarding_auto are true."""
        # Keep the cycle interval in sync with the setting.
        interval = max(1, self._s("onboarding_remind_interval_minutes"))
        if self.auto_loop.minutes != interval:
            self.auto_loop.change_interval(minutes=interval)

        if not (self._s("onboarding_enabled") and self._s("onboarding_auto")):
            return
        guild = self._guild()
        if guild is None:
            return
        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                return
        await self._prune(guild)
        due_remind, due_kick = self._compute(guild)
        batch = max(1, self._s("onboarding_remind_batch"))
        delay = self._s("onboarding_action_delay") or 1.5

        sent = 0
        for m in due_remind[:batch]:
            if await self._send_reminder(guild, m):
                sent += 1
            await asyncio.sleep(delay)

        kicked = 0
        for m in due_kick[:batch]:
            ok = await self._kick(
                m, by=guild.me, reason="Unverified — grace period expired",
                dm_text=self._removal_dm(guild),
            )
            if not ok:  # likely missing Kick Members — stop and surface it
                break
            kicked += 1
            await asyncio.sleep(delay)

        if sent or kicked:
            await self._log_action(
                f"🤖 Auto-onboarding: notified {sent}, removed {kicked} this cycle "
                f"({len(due_remind)} awaiting reminder, {len(due_kick)} past grace)."
            )

        # Nothing left to do? Turn auto mode OFF so the loop stops working.
        # "Nothing left" = no one awaiting a reminder, no one past grace, and
        # no one still inside their grace window (the reminded list is empty
        # after pruning verified/left members).
        await self._prune(guild)
        due_remind2, due_kick2 = self._compute(guild)
        if not due_remind2 and not due_kick2 and not self.store.get(REMINDED, {}):
            await self.settings.set("onboarding_auto", False)
            await self._log_action(
                "✅ Onboarding backlog cleared — **auto mode turned OFF** automatically."
            )

    @auto_loop.before_loop
    async def _before_auto(self) -> None:
        await self.bot.wait_until_ready()

    # ---- "answered but still waiting" escalation ------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Track unverified members who post in the verification channel, so we
        can ping staff if they're left waiting too long."""
        if message.author.bot or not self._s("verify_pending_enabled"):
            return
        if message.channel.id != self.config.verification_channel_id:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        role = self._floofs_role(member.guild)
        if role is not None and role in member.roles:
            return  # already verified

        content = (message.content or "").strip()
        if message.attachments:
            content += ("\n" if content else "") + "📎 " + ", ".join(a.filename for a in message.attachments)
        key = str(member.id)

        def mut(d: dict) -> None:
            waiting = d.setdefault(VERIFY_WAITING, {})
            if key not in waiting:  # keep the original "waiting since" time
                waiting[key] = {"at": int(time.time()), "escalated_at": None, "mod_id": None, "followed_up": False}
            if content:
                msgs = d.setdefault(VERIFY_MESSAGES, {}).setdefault(key, [])
                msgs.append({"c": content[:1000], "t": int(time.time())})
                del msgs[:-5]  # keep the latest 5

        await self.store.update(mut)

    @tasks.loop(minutes=30)
    async def verify_pending_loop(self) -> None:
        if not self._s("verify_pending_enabled"):
            return
        guild = self._guild()
        if guild is None or not self.config.verification_channel_id:
            return
        waiting = dict(self.store.get(VERIFY_WAITING, {}))
        if not waiting:
            return
        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                return
        role = self._floofs_role(guild)
        now = time.time()
        esc_s = self._s("verify_escalate_hours") * 3600
        fu_s = self._s("verify_followup_hours") * 3600
        changed = False
        for key in list(waiting.keys()):
            info = waiting[key]
            member = guild.get_member(int(key))
            if member is None or (role is not None and role in member.roles):
                del waiting[key]  # verified or left — done
                changed = True
                continue
            if info.get("escalated_at") is None:
                if now - info.get("at", now) >= esc_s:
                    mod = await self._dm_random_mod(guild, member, "pending")
                    if mod is None:
                        await self._escalate_to_log(guild, member, "pending")
                    info["escalated_at"] = now
                    info["mod_id"] = mod.id if mod else 0
                    changed = True
            elif not info.get("followed_up"):
                if now - info["escalated_at"] >= fu_s:
                    await self._followup_mod(guild, member, info.get("mod_id") or 0)
                    info["followed_up"] = True
                    changed = True
        if changed:
            await self.store.set(VERIFY_WAITING, waiting)

    @verify_pending_loop.before_loop
    async def _before_pending(self) -> None:
        await self.bot.wait_until_ready()

    async def _followup_mod(self, guild: discord.Guild, member: discord.Member, mod_id: int) -> None:
        """Nudge the moderator who was pinged but hasn't acted."""
        content = f"are you still there? >:c — **{member}** is **still** waiting to be verified."
        messages = await self._collect_messages(guild, member)
        embed = self._escalation_embed(guild, member, "pending", messages)
        mod = guild.get_member(mod_id) if mod_id else None
        if mod is not None:
            try:
                await mod.send(content=content, embed=embed, view=build_mod_view(guild.id, member.id))
                return
            except discord.Forbidden:
                await self._call_out_blocked(guild, mod)  # they blocked/closed DMs
            except discord.HTTPException:
                pass
        # Original mod unreachable — try a fresh one, else the log channel.
        if await self._dm_random_mod(guild, member, "pending") is None:
            await self._escalate_to_log(guild, member, "pending")

    # ---- manual trigger --------------------------------------------------

    @app_commands.command(
        name="onboarding_sweep",
        description="Scan unverified members now and post a staff-confirm summary.",
    )
    @is_staff()
    async def onboarding_sweep(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild or self._guild()
        if guild is None:
            await interaction.followup.send("No server available.", ephemeral=True)
            return
        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                pass
        await self._prune(guild)
        await self._post_or_update_summary(guild, note=f"Triggered by {interaction.user.display_name}")
        await interaction.followup.send("Posted the onboarding summary in the log channel.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Command error in Onboarding cog", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
