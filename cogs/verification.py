"""Verification flow, driven by staff reactions in the verification channel.

A staff member reacts to a new member's message with one of three emojis:

  ✅  approve  -> give the member the Floofs role + welcome DM
  ❌  reject   -> DM the member, then temp-ban them for a cooldown period
                  (default 24h) and auto-unban when it expires
  ⚠️  warn     -> DM the member that something was wrong with how they
                  verified and they should try again

There is also a manual `/verify` slash command as a fallback for approval.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff
from verification_actions import MemberActions

log = logging.getLogger("furbot.verification")

# Key in the shared store for the temp-ban cooldown list.
PENDING_UNBANS = "pending_unbans"  # {"guild_id:user_id": unban_at_epoch}


class Verification(commands.Cog, MemberActions):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.process_unbans.start()

    async def cog_unload(self) -> None:
        self.process_unbans.cancel()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _emoji_matches(emoji: discord.PartialEmoji | discord.Emoji | str, target: str) -> bool:
        """True if the reacted emoji matches `target`. Supports unicode emoji
        and custom server emoji (matched by name or full form)."""
        if str(emoji) == target:
            return True
        name = getattr(emoji, "name", None)
        return name is not None and name == target.strip(":")

    # ---- reject (temp-ban with cooldown) ---------------------------------

    async def _reject(self, member: discord.Member, *, by: discord.Member) -> None:
        guild = member.guild
        hours = self.config.reject_cooldown_hours

        # DM first — once banned we may no longer share a server to DM them.
        await self._try_dm(
            member,
            f"You were **not verified** in **{guild.name}**. There is a "
            f"**{hours}-hour cooldown** before you can rejoin and try again. "
            "If you believe this was a mistake, please reach out to the staff team.",
        )

        try:
            await guild.ban(
                member,
                reason=f"Verification rejected by {by} ({hours}h cooldown)",
                delete_message_seconds=0,
            )
        except discord.Forbidden:
            log.warning("Missing Ban Members permission — cannot reject %s", member)
            await self._log_action(
                f"⚠️ Tried to reject **{member.display_name}** but I'm missing the "
                "**Ban Members** permission. Please grant it to my role."
            )
            return
        except discord.HTTPException:
            log.exception("Failed to ban %s", member)
            return

        unban_at = time.time() + hours * 3600

        def _mutate(data: dict) -> None:
            data.setdefault(PENDING_UNBANS, {})[f"{guild.id}:{member.id}"] = unban_at
            self._bump_and_audit(data, "rejected", member, by)

        await self.store.update(_mutate)

        log.info("Rejected (temp-banned) %s for %sh (by %s)", member, hours, by)
        await self._log_action(
            f"⛔ **{member.display_name}** was not verified by **{by.display_name}** "
            f"and was banned for {hours}h (auto-unban scheduled)."
        )

    # ---- reaction dispatch ----------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self.config.verification_channel_id:
            return
        if payload.channel_id != self.config.verification_channel_id:
            return
        if payload.guild_id is None:
            return

        # Which action does this emoji map to?
        if self._emoji_matches(payload.emoji, self.config.approval_emoji):
            action = "approve"
        elif self._emoji_matches(payload.emoji, self.config.reject_emoji):
            action = "reject"
        elif self._emoji_matches(payload.emoji, self.config.warn_emoji):
            action = "warn"
        else:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        reactor = payload.member or guild.get_member(payload.user_id)
        if reactor is None or reactor.bot or not self._is_staff(reactor):
            return

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

        if action == "approve":
            await self._grant_floofs(target, by=reactor, reason="reaction approval")
        elif action == "reject":
            await self._reject(target, by=reactor)
        elif action == "warn":
            await self._warn(target, by=reactor)

    # ---- background: expire cooldowns -----------------------------------

    @tasks.loop(minutes=5)
    async def process_unbans(self) -> None:
        """Periodically unban anyone whose cooldown has expired. Re-reads the
        persisted list each tick, so it's robust across restarts."""
        pending = self.store.get(PENDING_UNBANS, {})
        if not pending:
            return
        now = time.time()
        changed = False
        for key, unban_at in list(pending.items()):
            if unban_at > now:
                continue
            try:
                guild_id_str, user_id_str = key.split(":")
                guild = self.bot.get_guild(int(guild_id_str))
                if guild is not None:
                    await guild.unban(
                        discord.Object(id=int(user_id_str)),
                        reason="Verification cooldown expired",
                    )
                    log.info("Auto-unbanned user %s in guild %s", user_id_str, guild_id_str)
            except discord.NotFound:
                pass  # already unbanned / not banned anymore
            except discord.HTTPException:
                log.exception("Failed to auto-unban %s", key)
                continue  # leave it pending; retry next tick
            del pending[key]
            changed = True
        if changed:
            await self.store.set(PENDING_UNBANS, pending)

    @process_unbans.before_loop
    async def _before_unbans(self) -> None:
        await self.bot.wait_until_ready()

    # ---- manual fallback command ----------------------------------------

    @app_commands.command(name="verify", description="Manually verify a member and give them the Floofs role.")
    @app_commands.describe(member="The member to verify")
    @is_staff()
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

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Command error in Verification cog", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verification(bot))
