"""Light community-culture bit: if someone drops the f-slur, the bot pushes
back with "THIS BETTER BE SATIRICAL". If that same person replies "it is", it
follows up with "I'm watching you".
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

log = logging.getLogger("furbot.slurwatch")

TRIGGER = "faggot"          # matched as a substring, case-insensitive
PROMPT = "THIS BETTER BE SATIRICAL"
CONFIRM = "I'm watching you"
PENDING_TTL = 300          # how long (s) we wait for the "it is" follow-up
AFFIRMATIONS = {"it is", "it was", "yes it is", "yeah it is", "yes", "yea", "yep"}


class SlurWatch(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self._pending: dict[tuple[int, int], float] = {}  # (channel, user) -> expiry

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.settings.get("slur_watch_enabled"):
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return

        content = (message.content or "").lower()
        key = (message.channel.id, message.author.id)
        now = time.time()

        if TRIGGER in content:
            self._pending[key] = now + PENDING_TTL
            await self._say(message.channel, PROMPT)
            return

        expiry = self._pending.get(key)
        if expiry is not None:
            if now >= expiry:
                self._pending.pop(key, None)
                return
            self._pending.pop(key, None)  # this is their follow-up either way
            if content.strip().strip(" .!~,") in AFFIRMATIONS:
                await self._say(message.channel, CONFIRM)

    @staticmethod
    async def _say(channel, text: str) -> None:
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SlurWatch(bot))
