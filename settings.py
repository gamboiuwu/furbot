"""Runtime settings, editable from Discord via /config and persisted to the
shared store (so they save to Nextcloud and survive restarts/redeploys).

Resolution order for a setting's value:
  1. a stored override (set with /config set)  -> lives in the WebDAV store
  2. the environment variable (from config.py)  -> bootstrap default
  3. the registry default below
"""

from __future__ import annotations

import logging

log = logging.getLogger("furbot.settings")

SETTINGS_KEY = "settings"

# name -> (type, default, help text)
SETTINGS: dict[str, tuple[type, object, str]] = {
    "onboarding_enabled":        (bool,  False, "Master switch for the unverified-onboarding sweep."),
    "onboarding_auto":           (bool,  False, "AUTO mode: send reminders & kick automatically (no staff clicks)."),
    "onboarding_reminder_hours": (int,   12,    "Hours a member can stay unverified before a reminder."),
    "onboarding_grace_hours":    (int,   12,    "Hours after a member is notified before auto-kick."),
    "onboarding_remind_batch":   (int,   25,    "AUTO mode: reminders/kicks processed per cycle."),
    "onboarding_remind_interval_minutes": (int, 10, "AUTO mode: minutes between cycles."),
    "onboarding_kick_hours":     (int,   24,    "Manual mode: hours unverified before kick-eligible (legacy)."),
    "onboarding_sweep_minutes":  (int,   30,    "How often the staff-summary sweep runs (minutes)."),
    "onboarding_batch_cap":      (int,   25,    "Max members actioned per confirmed batch click."),
    "onboarding_action_delay":   (float, 1.5,   "Seconds between actions in a staff-confirmed batch."),
    "invite_link":               (str,   "",    "Invite link included in the removal DM (optional)."),
    "welcome_channel_id":        (int,   863527523757588486, "Channel where a welcome is posted when someone is verified."),
    "welcome_message":           (str,   "🎉 Everyone please welcome {member} to **{server}**! So glad you're here. 🐾",
                                  "Welcome message. {member} = mention, {server} = server name."),
    "birthday_role_id":          (int,   0,     "Role given to members on their birthday."),
    "birthday_channel_id":       (int,   0,     "Channel where birthday shoutouts are posted (e.g. general)."),
    "birthday_message":          (str,   "🎉🎂 Happy birthday {member}! Everyone give them some love today! 🐾",
                                  "Birthday shoutout. {member} = mention, {server} = server name."),
    "birthday_timezone":         (str,   "America/New_York", "Timezone used to decide when it's someone's birthday."),
    "birthday_announce_hour":    (int,   8,     "Hour (0-23, in the birthday timezone) to post shoutouts & give the role."),
    "birthday_promo_channel_id": (int,   0,     "Channel to periodically post the birthday-feature invite (e.g. #bot-commands)."),
    "birthday_promo_days":       (int,   3,     "How often (days) to post the birthday-feature invite."),
    "leaderboard_channel_id":    (int,   0,     "Channel for the month-end staff congratulations (defaults to the log channel)."),
    "verify_pending_enabled":    (bool,  True,  "Ping staff when someone who answered in the verify channel waits too long."),
    "verify_escalate_hours":     (int,   48,    "Hours after a verify-channel message before pinging staff to push them."),
    "verify_followup_hours":     (int,   24,    "Hours after staff is pinged (no action) before the 'are you still there?' nudge."),
    "birthday_promo_message":    (str,
        "Hey there! 🐾\n"
        "If you want, you can let us know about your birthday so the bot can give you the "
        "**Birthday role** when the time comes 🎉\n\n"
        "Just type:\n`/birthday set [day] [month] [year]`\n\n"
        "Totally optional—no pressure, but you'd get a shoutout for the day! >:3",
        "The recurring birthday-feature invite message."),
}


def _coerce(typ: type, raw):
    if typ is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
    if typ is int:
        return int(str(raw).strip())
    if typ is float:
        return float(str(raw).strip())
    return str(raw)


class Settings:
    def __init__(self, store, config) -> None:
        self.store = store
        self.config = config

    def _env_default(self, key: str):
        """The env-var-provided value from config.py, or None if not set."""
        val = getattr(self.config, key, None)
        if val is None or val == "":
            return None
        return val

    def get(self, key: str):
        if key not in SETTINGS:
            raise KeyError(key)
        typ, default, _ = SETTINGS[key]
        overrides = self.store.get(SETTINGS_KEY, {})
        if key in overrides:
            return overrides[key]
        env_val = self._env_default(key)
        if env_val is not None:
            return env_val
        return default

    def source(self, key: str) -> str:
        overrides = self.store.get(SETTINGS_KEY, {})
        if key in overrides:
            return "stored"
        if self._env_default(key) is not None:
            return "env"
        return "default"

    async def set(self, key: str, raw) -> object:
        """Validate, coerce, and persist an override. Raises KeyError for an
        unknown key, ValueError for a bad value."""
        if key not in SETTINGS:
            raise KeyError(key)
        typ = SETTINGS[key][0]
        value = _coerce(typ, raw)
        overrides = dict(self.store.get(SETTINGS_KEY, {}))
        overrides[key] = value
        await self.store.set(SETTINGS_KEY, overrides)
        log.info("Setting %s set to %r", key, value)
        return value

    async def initialize(self) -> None:
        """Make sure settings.json exists and lists every known property, so
        it's visible/editable in the storage folder. Fills any missing key with
        its current effective (env/default) value. Idempotent."""
        overrides = dict(self.store.get(SETTINGS_KEY, {}))
        changed = False
        for key in SETTINGS:
            if key not in overrides:
                overrides[key] = self.get(key)
                changed = True
        if changed:
            await self.store.set(SETTINGS_KEY, overrides)

    async def reset(self, key: str) -> bool:
        """Remove an override (revert to env/default). Returns True if removed."""
        if key not in SETTINGS:
            raise KeyError(key)
        overrides = dict(self.store.get(SETTINGS_KEY, {}))
        if key in overrides:
            del overrides[key]
            await self.store.set(SETTINGS_KEY, overrides)
            return True
        return False
