"""Central configuration for FurBot.

All settings are read from environment variables so that nothing
sensitive (or server-specific) is hard-coded into the source. For local
development you can put these in a `.env` file (see `.env.example`); in
production you set them in your host's dashboard / secrets manager.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    # Optional: only needed for local development. In production the host
    # injects real environment variables, so this import failing is fine.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _get_int(name: str) -> int | None:
    """Read an environment variable as an int, or None if unset/blank."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be a number, got: {raw!r}")


def _get_float(name: str) -> float | None:
    """Read an environment variable as a float, or None if unset/blank."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {name} must be a number, got: {raw!r}")


def _get_bool(name: str) -> bool | None:
    """Read an environment variable as a bool, or None if unset/blank."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on", "y")


@dataclass(frozen=True)
class Config:
    # Required: the bot's login token from the Discord Developer Portal.
    token: str

    # Your server (guild) ID. Used to register slash commands instantly
    # instead of waiting up to an hour for global propagation.
    guild_id: int | None

    # Verification flow.
    verification_channel_id: int | None
    floofs_role_id: int | None
    staff_role_id: int | None
    approval_emoji: str   # staff reacts with this to APPROVE -> grant Floofs
    reject_emoji: str     # staff reacts with this to REJECT -> temp-ban + DM
    warn_emoji: str       # staff reacts with this to WARN -> DM "redo verification"
    reject_cooldown_hours: int  # how long a rejected user is banned before auto-unban

    # Optional channel to log staff actions to.
    log_channel_id: int | None

    # Where to keep the local copy of persisted data (cooldowns, audit log,
    # stats). Used as a fallback and working cache.
    data_dir: str

    # Optional Nextcloud (WebDAV) backing for persistence. If all three are
    # set, the bot stores its data on your Nextcloud server so it survives
    # Railway redeploys. If unset, it just uses local files in data_dir.
    webdav_url: str | None
    webdav_username: str | None
    webdav_password: str | None

    # Optional onboarding defaults. These are bootstrap defaults only — the
    # live values are managed at runtime via /config (see settings.py). Leave
    # them unset and configure from Discord instead.
    onboarding_enabled: bool | None
    onboarding_reminder_hours: int | None
    onboarding_kick_hours: int | None
    onboarding_sweep_minutes: int | None
    onboarding_batch_cap: int | None
    onboarding_action_delay: float | None
    invite_link: str | None

    @property
    def webdav_enabled(self) -> bool:
        return bool(self.webdav_url and self.webdav_username and self.webdav_password)

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            # Help diagnose hosting setups: list which of OUR expected
            # variables the environment actually passed in (names only —
            # never the secret values themselves).
            expected = [
                "DISCORD_TOKEN", "GUILD_ID", "VERIFICATION_CHANNEL_ID",
                "FLOOFS_ROLE_ID", "STAFF_ROLE_ID", "APPROVAL_EMOJI", "LOG_CHANNEL_ID",
            ]
            present = [name for name in expected if os.getenv(name, "").strip()]
            detected = ", ".join(present) if present else "(none)"
            raise RuntimeError(
                "DISCORD_TOKEN is not set. The bot can't log in without it.\n"
                f"  Variables this container actually received: {detected}\n"
                "  Fix: in your host (e.g. Railway > your service > Variables), make sure a\n"
                "  variable named exactly DISCORD_TOKEN exists, then click Deploy/Apply so the\n"
                "  change takes effect. Make sure you're editing the bot service, not a different one."
            )
        return cls(
            token=token,
            guild_id=_get_int("GUILD_ID"),
            verification_channel_id=_get_int("VERIFICATION_CHANNEL_ID"),
            floofs_role_id=_get_int("FLOOFS_ROLE_ID"),
            staff_role_id=_get_int("STAFF_ROLE_ID"),
            approval_emoji=os.getenv("APPROVAL_EMOJI", "✅").strip() or "✅",
            reject_emoji=os.getenv("REJECT_EMOJI", "❌").strip() or "❌",
            warn_emoji=os.getenv("WARN_EMOJI", "⚠️").strip() or "⚠️",
            reject_cooldown_hours=_get_int("REJECT_COOLDOWN_HOURS") or 24,
            log_channel_id=_get_int("LOG_CHANNEL_ID"),
            data_dir=os.getenv("DATA_DIR", "data").strip() or "data",
            webdav_url=os.getenv("WEBDAV_URL", "").strip() or None,
            webdav_username=os.getenv("WEBDAV_USERNAME", "").strip() or None,
            webdav_password=os.getenv("WEBDAV_PASSWORD", "").strip() or None,
            onboarding_enabled=_get_bool("ONBOARDING_ENABLED"),
            onboarding_reminder_hours=_get_int("ONBOARDING_REMINDER_HOURS"),
            onboarding_kick_hours=_get_int("ONBOARDING_KICK_HOURS"),
            onboarding_sweep_minutes=_get_int("ONBOARDING_SWEEP_MINUTES"),
            onboarding_batch_cap=_get_int("ONBOARDING_BATCH_CAP"),
            onboarding_action_delay=_get_float("ONBOARDING_ACTION_DELAY"),
            invite_link=os.getenv("INVITE_LINK", "").strip() or None,
        )
