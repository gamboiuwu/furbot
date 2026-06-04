"""Birthdays: members set their birthday with /birthday, and a daily check
(in Eastern time) gives them a Birthday role and posts a shoutout.

The Birthday role is added on the day and removed once it's over, so role
presence itself prevents duplicate shoutouts.
"""

from __future__ import annotations

import datetime
import logging
import time
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.birthday")

BIRTHDAYS = "birthdays"  # {user_id_str: {"d": int, "m": int, "y": int|None}}
PROMO_LAST = "birthday_promo_last"  # epoch of last invite post

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


class Birthday(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.daily_check.start()
        self.promo_loop.start()

    async def cog_unload(self) -> None:
        self.daily_check.cancel()
        self.promo_loop.cancel()

    # ---- helpers ---------------------------------------------------------

    def _s(self, key: str):
        return self.settings.get(key)

    def _tz(self) -> datetime.tzinfo:
        name = self._s("birthday_timezone") or "America/New_York"
        try:
            return ZoneInfo(name)
        except Exception:
            log.warning("Unknown timezone %r; falling back to fixed EST (UTC-5).", name)
            return timezone(timedelta(hours=-5))

    def _guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

    @staticmethod
    def _is_today(bd: dict, now: datetime.datetime) -> bool:
        m, d = bd.get("m"), bd.get("d")
        if m == now.month and d == now.day:
            return True
        # Feb 29 birthdays celebrate on Feb 28 in non-leap years.
        if m == 2 and d == 29 and now.month == 2 and now.day == 28 and not _is_leap(now.year):
            return True
        return False

    async def _announce(self, channel: discord.TextChannel, member: discord.Member) -> None:
        template = self._s("birthday_message") or "🎉🎂 Happy birthday {member}!"
        text = template.replace("{member}", member.mention).replace("{server}", member.guild.name)
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.exception("Failed to post birthday message")

    async def _celebrate(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Give the birthday role + announce (once). Returns True if newly added."""
        role = guild.get_role(self._s("birthday_role_id")) if self._s("birthday_role_id") else None
        if role is None or role in member.roles:
            return False
        try:
            await member.add_roles(role, reason="Happy birthday! 🎂")
        except discord.HTTPException:
            log.exception("Failed to add birthday role to %s", member)
            return False
        channel = self.bot.get_channel(self._s("birthday_channel_id")) if self._s("birthday_channel_id") else None
        if isinstance(channel, discord.TextChannel):
            await self._announce(channel, member)
        return True

    # ---- daily check -----------------------------------------------------

    @tasks.loop(minutes=30)
    async def daily_check(self) -> None:
        guild = self._guild()
        if guild is None:
            return
        role_id = self._s("birthday_role_id")
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            return  # not configured yet
        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.HTTPException, discord.ClientException):
                return

        now = datetime.datetime.now(self._tz())
        birthdays = self.store.get(BIRTHDAYS, {})
        celebrant_ids = {int(uid) for uid, bd in birthdays.items() if self._is_today(bd, now)}

        # Remove the role from anyone whose birthday isn't today.
        for m in list(role.members):
            if m.id not in celebrant_ids:
                try:
                    await m.remove_roles(role, reason="Birthday over")
                except discord.HTTPException:
                    log.exception("Failed to remove birthday role from %s", m)

        # Add the role + shout out today's birthdays (once each), but only from
        # the announce hour onward — so it happens at ~8 AM, not just past midnight.
        if now.hour >= (self._s("birthday_announce_hour") or 8):
            for uid in celebrant_ids:
                member = guild.get_member(uid)
                if member is not None:
                    await self._celebrate(guild, member)

    @daily_check.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- recurring feature invite ---------------------------------------

    @tasks.loop(minutes=30)
    async def promo_loop(self) -> None:
        """Post the birthday-feature invite every `birthday_promo_days` days.
        The last-post time is persisted so restarts/redeploys don't repost."""
        ch_id = self._s("birthday_promo_channel_id")
        if not ch_id:
            return
        channel = self.bot.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            return
        days = self._s("birthday_promo_days") or 3
        now = time.time()
        if now - self.store.get(PROMO_LAST, 0) < days * 86400:
            return
        message = self._s("birthday_promo_message")
        if not message:
            return
        try:
            await channel.send(message)
            await self.store.set(PROMO_LAST, int(now))
        except discord.HTTPException:
            log.exception("Failed to post birthday promo message")

    @promo_loop.before_loop
    async def _before_promo(self) -> None:
        await self.bot.wait_until_ready()

    # ---- commands --------------------------------------------------------

    group = app_commands.Group(name="birthday", description="Set your birthday for a shoutout and the Birthday role.")

    @group.command(name="set", description="Save your birthday so the bot can celebrate you!")
    @app_commands.describe(day="Day (1-31)", month="Month (1-12)", year="Year (optional)")
    async def set_birthday(
        self,
        interaction: discord.Interaction,
        day: app_commands.Range[int, 1, 31],
        month: app_commands.Range[int, 1, 12],
        year: app_commands.Range[int, 1900, 2026] | None = None,
    ) -> None:
        # Validate it's a real calendar date (use a leap year so Feb 29 is allowed).
        try:
            datetime.date(year or 2000, month, day)
        except ValueError:
            await interaction.response.send_message(
                "🤔 That doesn't look like a real date — double-check the day and month.", ephemeral=True
            )
            return

        birthdays = dict(self.store.get(BIRTHDAYS, {}))
        birthdays[str(interaction.user.id)] = {"d": day, "m": month, "y": year}
        await self.store.set(BIRTHDAYS, birthdays)

        await interaction.response.send_message(
            f"🎂 Saved! I'll wish you a happy birthday on **{MONTHS[month - 1]} {day}**. "
            "You'll get the Birthday role and a shoutout on the day! 🎉",
            ephemeral=True,
        )

        # If it's already their birthday AND past the announce hour, celebrate now;
        # otherwise the daily check handles it at the announce hour.
        guild = interaction.guild
        member = interaction.user
        if isinstance(guild, discord.Guild) and isinstance(member, discord.Member):
            now = datetime.datetime.now(self._tz())
            if self._is_today(birthdays[str(member.id)], now) and now.hour >= (self._s("birthday_announce_hour") or 8):
                await self._celebrate(guild, member)

    @group.command(name="view", description="See a saved birthday.")
    @app_commands.describe(member="Whose birthday to view (defaults to you)")
    async def view_birthday(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        bd = self.store.get(BIRTHDAYS, {}).get(str(target.id))
        if not bd:
            who = "You haven't" if target == interaction.user else f"{target.display_name} hasn't"
            await interaction.response.send_message(f"{who} set a birthday yet.", ephemeral=True)
            return
        date_str = f"{MONTHS[bd['m'] - 1]} {bd['d']}" + (f", {bd['y']}" if bd.get("y") else "")
        await interaction.response.send_message(f"🎂 {target.display_name}'s birthday: **{date_str}**", ephemeral=True)

    @group.command(name="clear", description="Remove your saved birthday.")
    async def clear_birthday(self, interaction: discord.Interaction) -> None:
        birthdays = dict(self.store.get(BIRTHDAYS, {}))
        if birthdays.pop(str(interaction.user.id), None) is None:
            await interaction.response.send_message("You don't have a birthday saved.", ephemeral=True)
            return
        await self.store.set(BIRTHDAYS, birthdays)
        await interaction.response.send_message("🗑️ Your birthday has been removed.", ephemeral=True)

    @group.command(name="announce", description="(Staff) Post the birthday-feature invite now.")
    @is_staff()
    async def announce(self, interaction: discord.Interaction) -> None:
        ch_id = self._s("birthday_promo_channel_id")
        channel = self.bot.get_channel(ch_id) if ch_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Set the channel first: `/config set birthday_promo_channel_id <id>`.", ephemeral=True
            )
            return
        message = self._s("birthday_promo_message")
        if not message:
            await interaction.response.send_message("No promo message is configured.", ephemeral=True)
            return
        await channel.send(message)
        await self.store.set(PROMO_LAST, int(time.time()))
        await interaction.response.send_message(f"📣 Posted the birthday invite in {channel.mention}.", ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        log.exception("Birthday command error", exc_info=error)
        msg = "Something went wrong with that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Birthday(bot))
