"""When several people say "hi" in a row in any channel, the bot joins in and
says "hi" too. Simple, lowercase, no variation.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

log = logging.getLogger("furbot.hipileon")


class HiPileon(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self._streaks: dict[int, set[int]] = {}   # channel_id -> set of author ids in the current run
        self._cooldown: dict[int, float] = {}      # channel_id -> last-joined epoch

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.settings.get("hi_pileon_enabled"):
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return

        cleaned = (message.content or "").strip().lower().strip(" !.~,")
        cid = message.channel.id

        if cleaned != "hi":
            self._streaks.pop(cid, None)  # a non-"hi" message breaks the streak
            return

        run = self._streaks.setdefault(cid, set())
        run.add(message.author.id)
        threshold = self.settings.get("hi_pileon_count") or 5
        if len(run) >= threshold:
            self._streaks[cid] = set()  # reset so it doesn't immediately retrigger
            if time.time() - self._cooldown.get(cid, 0) < 15:
                return
            self._cooldown[cid] = time.time()
            try:
                await message.channel.send("hi", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HiPileon(bot))
