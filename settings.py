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
    "commission_watch_enabled":  (bool,  True,  "Warn + time out members who advertise art commissions within days of verifying (rules 5c/5d)."),
    "commission_watch_hours":    (int,   72,    "How long after verification a member is watched for commission soliciting."),
    "commission_timeout_hours":  (int,   24,    "Timeout length applied for new-member commission soliciting."),
    "commission_threshold":      (float, 0.6,   "Classifier confidence (0-1) to flag commission soliciting. Higher = stricter/fewer flags."),
    "verify_thanks_enabled":     (bool,  True,  "Privately DM a varied thank-you the first time someone posts in the verify channel."),
    "verify_pending_enabled":    (bool,  True,  "Ping staff when someone who answered in the verify channel waits too long."),
    "verify_escalate_hours":     (int,   48,    "Hours after a verify-channel message before pinging staff to push them."),
    "verify_followup_hours":     (int,   24,    "Hours after staff is pinged (no action) before the 'are you still there?' nudge."),
    "warn_grace_hours":          (int,   24,    "Hours after a 'needs more info' warning before the member becomes kick-eligible."),
    "warn_kick_enabled":         (bool,  True,  "Automatically kick warned members once their grace period expires (independent of auto-onboarding)."),
    "blocked_callout_enabled":   (bool,  True,  "Humorously call out staff in the welcome channel if they've blocked/closed DMs to the bot."),
    "blocked_image_file":        (str,   "",    "Filename of the call-out image on the WebDAV drive (Bot Data folder). Preferred over the URL."),
    "blocked_image_url":         (str,   "",    "Image/GIF URL used for the 'point and laugh' staff call-out (fallback if no WebDAV file)."),
    "review_enabled":            (bool,  False, "Master switch for the one-month member check-in survey."),
    "feedback_channel_id":       (int,   1510064548084715520, "Channel (in the staff server) where check-in feedback is posted."),
    "review_after_days":         (int,   30,    "Days after joining before a member gets the check-in DM."),
    "review_batch":              (int,   25,    "Max check-in DMs sent per cycle."),
    "review_interval_minutes":   (int,   30,    "Minutes between check-in cycles."),
    "review_recheck_days":       (int,   90,    "Days until an opted-in member is asked again (~3 months)."),
    "birthday_promo_message":    (str,
        "Hey there! 🐾\n"
        "If you want, you can let us know about your birthday so the bot can give you the "
        "**Birthday role** when the time comes 🎉\n\n"
        "Just type:\n`/birthday set [day] [month] [year]`\n\n"
        "Totally optional—no pressure, but you'd get a shoutout for the day! >:3",
        "The recurring birthday-feature invite message."),
    "task_enabled":              (bool,  True,  "Enable the staff task-forum assistant (nudges, board, completion)."),
    "task_forum_id":             (int,   1272358508121034802, "Forum channel used as the staff to-do board."),
    "task_done_emoji":           (str,   "✅",   "React with this on a task's first post to mark it complete."),
    "task_nudge_first_days":     (int,   3,     "Days of inactivity before the first nudge to a task's owner."),
    "task_nudge_repeat_days":    (int,   7,     "Days between repeat nudges after the first."),
    "dm_relay_enabled":          (bool,  True,  "Greet people who DM the bot and log their messages to the log channel."),
    "member_log_enabled":        (bool,  True,  "Log joins, leaves, kicks, bans, and unbans to the log channel."),
    "welcome_points_enabled":    (bool,  True,  "Reward the first person to welcome each newly-verified member."),
    "welcome_points_channel_id": (int,   0,     "Channel watched for welcomes (0 = use the welcome channel)."),
    "welcome_window_minutes":    (int,   60,    "How long after a verification a 'welcome' can still earn a point."),
    "welcome_shoutout_every":    (int,   50,    "Give a member a shoutout each time they hit this many Welcome Points."),
    "indico_url":                (str,   "https://events.nyfurs.org", "Base URL of the Indico instance for /events."),
    "indico_category_id":        (int,   0,     "Indico category id to pull events from (0 = root category)."),
    "events_days":               (int,   30,    "How many days ahead /events looks for upcoming events."),
    "events_sync_enabled":       (bool,  True,  "Auto-post upcoming Indico events to the server's Scheduled Events page."),
    "events_sync_interval_hours":(int,   6,     "How often to sync events to the Scheduled Events page."),
    "events_interest_dm_enabled":(bool,  True,  "DM users a registration link when they mark Interested (or withdraw) on an event."),
    "events_reminder_enabled":   (bool,  True,  "DM interested users a reminder before an event starts."),
    "events_reminder_hours":     (int,   24,    "How many hours before an event to remind interested users."),
    "hi_enabled":                (bool,  True,  "Occasionally post 'hi' in the welcome channel for fun."),
    "hi_per_week":               (int,   5,     "Roughly how many times a week the bot says hi."),
    "hi_channel_id":             (int,   0,     "Channel for the random hi (0 = use the welcome channel)."),
    "hi_pileon_enabled":         (bool,  True,  "When several people say 'hi' in a row, the bot joins in and says hi."),
    "hi_pileon_count":           (int,   5,     "How many people must say 'hi' in a row before the bot joins in."),
    "slur_watch_enabled":        (bool,  True,  "Respond 'THIS BETTER BE SATIRICAL' when someone uses the f-slur."),
    "argument_enabled":          (bool,  True,  "Once a week the bot has a self-argument in the welcome/general channel."),
    "argument_channel_id":       (int,   0,     "Channel for the weekly self-argument (0 = use the welcome channel)."),
    "verify_copy_kick_enabled":  (bool,  True,  "Auto-kick members who copy another person's verification message (spam)."),
    "verify_copy_similarity":    (float, 0.9,   "Similarity (0-1) to another member's message that counts as a copy (0.9 = 90%)."),
    "verify_copy_min_chars":     (int,   40,    "Ignore messages shorter than this for copy-detection (avoids false positives)."),
    "verify_copy_window":        (int,   500,   "How many recent verification messages to compare against for copies."),
    "risk_warnings_enabled":     (bool,  True,  "DM a moderator when a new join looks like a likely scam/bot account."),
    "risk_learn_enabled":        (bool,  True,  "Let the join-risk checks adapt over time from who gets verified vs denied."),
    "risk_dm_threshold":         (float, 0.6,   "Risk score (0-1) at which to warn a mod about a new join. Higher = fewer alerts."),
    "risk_raid_window_minutes":  (int,   10,    "Window for detecting a burst of rapid joins (raid)."),
    "risk_raid_min_joins":       (int,   6,     "Joins within the window that count as a raid burst."),
    "roommate_enabled":          (bool,  False, "Master switch for the 18+ con roommate finder."),
    "roommate_hub_channel_id":   (int,   1512529974266040443, "Channel where the roommate-finder hub message is posted (/roommate setup)."),
    "roommate_adult_role_id":    (int,   0,     "Fallback 18+ role if no age-range roles are configured (0 = use the verified-members role)."),
    "roommate_age_roles":        (str,
        "18-19:968639927942271066,20-29:968639955880534037,30-39:968639982052995102,"
        "40-49:1484909865590718625,50-59:1484909891939336375,60+:1484909914701697095",
        "Age-range roles as 'label:role_id' pairs (comma-separated). Holding any one proves 18+; used for age matching."),
    "roommate_cons":             (str,   "",    "Comma-separated list of conventions members can pick from."),
    "roommate_match_interval_hours": (int, 6,   "Safety-net sweep interval (hours) — matching is mostly immediate on registration."),
    "roommate_cons_channel_id":  (int,   1173049058856816772, "Forum/channel scanned by /roommate importcons to auto-add con names."),
    "roommate_con_dates":        (str,   "", "Optional con date windows for validation, e.g. 'Anthrocon: 2026-07-02..2026-07-05; FurDU: 2026-04-24..2026-04-27'."),
    "reports_enabled":           (bool,  False, "Master switch for the member safety-report button."),
    "reports_channel_id":        (int,   0,     "Private staff channel where safety reports are posted (staff-only access)."),
    "reports_role_id":           (int,   0,     "Role pinged when a safety report is filed (e.g. Safety Team). 0 = no ping."),
    "reports_hub_channel_id":    (int,   0,     "Channel where the pinned 'Report' button lives (/report setup)."),
    "suggestions_enabled":       (bool,  False, "Master switch for suggestion-box auto-polls and staff to-do follow-up."),
    "suggestions_channel_id":    (int,   1211177177379242074, "Suggestion-box forum/channel whose threads get a final poll after 2 weeks."),
    "suggestions_todo_channel_id": (int, 1272358508121034802, "Staff to-do forum/channel where approved suggestions are pushed."),
    "suggestions_staff_role_id": (int,   0,     "Role pinged when a to-do is overdue (0 = use the staff role from config)."),
    "suggestions_active_days":   (int,   14,    "Days a suggestion thread is open before its final poll is posted."),
    "suggestions_poll_days":     (int,   14,    "How long the final Yes/No poll stays open (days, max 32)."),
    "suggestions_deadline_days": (int,   14,    "Days after a to-do is created before the 'Live' tag is expected."),
    "suggestions_ping_days":     (int,   3,     "How often (days) to ping staff about an overdue to-do until it's Live."),
    "suggestions_live_tag":      (str,   "Live", "Forum tag name on the to-do channel that marks a task integrated/done."),
    "event_intake_enabled":      (bool,  False, "Master switch for the Event Application intake (Google Form -> #event-post)."),
    "event_ping_enabled":        (bool,  False, "Ping the Events Team role on new applications and escalations. Disable during testing."),
    "event_post_channel_id":     (int,   1356772182956445786, "Forum channel where event applications are posted for review."),
    "event_team_role_id":        (int,   1217534869664567306, "Events Team role pinged for new/overdue applications."),
    "event_tag_pending":         (str,   "Changes Required", "Forum tag applied to a new application awaiting a decision."),
    "event_tag_accepted":        (str,   "Accepted", "Forum tag applied when an application is accepted."),
    "event_tag_declined":        (str,   "Declined", "Forum tag applied when an application is declined."),
    "event_escalate_hours":      (int,   72,    "Hours with no decision before the Events Team starts getting daily pings."),
    "event_escalate_repeat_hours": (int, 24,    "How often (hours) to re-ping the Events Team about an undecided application."),
    "event_intake_path":         (str,   "/event-intake", "URL path the Google Form's Apps Script POSTs submissions to."),
}


