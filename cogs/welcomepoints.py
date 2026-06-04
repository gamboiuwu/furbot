"""Welcome Points — reward members for welcoming newcomers.

When a member gets verified (gains the Floofs role), the next person to say
"welcome" in the welcome channel earns a Welcome Point: their message gets a ⭐,
and (the first time only) they get a DM explaining the system. Only the *first*
welcome after each verification counts. Every N points (default 50) the member
gets a shoutout in the channel — with no pings.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("furbot.welcomepoints")

WELCOME_AWARD = "welcome_award"    # {"at": epoch, "member_id": newly_verified_id} | None
WELCOME_POINTS = "welcome_points"  # {user_id: count}


class WelcomePoints(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    def _s(self, key: str):
        return self.settings.get(key)

    def _watch_channel_id(self) -> int:
        return self._s("welcome_points_channel_id") or self._s("welcome_channel_id") or 0

    @staticmethod
    def _is_welcome(text: str) -> bool:
        low = (text or "").lower()
        return "welcom" in low or "wlcm" in low  # welcome / welcomes / welcoming / wlcm

    # ---- arm on verification --------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """When a member gains the Floofs role, arm the next 'welcome' to score."""
        if not self._s("welcome_points_enabled"):
            return
        rid = self.config.floofs_role_id
        if not rid:
            return
        if any(r.id == rid for r in after.roles) and not any(r.id == rid for r in before.roles):
            await self.store.set(WELCOME_AWARD, {"at": int(time.time()), "member_id": after.id})

    # ---- award on the first welcome -------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._s("welcome_points_enabled"):
            return
        if message.channel.id != self._watch_channel_id():
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not self.store.get(WELCOME_AWARD) or not self._is_welcome(message.content):
            return

        window = (self._s("welcome_window_minutes") or 60) * 60
        now = time.time()
        result: dict = {}

        def consume(d: dict) -> None:
            award = d.get(WELCOME_AWARD)
            if not award:
                return
            if now - award.get("at", 0) > window:
                d[WELCOME_AWARD] = None  # stale — drop it
                return
            if member.id == award.get("member_id"):
                return  # can't welcome yourself; leave the award for someone else
            d[WELCOME_AWARD] = None  # first welcome consumes it
            points = d.setdefault(WELCOME_POINTS, {})
            count = points.get(str(member.id), 0) + 1
            points[str(member.id)] = count
            result["count"] = count

        await self.store.update(consume)
        count = result.get("count")
        if not count:
            return

        try:
            await message.add_reaction("⭐")
        except discord.HTTPException:
            pass

        if count == 1:  # explain the system the first time only
            await self._send_first_dm(member)

        every = self._s("welcome_shoutout_every") or 50
        if every > 0 and count % every == 0:
            await self._shoutout(message.channel, member, count)

    async def _send_first_dm(self, member: discord.Member) -> None:
        text = (
            "🌟 **Congratulations — you earned your first Welcome Point!**\n\n"
            f"You were the first to welcome a new member in **{member.guild.name}**, and that earns "
            "you a **Welcome Point**. Here's how it works:\n"
            "• When a new member gets verified, the **first person** to say *welcome* to them earns a "
            "point — I'll react with a ⭐ to confirm.\n"
            "• Only the **first** welcome counts, so be quick and kind!\n"
            "• Every **50 points**, I'll give you a little shoutout in the channel.\n\n"
            "Thanks for helping make everyone feel at home! 🐾"
        )
        try:
            await member.send(text)
        except discord.HTTPException:
            pass  # closed DMs — the ⭐ still confirms it

    async def _shoutout(self, channel: discord.abc.Messageable, member: discord.Member, count: int) -> None:
        text = (
            f"🌟 Big shoutout to **{member.display_name}** for reaching **{count} Welcome Points**! "
            "Thanks for always making newcomers feel at home. 🐾"
        )
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to post welcome-points shoutout")

    # ---- check your points ----------------------------------------------

    @app_commands.command(name="welcomepoints", description="See how many Welcome Points you (or someone) have.")
    @app_commands.describe(member="Whose points to check (defaults to you).")
    async def welcomepoints(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        pts = self.store.get(WELCOME_POINTS, {}).get(str(target.id), 0)
        who = "You have" if target.id == interaction.user.id else f"**{target.display_name}** has"
        await interaction.response.send_message(
            f"🌟 {who} **{pts}** Welcome Point{'s' if pts != 1 else ''}.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomePoints(bot))
