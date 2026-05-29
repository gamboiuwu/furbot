"""Shared member-action primitives used by both the verification and
onboarding cogs.

`MemberActions` is a mixin: any cog that inherits it (and sets `self.bot`,
`self.config`, `self.store` in `__init__`) gets the same, single source of
truth for granting Floofs, warning, kicking, DMing, logging, and recording
stats/audit. This avoids duplicated logic drifting between cogs.
"""

from __future__ import annotations

import datetime
import logging
import time
from datetime import timezone
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger("furbot.actions")

# Shared store keys for stats and the audit log.
STATS = "stats"          # {"verified": n, "rejected": n, "warned": n, ...}
AUDIT = "audit"          # list of recent action records (capped)
AUDIT_CAP = 1000
STAFF_MONTHLY = "staff_monthly"  # {"YYYY-MM": {staff_id: {action: n, "name": str}}}


def month_key(now: datetime.datetime | None = None, tz_name: str = "America/New_York") -> str:
    if now is None:
        try:
            now = datetime.datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def build_userinfo_embed(
    member: discord.Member, *, floofs_role_id: int | None, title: str | None = None
) -> discord.Embed:
    """Build the vetting card used by /userinfo and the moderator escalation DM."""
    created_ts = int(member.created_at.timestamp())
    account_age_days = (discord.utils.utcnow() - member.created_at).days

    embed = discord.Embed(
        title=title or f"👤 {member}",
        color=member.color if member.color.value else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=False)
    embed.add_field(
        name="Account created",
        value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)\n**{account_age_days} days** old",
        inline=False,
    )
    if member.joined_at:
        joined_ts = int(member.joined_at.timestamp())
        embed.add_field(name="Joined server", value=f"<t:{joined_ts}:D> (<t:{joined_ts}:R>)", inline=False)

    # Young accounts are the classic raid/alt signal — flag them.
    if account_age_days < 7:
        embed.add_field(
            name="⚠️ Heads up",
            value="This account is **less than a week old** — double-check before verifying.",
            inline=False,
        )

    verified = bool(floofs_role_id) and any(r.id == floofs_role_id for r in member.roles)
    embed.add_field(name="Verified?", value="✅ Yes" if verified else "❌ Not yet", inline=False)

    roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
    value = ", ".join(roles) if roles else "None"
    embed.add_field(name=f"Roles ({len(roles)})", value=value[:1024], inline=False)
    return embed


