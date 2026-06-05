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
    onboarding_auto: bool | None
    onboarding_reminder_hours: int | None
    onboarding_grace_hours: int | None
    onboarding_remind_batch: int | None
    onboarding_remind_interval_minutes: int | None
    onboarding_kick_hours: int | None
    onboarding_sweep_minutes: int | None
    onboarding_batch_cap: int | None
    onboarding_action_delay: float | None
    invite_link: str | None
    welcome_channel_id: int | None
    welcome_message: str | None
    birthday_role_id: int | None
    birthday_channel_id: int | None
    birthday_message: str | None
    birthday_timezone: str | None
    birthday_announce_hour: int | None
    birthday_promo_channel_id: int | None
    birthday_promo_days: int | None
    birthday_promo_message: str | None
    leaderboard_channel_id: int | None
    commission_watch_enabled: bool | None
    commission_watch_hours: int | None
    commission_timeout_hours: int | None
    commission_threshold: float | None
    verify_thanks_enabled: bool | None
    verify_pending_enabled: bool | None
    verify_escalate_hours: int | None
    verify_followup_hours: int | None
    warn_grace_hours: int | None
    warn_kick_enabled: bool | None
    blocked_callout_enabled: bool | None
    blocked_image_file: str | None
    blocked_image_url: str | None
    task_enabled: bool | None
    task_forum_id: int | None
    task_done_emoji: str | None
    task_nudge_first_days: int | None
    task_nudge_repeat_days: int | None
    dm_relay_enabled: bool | None
    member_log_enabled: bool | None
    hi_enabled: bool | None
    hi_per_week: int | None
    hi_channel_id: int | None
    welcome_points_enabled: bool | None
    welcome_points_channel_id: int | None
    welcome_window_minutes: int | None
    welcome_shoutout_every: int | None
    # Indico (events.nyfurs.org) — token is a secret, env-only (never stored).
    indico_api_token: str | None
    # Shared secret for the Event Application web intake (HMAC) — env-only secret.
    event_form_secret: str | None
    events_sync_enabled: bool | None
    events_sync_interval_hours: int | None
    events_interest_dm_enabled: bool | None
    events_reminder_enabled: bool | None
    events_reminder_hours: int | None
    hi_pileon_enabled: bool | None
    hi_pileon_count: int | None
    slur_watch_enabled: bool | None
    argument_enabled: bool | None
    argument_channel_id: int | None
    verify_copy_kick_enabled: bool | None
    verify_copy_similarity: float | None
    verify_copy_min_chars: int | None
    verify_copy_window: int | None
    risk_warnings_enabled: bool | None
    risk_learn_enabled: bool | None
    risk_dm_threshold: float | None
    risk_raid_window_minutes: int | None
    risk_raid_min_joins: int | None
    review_enabled: bool | None
    feedback_channel_id: int | None
    review_after_days: int | None
    review_batch: int | None
    review_interval_minutes: int | None
    review_recheck_days: int | None

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
            onboarding_auto=_get_bool("ONBOARDING_AUTO"),
            onboarding_reminder_hours=_get_int("ONBOARDING_REMINDER_HOURS"),
            onboarding_grace_hours=_get_int("ONBOARDING_GRACE_HOURS"),
            onboarding_remind_batch=_get_int("ONBOARDING_REMIND_BATCH"),
            onboarding_remind_interval_minutes=_get_int("ONBOARDING_REMIND_INTERVAL_MINUTES"),
            onboarding_kick_hours=_get_int("ONBOARDING_KICK_HOURS"),
            onboarding_sweep_minutes=_get_int("ONBOARDING_SWEEP_MINUTES"),
            onboarding_batch_cap=_get_int("ONBOARDING_BATCH_CAP"),
            onboarding_action_delay=_get_float("ONBOARDING_ACTION_DELAY"),
            invite_link=os.getenv("INVITE_LINK", "").strip() or None,
            welcome_channel_id=_get_int("WELCOME_CHANNEL_ID"),
            welcome_message=os.getenv("WELCOME_MESSAGE", "").strip() or None,
            birthday_role_id=_get_int("BIRTHDAY_ROLE_ID"),
            birthday_channel_id=_get_int("BIRTHDAY_CHANNEL_ID"),
            birthday_message=os.getenv("BIRTHDAY_MESSAGE", "").strip() or None,
            birthday_timezone=os.getenv("BIRTHDAY_TIMEZONE", "").strip() or None,
            birthday_announce_hour=_get_int("BIRTHDAY_ANNOUNCE_HOUR"),
            birthday_promo_channel_id=_get_int("BIRTHDAY_PROMO_CHANNEL_ID"),
            birthday_promo_days=_get_int("BIRTHDAY_PROMO_DAYS"),
            birthday_promo_message=os.getenv("BIRTHDAY_PROMO_MESSAGE", "").strip() or None,
            leaderboard_channel_id=_get_int("LEADERBOARD_CHANNEL_ID"),
            commission_watch_enabled=_get_bool("COMMISSION_WATCH_ENABLED"),
            commission_watch_hours=_get_int("COMMISSION_WATCH_HOURS"),
            commission_timeout_hours=_get_int("COMMISSION_TIMEOUT_HOURS"),
            commission_threshold=_get_float("COMMISSION_THRESHOLD"),
            verify_thanks_enabled=_get_bool("VERIFY_THANKS_ENABLED"),
            verify_pending_enabled=_get_bool("VERIFY_PENDING_ENABLED"),
            verify_escalate_hours=_get_int("VERIFY_ESCALATE_HOURS"),
            verify_followup_hours=_get_int("VERIFY_FOLLOWUP_HOURS"),
            warn_grace_hours=_get_int("WARN_GRACE_HOURS"),
            warn_kick_enabled=_get_bool("WARN_KICK_ENABLED"),
            blocked_callout_enabled=_get_bool("BLOCKED_CALLOUT_ENABLED"),
            blocked_image_file=os.getenv("BLOCKED_IMAGE_FILE", "").strip() or None,
            blocked_image_url=os.getenv("BLOCKED_IMAGE_URL", "").strip() or None,
            review_enabled=_get_bool("REVIEW_ENABLED"),
            feedback_channel_id=_get_int("FEEDBACK_CHANNEL_ID"),
            review_after_days=_get_int("REVIEW_AFTER_DAYS"),
            review_batch=_get_int("REVIEW_BATCH"),
            review_interval_minutes=_get_int("REVIEW_INTERVAL_MINUTES"),
            review_recheck_days=_get_int("REVIEW_RECHECK_DAYS"),
            task_enabled=_get_bool("TASK_ENABLED"),
            task_forum_id=_get_int("TASK_FORUM_ID"),
            task_done_emoji=os.getenv("TASK_DONE_EMOJI", "").strip() or None,
            task_nudge_first_days=_get_int("TASK_NUDGE_FIRST_DAYS"),
            task_nudge_repeat_days=_get_int("TASK_NUDGE_REPEAT_DAYS"),
            dm_relay_enabled=_get_bool("DM_RELAY_ENABLED"),
            member_log_enabled=_get_bool("MEMBER_LOG_ENABLED"),
            hi_enabled=_get_bool("HI_ENABLED"),
            hi_per_week=_get_int("HI_PER_WEEK"),
            hi_channel_id=_get_int("HI_CHANNEL_ID"),
            welcome_points_enabled=_get_bool("WELCOME_POINTS_ENABLED"),
            welcome_points_channel_id=_get_int("WELCOME_POINTS_CHANNEL_ID"),
            welcome_window_minutes=_get_int("WELCOME_WINDOW_MINUTES"),
            welcome_shoutout_every=_get_int("WELCOME_SHOUTOUT_EVERY"),
            indico_api_token=os.getenv("INDICO_API_TOKEN", "").strip() or None,
            event_form_secret=os.getenv("EVENT_FORM_SECRET", "").strip() or None,
            events_sync_enabled=_get_bool("EVENTS_SYNC_ENABLED"),
            events_sync_interval_hours=_get_int("EVENTS_SYNC_INTERVAL_HOURS"),
            events_interest_dm_enabled=_get_bool("EVENTS_INTEREST_DM_ENABLED"),
            events_reminder_enabled=_get_bool("EVENTS_REMINDER_ENABLED"),
            events_reminder_hours=_get_int("EVENTS_REMINDER_HOURS"),
            hi_pileon_enabled=_get_bool("HI_PILEON_ENABLED"),
            hi_pileon_count=_get_int("HI_PILEON_COUNT"),
            slur_watch_enabled=_get_bool("SLUR_WATCH_ENABLED"),
            argument_enabled=_get_bool("ARGUMENT_ENABLED"),
            argument_channel_id=_get_int("ARGUMENT_CHANNEL_ID"),
            verify_copy_kick_enabled=_get_bool("VERIFY_COPY_KICK_ENABLED"),
            verify_copy_similarity=_get_float("VERIFY_COPY_SIMILARITY"),
            verify_copy_min_chars=_get_int("VERIFY_COPY_MIN_CHARS"),
            verify_copy_window=_get_int("VERIFY_COPY_WINDOW"),
            risk_warnings_enabled=_get_bool("RISK_WARNINGS_ENABLED"),
            risk_learn_enabled=_get_bool("RISK_LEARN_ENABLED"),
            risk_dm_threshold=_get_float("RISK_DM_THRESHOLD"),
            risk_raid_window_minutes=_get_int("RISK_RAID_WINDOW_MINUTES"),
            risk_raid_min_joins=_get_int("RISK_RAID_MIN_JOINS"),
        )
