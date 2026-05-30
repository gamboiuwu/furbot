"""Server event logging — joins, leaves, kicks, bans, unbans.

Everything is posted to the staff log channel only (never a public channel).
Kick vs leave is inferred from the audit log, so the bot needs the View Audit
Log permission for kick/ban attribution (it degrades gracefully without it).
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from verification_actions import MemberActions

log = logging.getLogger("furbot.serverlog")


class ServerLog(commands.Cog, MemberActions):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    def _enabled(self) -> bool:
        return bool(self.settings.get("member_log_enabled"))

    async def _recent_audit(self, guild: discord.Guild, user_id: int, action: discord.AuditLogAction):
        """Return a recent audit-log entry targeting `user_id`, or None."""
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                target = entry.target
                if target is not None and target.id == user_id and \
                        (discord.utils.utcnow() - entry.created_at).total_seconds() < 15:
                    return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._enabled():
            return
        created = int(member.created_at.timestamp())
        await self._log_action(
            f"📥 **{member}** (`{member.id}`) joined. Account created <t:{created}:R>. "
            f"(now {member.guild.member_count} members)"
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self._enabled():
            return
        guild = member.guild
        # A ban also fires member_remove — let on_member_ban handle those.
        if await self._recent_audit(guild, member.id, discord.AuditLogAction.ban):
            return
        kick = await self._recent_audit(guild, member.id, discord.AuditLogAction.kick)
        if kick is not None:
            by = kick.user.display_name if kick.user else "unknown"
            reason = f" — {kick.reason}" if kick.reason else ""
            await self._log_action(f"👢 **{member}** (`{member.id}`) was kicked by **{by}**{reason}.")
        else:
            await self._log_action(f"📤 **{member}** (`{member.id}`) left the server.")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._enabled():
            return
        entry = await self._recent_audit(guild, user.id, discord.AuditLogAction.ban)
        by = f" by **{entry.user.display_name}**" if entry and entry.user else ""
        reason = f" — {entry.reason}" if entry and entry.reason else ""
        await self._log_action(f"🔨 **{user}** (`{user.id}`) was banned{by}{reason}.")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._enabled():
            return
        await self._log_action(f"♻️ **{user}** (`{user.id}`) was unbanned.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLog(bot))