# Settings grouped into categories so /config is easy to navigate. Each key is
# matched to the FIRST group whose prefixes it starts with; anything unmatched
# falls into "Other". Order here is the order shown in /config view.
SETTING_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("🚪", "Onboarding",        ("onboarding_", "invite_link")),
    ("✅", "Verification",      ("verify_", "warn_")),
    ("🛡️", "Join risk & raids", ("risk_",)),
    ("👋", "Welcome & points",  ("welcome_", "blocked_")),
    ("🎂", "Birthdays",         ("birthday_",)),
    ("🏆", "Leaderboard",       ("leaderboard_",)),
    ("🎨", "Commission watch",  ("commission_",)),
    ("📋", "Member check-in",   ("review_", "feedback_")),
    ("📌", "Staff tasks",       ("task_",)),
    ("📅", "Events",            ("indico_", "events_")),
    ("📜", "DM relay & logging", ("dm_relay_", "member_log_")),
    ("🎉", "Fun & chatter",     ("hi_", "argument_", "slur_")),
    ("🛏️", "Roommate finder",   ("roommate_",)),
    ("🚨", "Safety reports",    ("reports_",)),
    ("🗳️", "Suggestion box",    ("suggestions_",)),
    ("🎫", "Event applications", ("event_",)),
]


def group_of(key: str) -> str:
    """Return the category NAME (no emoji) a setting key belongs to."""
    for _emoji, name, prefixes in SETTING_GROUPS:
        if key.startswith(prefixes):
            return name
    return "Other"


def group_label(name: str) -> str:
    """'emoji name' label for a category name (name itself if unknown)."""
    for emoji, gname, _ in SETTING_GROUPS:
        if gname == name:
            return f"{emoji} {gname}"
    return name


def grouped_settings() -> dict[str, list[str]]:
    """Ordered {category name: [keys]} for every registered setting (no empties)."""
    out: dict[str, list[str]] = {name: [] for _e, name, _p in SETTING_GROUPS}
    out["Other"] = []
    for key in SETTINGS:
        out[group_of(key)].append(key)
    return {name: keys for name, keys in out.items() if keys}


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
