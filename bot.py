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
from settings import Settings
from store import Store
from webdav import WebDAVClient

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
    "cogs.config",
    "cogs.onboarding",
    "cogs.birthday",
    "cogs.leaderboard",
    "cogs.review",
    "cogs.taskboard",
    "cogs.dmrelay",
    "cogs.serverlog",
    "cogs.fart",
    "cogs.hipileon",
    "cogs.slurwatch",
    "cogs.argument",
)


class FurBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=INTENTS,
            help_command=commands.DefaultHelpCommand(),
        )
        self.config = config

        # Shared persistence. Backed by Nextcloud (WebDAV) if configured,
        # otherwise local files in DATA_DIR.
        webdav = None
        if config.webdav_enabled:
            webdav = WebDAVClient(config.webdav_url, config.webdav_username, config.webdav_password)
        self.store = Store(local_dir=config.data_dir, webdav=webdav)
        # Runtime settings, persisted to the store and editable via /config.
        self.settings = Settings(self.store, config)

    async def setup_hook(self) -> None:
        # Prepare persistence before any cog needs it.
        if self.store.webdav:
            try:
                await self.store.webdav.ensure_base()
                ok = await self.store.webdav.check()
                log.info("Nextcloud storage %s.", "connected" if ok else "NOT reachable (using local fallback)")
            except Exception:
                log.exception("Nextcloud setup failed; using local fallback.")
        else:
            log.info("Nextcloud not configured; using local files in %s.", self.config.data_dir)
        await self.store.load()

        # Populate the storage folder up-front so the JSON files exist (and are
        # visible on Nextcloud) rather than appearing only on first change.
        await self.settings.initialize()
        await self.store.ensure_defaults({
            "stats": {},
            "audit": [],
            "pending_unbans": {},
            "onboarding.reminded": {},
            "onboarding.escalated": {},
        })
        # Write a complete snapshot of all configuration (core + settings) to
        # config.json on the WebDAV drive.
        await self.save_config_snapshot()

        # Load every feature module.
        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        # Register persistent button handlers so they keep working after a
        # restart. DynamicItems are registered by class; the batch view by
        # instance (its custom_ids are fixed).
        from cogs.onboarding import BatchConfirmView, ModActionButton, PhoneReviewButton, WaitingButton
        from cogs.review import ReviewButton, ReviewConsentButton, ReviewOptOutButton
        self.add_dynamic_items(
            WaitingButton, PhoneReviewButton, ModActionButton,
            ReviewButton, ReviewConsentButton, ReviewOptOutButton,
        )
        self.add_view(BatchConfirmView(self))

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

    async def save_config_snapshot(self) -> None:
        """Write a complete, non-secret snapshot of all configuration (the core
        env config + every effective /config setting) to config.json on WebDAV."""
        import dataclasses

        from settings import SETTINGS

        core = dataclasses.asdict(self.config)
        for secret in ("token", "webdav_url", "webdav_username", "webdav_password"):
            core.pop(secret, None)
        snapshot = {
            "core_config": core,
            "settings": {key: self.settings.get(key) for key in SETTINGS},
        }
        try:
            await self.store.set("config", snapshot)
        except Exception:
            log.exception("Failed to save config snapshot")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id: %s)", self.user, self.user.id if self.user else "?")
        await self.change_presence(activity=discord.Game(name="watching over NYFurs 🐾"))
        self._log_config_check()

    def _log_config_check(self) -> None:
        """Print, to the logs, each configured ID and whether it actually
        resolves to a real role/channel in the server. Makes "wrong ID"
        misconfiguration obvious at a glance."""
        cfg = self.config
        guild = self.get_guild(cfg.guild_id) if cfg.guild_id else (self.guilds[0] if self.guilds else None)
        if guild is None:
            log.warning("CONFIG CHECK: bot is not in any guild yet — skipping ID validation.")
            return

        log.info("CONFIG CHECK in guild '%s' (id: %s):", guild.name, guild.id)

        def check_role(label: str, role_id: int | None) -> None:
            if not role_id:
                log.info("  %s: (not set)", label)
                return
            role = guild.get_role(role_id)
            if role:
                log.info("  %s=%s -> OK ('%s')", label, role_id, role.name)
            else:
                log.warning("  %s=%s -> NOT FOUND (no role with this ID in the server!)", label, role_id)

        def check_channel(label: str, channel_id: int | None) -> None:
            if not channel_id:
                log.info("  %s: (not set)", label)
                return
            channel = guild.get_channel(channel_id)
            if channel:
                log.info("  %s=%s -> OK ('#%s')", label, channel_id, channel.name)
            else:
                log.warning("  %s=%s -> NOT FOUND (no channel with this ID in the server!)", label, channel_id)

        check_channel("VERIFICATION_CHANNEL_ID", cfg.verification_channel_id)
        check_role("FLOOFS_ROLE_ID", cfg.floofs_role_id)
        check_role("STAFF_ROLE_ID", cfg.staff_role_id)
        check_channel("LOG_CHANNEL_ID", cfg.log_channel_id)
        log.info("CONFIG CHECK complete. Fix any 'NOT FOUND' values in your host's Variables.")


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
