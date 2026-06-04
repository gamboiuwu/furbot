"""DM concierge.

When someone messages the bot in DMs:
  * every message is logged to the staff log channel;
  * a plain greeting ("hi", "hello", …) gets a friendly reply asking if they
    have any questions or comments;
  * anything else gets a thank-you letting them know staff will see it.

Nothing is posted publicly — DMs are only mirrored to the log channel.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

import messages
from verification_actions import MemberActions

log = logging.getLogger("furbot.dmrelay")

DM_RELAY = "dm_relay"   # {user_id: {"greet": epoch, "ack": epoch}}
GREET_COOLDOWN = 300    # seconds between greetings to the same person
ACK_COOLDOWN = 60       # seconds between thank-yous to the same person

GREETINGS = {
    "hi", "hello", "hey", "heya", "hiya", "yo", "sup", "howdy", "hewwo",
    "henlo", "hai", "hii", "helo", "hallo", "heyo", "ello", "hihi", "hewo",
}


class DMRelay(commands.Cog, MemberActions):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    @staticmethod
    def _is_greeting(content: str) -> bool:
        cleaned = content.lower().strip(" !.?~,")
        if cleaned in GREETINGS:
            return True
        words = cleaned.split()
        return bool(words) and words[0] in GREETINGS and len(content) <= 15

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if not self.settings.get("dm_relay_enabled"):
            return
        content = (message.content or "").strip()
        if not content and not message.attachments:
            return

        # Mirror every DM to the staff log channel (never posted publicly).
        await self._log_dm(message)

        if content and self._is_greeting(content):
            await self._reply(message, "greet", GREET_COOLDOWN, messages.DM_GREET)
        else:
            await self._reply(message, "ack", ACK_COOLDOWN, messages.DM_RELAY_ACK)

    async def _log_dm(self, message: discord.Message) -> None:
        body = (message.content or "").strip()
        if message.attachments:
            body += ("\n" if body else "") + "\n".join(a.url for a in message.attachments)
        await self._log_action(
            f"📨 **DM to bot** from {message.author.mention} "
            f"({message.author} · `{message.author.id}`):\n>>> {body[:1700]}"
        )

    async def _reply(self, message: discord.Message, kind: str, cooldown: int, pool: list[str]) -> None:
        key = str(message.author.id)
        last = self.store.get(DM_RELAY, {}).get(key, {}).get(kind, 0)
        if time.time() - last < cooldown:
            return
        try:
            await message.channel.send(messages.pick(pool))
        except discord.HTTPException:
            return
        await self.store.update(
            lambda d: d.setdefault(DM_RELAY, {}).setdefault(key, {}).__setitem__(kind, int(time.time()))
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DMRelay(bot))
