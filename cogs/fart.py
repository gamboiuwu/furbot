"""Occasionally says "fart" in the welcome channel. ~5 random times a week,
with the first one about 2 minutes after startup. Peak engineering. 💨
"""

from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands, tasks

log = logging.getLogger("furbot.fart")

FART_NEXT = "fart_next"  # epoch of the next scheduled fart
FIRST_DELAY = 120        # first fart ~2 minutes after startup


class Fart(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.fart_loop.start()

    async def cog_unload(self) -> None:
        self.fart_loop.cancel()

    def _s(self, key: str):
        return self.settings.get(key)

    def _next_interval(self) -> float:
        per_week = max(1, self._s("fart_per_week"))
        avg = 7 * 86400 / per_week
        return random.uniform(0.4 * avg, 1.6 * avg)  # spread it out randomly

    @tasks.loop(seconds=60)
    async def fart_loop(self) -> None:
        if not self._s("fart_enabled"):
            return
        now = time.time()
        nxt = self.store.get(FART_NEXT)
        if nxt is None:
            # First run ever: schedule the very first fart ~2 minutes out.
            await self.store.set(FART_NEXT, int(now + FIRST_DELAY))
            return
        if now < nxt:
            return
        await self._fart()
        await self.store.set(FART_NEXT, int(now + self._next_interval()))

    @fart_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _fart(self) -> None:
        cid = self._s("fart_channel_id") or self._s("welcome_channel_id")
        channel = self.bot.get_channel(cid) if cid else None
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send("fart", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to fart")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fart(bot))
