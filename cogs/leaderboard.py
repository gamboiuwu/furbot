"""Staff verification leaderboard + month-end congratulations.

Per-staff monthly action counts are recorded by MemberActions._bump_and_audit
under the STAFF_MONTHLY store key. This cog reads them for /leaderboard and,
once a month has ended, congratulates that month's top performer.
"""

from __future__ import annotations

import datetime
import logging
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff
from verification_actions import STAFF_MONTHLY, month_key

log = logging.getLogger("furbot.leaderboard")

AWARD_STATE = "leaderboard_state"  # {"last_awarded": "YYYY-MM"}

# Friendly phrasing for the "your top stat" sentence and leaderboard breakdown.
ACTION_EMOJI = {"verified": "✅", "rejected": "⛔", "warned": "⚠️", "onboard_kicked": "👢"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _top_action_sentence(rec: dict) -> str:
    counts = {k: v for k, v in rec.items() if isinstance(v, int)}
    if not counts:
        return "helped out with verification"
    action, n = max(counts.items(), key=lambda kv: kv[1])
    phrases = {
        "verified": f"verified **{_plural(n, 'member')}**",
        "rejected": f"handled **{_plural(n, 'rejection')}**",
        "warned": f"sent **{_plural(n, 'verification warning')}**",
        "onboard_kicked": f"cleared **{_plural(n, 'unverified member')}**",
    }
    return phrases.get(action, f"logged **{_plural(n, 'action')}**")


def _total(rec: dict) -> int:
    return sum(v for v in rec.values() if isinstance(v, int))


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.monthly_award.start()

    async def cog_unload(self) -> None:
        self.monthly_award.cancel()

    def _tz(self) -> datetime.tzinfo:
        try:
            return ZoneInfo(self.settings.get("birthday_timezone") or "America/New_York")
        except Exception:
            return timezone(timedelta(hours=-5))

    def _award_channel(self) -> discord.TextChannel | None:
        cid = self.settings.get("leaderboard_channel_id") or self.config.log_channel_id
        ch = self.bot.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    def _ranking(self, key: str) -> list[tuple[str, dict]]:
        data = self.store.get(STAFF_MONTHLY, {}).get(key, {})
        return sorted(data.items(), key=lambda kv: _total(kv[1]), reverse=True)

    # ---- /leaderboard ----------------------------------------------------

    @app_commands.command(name="leaderboard", description="Staff verification leaderboard for a month.")
    @app_commands.describe(month="Month as YYYY-MM (defaults to the current month)")
    @is_staff()
    async def leaderboard(self, interaction: discord.Interaction, month: str | None = None) -> None:
        key = month or month_key(datetime.datetime.now(self._tz()))
        ranking = self._ranking(key)
        embed = discord.Embed(title=f"🏆 Verification leaderboard — {key}", color=discord.Color.gold())
        if not ranking:
            embed.description = "No staff actions recorded for this month yet."
        else:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, (sid, rec) in enumerate(ranking[:15]):
                rank = medals[i] if i < 3 else f"**{i + 1}.**"
                breakdown = " ".join(
                    f"{ACTION_EMOJI[a]}{rec[a]}" for a in ACTION_EMOJI if rec.get(a)
                )
                lines.append(f"{rank} {rec.get('name', 'Unknown')} — **{_total(rec)}** total  {breakdown}")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- month-end congratulations --------------------------------------

    @tasks.loop(hours=6)
    async def monthly_award(self) -> None:
        now = datetime.datetime.now(self._tz())
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        state = self.store.get(AWARD_STATE, {})
        last = state.get("last_awarded")

        # First run ever: seed to the previous month so we don't retroactively
        # announce an old, partial month — we start awarding from the next rollover.
        if last is None:
            await self.store.set(AWARD_STATE, {"last_awarded": prev})
            return
        if last == prev:
            return  # already awarded the most-recently-finished month

        ranking = self._ranking(prev)
        # Mark as handled regardless, so we don't recompute every 6h.
        await self.store.set(AWARD_STATE, {"last_awarded": prev})
        if not ranking or _total(ranking[0][1]) == 0:
            return

        winner_id, rec = ranking[0]
        channel = self._award_channel()
        if channel is None:
            log.warning("No leaderboard/log channel configured for month-end award")
            return
        text = (
            f"🏆 Congratulations <@{winner_id}> for being the highest performer for verification! "
            f"You {_top_action_sentence(rec)} — thank you for keeping NYFurs running! 🐾"
        )
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.exception("Failed to post month-end award")

    @monthly_award.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Leaderboard command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