class MemberActions:
    # These attributes are provided by the host cog's __init__.
    bot: discord.Client
    config: object
    store: object

    # ---- staff check -----------------------------------------------------

    def _is_staff(self, member: discord.Member) -> bool:
        """Staff = has the configured staff role, or the Manage Roles perm."""
        staff_role_id = getattr(self.config, "staff_role_id", None)
        if staff_role_id and any(r.id == staff_role_id for r in member.roles):
            return True
        return member.guild_permissions.manage_roles

    # ---- DM / logging ----------------------------------------------------

    @staticmethod
    async def _try_dm(member: discord.abc.User, content: str | None = None, **kwargs) -> bool:
        """DM a member; return True on success. Swallows closed-DM failures."""
        try:
            await member.send(content=content, **kwargs)
            return True
        except discord.HTTPException:
            return False

    async def _log_action(self, message: str) -> None:
        log_channel_id = getattr(self.config, "log_channel_id", None)
        if not log_channel_id:
            return
        channel = self.bot.get_channel(log_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Failed to write to log channel")

    # ---- stats / audit ---------------------------------------------------

    @staticmethod
    def _bump_and_audit(
        data: dict, action: str, member: discord.Member, by: discord.Member
    ) -> None:
        stats = data.setdefault(STATS, {})
        stats[action] = stats.get(action, 0) + 1
        audit = data.setdefault(AUDIT, [])
        audit.append({
            "action": action,
            "user_id": member.id, "user": str(member),
            "by_id": by.id, "by": str(by),
            "at": int(time.time()),
        })
        if len(audit) > AUDIT_CAP:
            del audit[: len(audit) - AUDIT_CAP]

        # Per-staff monthly leaderboard counts (skip automated/bot actors).
        if not getattr(by, "bot", False):
            bucket = data.setdefault(STAFF_MONTHLY, {}).setdefault(month_key(), {})
            rec = bucket.setdefault(str(by.id), {})
            rec[action] = rec.get(action, 0) + 1
            rec["name"] = getattr(by, "display_name", str(by))

    async def _record(self, action: str, member: discord.Member, by: discord.Member) -> None:
        await self.store.update(lambda d: self._bump_and_audit(d, action, member, by))

    # ---- approve ---------------------------------------------------------

    async def _grant_floofs(
        self, member: discord.Member, *, by: discord.Member, reason: str
    ) -> bool:
        """Add the Floofs role. Returns True if newly added."""
        floofs_role_id = getattr(self.config, "floofs_role_id", None)
        role = member.guild.get_role(floofs_role_id) if floofs_role_id else None
        if role is None:
            log.warning("Floofs role not found (FLOOFS_ROLE_ID=%s)", floofs_role_id)
            return False
        if role in member.roles:
            return False
        await member.add_roles(role, reason=f"Verified by {by} ({reason})")
        log.info("Granted Floofs to %s (by %s)", member, by)
        await self._log_action(
            f"🐾 **{member.display_name}** was verified by **{by.display_name}**."
        )
        await self._try_dm(
            member,
            f"Welcome to **{member.guild.name}**! You've been verified and given "
            f"the **{role.name}** role. 🐾",
        )
        await self._post_welcome(member)
        await self._record("verified", member, by)
        return True

    async def _post_welcome(self, member: discord.Member) -> None:
        """Post a public welcome message when a member is verified (if a welcome
        channel/message is configured via settings)."""
        settings = getattr(self, "settings", None)
        if settings is None:
            return
        channel_id = settings.get("welcome_channel_id")
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        template = settings.get("welcome_message") or "🎉 Welcome {member} to {server}!"
        text = template.replace("{member}", member.mention).replace("{server}", member.guild.name)
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.exception("Failed to post welcome message")

    # ---- warn ------------------------------------------------------------

    async def _warn(
        self, member: discord.Member, *, by: discord.Member, message: str | None = None
    ) -> None:
        content = message or (
            f"Hi! A staff member reviewed your verification in **{member.guild.name}** "
            "and it looks like something wasn't quite right with how you verified. "
            "Please re-read the verification instructions and try again. If you're "
            "unsure what needs fixing, reply to the staff team and we'll help you out. 🐾"
        )
        await self._try_dm(member, content)
        log.info("Warned %s (by %s)", member, by)
        await self._log_action(
            f"⚠️ **{member.display_name}** was warned by **{by.display_name}** "
            "to redo their verification."
        )
        await self._record("warned", member, by)

    # ---- kick ------------------------------------------------------------

    async def _kick(
        self, member: discord.Member, *, by: discord.Member, reason: str, dm_text: str
    ) -> bool:
        """DM the member (best-effort) then kick them. Returns True on success."""
        guild = member.guild
        await self._try_dm(member, dm_text)  # DM first — after kick we may lose the shared guild
        try:
            await guild.kick(member, reason=reason)
        except discord.Forbidden:
            log.warning("Missing Kick Members permission — cannot kick %s", member)
            await self._log_action(
                f"⚠️ Tried to remove **{member.display_name}** but I'm missing the "
                "**Kick Members** permission. Please grant it to my role."
            )
            return False
        except discord.HTTPException:
            log.exception("Failed to kick %s", member)
            return False
        log.info("Kicked %s (by %s): %s", member, by, reason)
        await self._record("onboard_kicked", member, by)
        await self._log_action(
            f"👢 **{member.display_name}** was removed by **{by.display_name}** (unverified)."
        )
        return True
