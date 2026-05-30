"""DM concierge.

When someone messages the bot in DMs:
  * a plain greeting ("hi", "hello", …) -> the bot says hi and asks if they
    have any questions or comments;
  * anything more substantial -> the bot forwards it to a random moderator's
    DMs and thanks the person.

Staff DMs aren't relayed (avoids loops), and forwarding is rate-limited per
person so it can't be used to spam moderators.
"""

from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands

import messages
from verification_actions import MemberActions

log = logging.getLogger("furbot.dmrelay")

DM_RELAY = "dm_relay"   # {user_id: {"greet": epoch, "fwd": [epochs]}}
GREET_COOLDOWN = 300    # seconds between greetings to the same person
FWD_WINDOW = 3600       # rate-limit window
FWD_MAX = 5             # max forwards per window per person

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

    def _main_guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

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

        guild = self._main_guild()
        # Don't relay staff DMs (prevents mod->mod loops).
        author_member = guild.get_member(message.author.id) if guild else None
        if author_member is not None and self._is_staff(author_member):
            return

        if content and self._is_greeting(content):
            await self._greet(message)
        else:
            await self._forward(message, guild)

    async def _greet(self, message: discord.Message) -> None:
        state = self.store.get(DM_RELAY, {})
        last = state.get(str(message.author.id), {}).get("greet", 0)
        if time.time() - last < GREET_COOLDOWN:
            return  # don't spam greetings
        try:
            await message.channel.send(messages.pick(messages.DM_GREET))
        except discord.HTTPException:
            return
        await self.store.update(
            lambda d: d.setdefault(DM_RELAY, {}).setdefault(str(message.author.id), {}).__setitem__("greet", int(time.time()))
        )

    async def _forward(self, message: discord.Message, guild: discord.Guild | None) -> None:
        if guild is None:
            return
        key = str(message.author.id)
        now = time.time()
        recent = [t for t in self.store.get(DM_RELAY, {}).get(key, {}).get("fwd", []) if now - t < FWD_WINDOW]
        if len(recent) >= FWD_MAX:
            try:
                await message.channel.send(
                    "I've already passed several of your messages along — a moderator will get back to you soon. "
                    "Thanks for your patience! ^_^"
                )
            except discord.HTTPException:
                pass
            return

        delivered = await self._dm_random_staff(guild, message)
        if not delivered:
            delivered = await self._relay_to_log(message)
        if not delivered:
            return

        recent.append(int(now))

        def mut(d: dict) -> None:
            entry = d.setdefault(DM_RELAY, {}).setdefault(key, {})
            entry["fwd"] = recent

        await self.store.update(mut)
        try:
            await message.channel.send(messages.pick(messages.DM_RELAY_ACK))
        except discord.HTTPException:
            pass

    def _relay_text(self, message: discord.Message) -> str:
        body = (message.content or "").strip()
        if message.attachments:
            body += ("\n" if body else "") + "\n".join(a.url for a in message.attachments)
        return (
            f"📨 **Message for staff** — {message.author.mention} ({message.author} · `{message.author.id}`) "
            f"sent me this in DMs:\n>>> {body[:1700]}\n\nFeel free to reach out to them directly."
        )

    async def _dm_random_staff(self, guild: discord.Guild, message: discord.Message) -> bool:
        role = guild.get_role(self.config.staff_role_id) if self.config.staff_role_id else None
        if role is None:
            return False
        candidates = [m for m in role.members if not m.bot]
        random.shuffle(candidates)
        text = self._relay_text(message)
        for mod in candidates[:5]:
            try:
                await mod.send(text, allowed_mentions=discord.AllowedMentions.none())
                return True
            except discord.HTTPException:
                continue
        return False

    async def _relay_to_log(self, message: discord.Message) -> bool:
        cid = self.config.log_channel_id
        channel = self.bot.get_channel(cid) if cid else None
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            await channel.send(self._relay_text(message), allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DMRelay(bot))
