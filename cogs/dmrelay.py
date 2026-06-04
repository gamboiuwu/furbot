"""DM concierge.

When someone messages the bot in DMs:
  * a plain greeting ("hi", "hello", …) gets a friendly reply;
  * anything else gets a friendly reply **with a button** — staff are only
    contacted if the user taps "Send to staff". This avoids forwarding every
    stray DM to the team (too many false positives).

Nothing is posted publicly — confirmed messages are mirrored to the log channel.
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

import messages
from verification_actions import MemberActions

log = logging.getLogger("furbot.dmrelay")

DM_RELAY = "dm_relay"   # {user_id: {"greet": epoch, "prompt": epoch}}
GREET_COOLDOWN = 300    # seconds between greetings to the same person
PROMPT_COOLDOWN = 120   # seconds between "contact staff?" prompts to the same person

GREETINGS = {
    "hi", "hello", "hey", "heya", "hiya", "yo", "sup", "howdy", "hewwo",
    "henlo", "hai", "hii", "helo", "hallo", "heyo", "ello", "hihi", "hewo",
}


class ContactStaffView(discord.ui.View):
    """A one-shot button shown on a DM. Only forwards to staff when tapped."""

    def __init__(self, cog: "DMRelay", author: discord.User, body: str) -> None:
        super().__init__(timeout=3600)
        self.cog = cog
        self.author = author
        self.body = body
        self.sent = False

    @discord.ui.button(label="Send to staff", emoji="📨", style=discord.ButtonStyle.primary)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return
        if self.sent:
            await interaction.response.send_message("Already sent to staff. ✅", ephemeral=True)
            return
        self.sent = True
        button.disabled = True
        button.label = "Sent to staff"
        await self.cog._forward_to_staff(self.author, self.body)
        await interaction.response.edit_message(
            content="✅ Passed along to the staff team — they'll follow up if needed. Thanks!",
            view=self,
        )


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

        if content and self._is_greeting(content):
            await self._cooldown_send(message, "greet", GREET_COOLDOWN, messages.pick(messages.DM_GREET))
            return

        # Substantive message: offer a button instead of auto-contacting staff.
        body = content
        if message.attachments:
            body += ("\n" if body else "") + "\n".join(a.url for a in message.attachments)
        prompt = (
            "Thanks for the message! If you'd like the **staff team** to see this, tap the button "
            "below and I'll pass it along. Otherwise, no worries — I won't bug them. 🐾"
        )
        await self._cooldown_send(message, "prompt", PROMPT_COOLDOWN, prompt,
                                  view=ContactStaffView(self, message.author, body))

    async def _cooldown_send(self, message, kind, cooldown, text, view=None) -> None:
        key = str(message.author.id)
        last = self.store.get(DM_RELAY, {}).get(key, {}).get(kind, 0)
        if time.time() - last < cooldown:
            return
        try:
            await message.channel.send(text, view=view or discord.utils.MISSING)
        except discord.HTTPException:
            return
        await self.store.update(
            lambda d: d.setdefault(DM_RELAY, {}).setdefault(key, {}).__setitem__(kind, int(time.time()))
        )

    async def _forward_to_staff(self, author: discord.abc.User, body: str) -> None:
        await self._log_action(
            f"📨 **DM forwarded to staff** by {author.mention} "
            f"({author} · `{author.id}`):\n>>> {body[:1700]}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DMRelay(bot))
