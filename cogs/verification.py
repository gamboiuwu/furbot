"""Verification flow.

When a staff member adds the approval emoji to a message in the
verification channel, the author of that message is given the "Floofs"
role automatically. There is also a manual `/verify` slash command as a
fallback.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("furbot.verification")


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config

    # ---- helpers ---------------------------------------------------------

    def _is_staff(self, member: discord.Member) -> bool:
        """A member is staff if they have the configured staff role, or
        (failing that) the Manage Roles permission."""
        if self.config.staff_role_id:
            if any(r.id == self.config.staff_role_id for r in member.roles):
                return True
        return member.guild_permissions.manage_roles

    def _emoji_matches(self, emoji: discord.PartialEmoji | discord.Emoji | str) -> bool:
        """True if the reacted emoji matches the configured approval emoji.

        Supports both unicode emoji (e.g. ✅) and custom server emoji
        (matched by name or by the full <:name:id> form)."""
        target = self.config.approval_emoji
        emoji_str = str(emoji)
        if emoji_str == target:
            return True
        # Allow matching a custom emoji by its bare name, e.g. APPROVAL_EMOJI=verified
        name = getattr(emoji, "name", None)
        return name is not None and name == target.strip(":")

    async def _grant_floofs(
        self, member: discord.Member, *, by: discord.Member, reason: str
    ) -> bool:
        """Add the Floofs role to `member`. Returns True if newly added."""
        role = member.guild.get_role(self.config.floofs_role_id) if self.config.floofs_role_id else None
        if role is None:
            log.warning("Floofs role not found (FLOOFS_ROLE_ID=%s)", self.config.floofs_role_id)
            return False
        if role in member.roles:
            return False
        await member.add_roles(role, reason=f"Verified by {by} ({reason})")
        log.info("Granted Floofs to %s (by %s)", member, by)
        await self._log_action(
            f"🐾 **{member.mention}** was given **{role.name}** by {by.mention} ({reason})."
        )
        # Friendly DM — ignore failures (user may have DMs closed).
        try:
            await member.send(
                f"Welcome to **{member.guild.name}**! You've been verified and given "
                f"the **{role.name}** role. 🐾"
            )
        except discord.HTTPException:
            pass
        return True

    async def _log_action(self, message: str) -> None:
        if not self.config.log_channel_id:
            return
        channel = self.bot.get_channel(self.config.log_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(message)
            except discord.HTTPException:
                log.exception("Failed to write to log channel")

    # ---- reaction-to-role ------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Only act in the configured verification channel.
        if not self.config.verification_channel_id:
            return
        if payload.channel_id != self.config.verification_channel_id:
            return
        if payload.guild_id is None:
            return
        if not self._emoji_matches(payload.emoji):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        # The staff member who reacted.
        reactor = payload.member or guild.get_member(payload.user_id)
        if reactor is None or reactor.bot:
            return
        if not self._is_staff(reactor):
            return

        # Find the message and its author.
        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return

        target = message.author
        if not isinstance(target, discord.Member) or target.bot:
            return

        await self._grant_floofs(target, by=reactor, reason="reaction approval")

    # ---- manual fallback command ----------------------------------------

    @app_commands.command(name="verify", description="Manually verify a member and give them the Floofs role.")
    @app_commands.describe(member="The member to verify")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def verify(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        added = await self._grant_floofs(member, by=interaction.user, reason="manual /verify")
        if added:
            await interaction.response.send_message(f"✅ Verified {member.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"{member.mention} already has the Floofs role (or it isn't configured).",
                ephemeral=True,
            )

    @verify.error
    async def verify_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Roles** permission to use this."
        else:
            log.exception("verify command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verification(bot))
