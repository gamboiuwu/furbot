"""FurBot — a Discord bot for the NYFurs server and staff team.

Entry point. Loads configuration, sets up the bot with the intents it
needs, loads every cog (feature module) in the `cogs/` folder, and starts
the connection to Discord.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("furbot")

# Intents tell Discord which events we want to receive. Members and message
# content are "privileged" — you must enable them in the Developer Portal
# under your application's Bot settings (see README).
INTENTS = discord.Intents.default()
INTENTS.members = True          # needed to add/remove roles and read member data
INTENTS.message_content = True  # needed for prefix commands and content checks
INTENTS.reactions = True        # needed for the reaction-to-role verification flow

# Cogs to load on startup. Add new feature modules here.
INITIAL_COGS = (
    "cogs.general",
    "cogs.verification",
)


class FurBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=INTENTS,
            help_command=commands.DefaultHelpCommand(),
        )
        self.config = config

    async def setup_hook(self) -> None:
        # Load every feature module.
        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        # Register slash commands. If a guild ID is configured we sync to
        # that guild for instant availability; otherwise we sync globally
        # (can take up to an hour to show up).
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash command(s) to guild %s", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s) globally", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id: %s)", self.user, self.user.id if self.user else "?")
        await self.change_presence(activity=discord.Game(name="watching over NYFurs 🐾"))


async def main() -> None:
    config = Config.load()
    bot = FurBot(config)
    async with bot:
        await bot.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
