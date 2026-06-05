# Integrations

## Event Application intake (`event_form.gs`)

Connects the NYFurs Event Application Google Form to FurBot's `cogs/events_intake.py`.
On every form submit, a Google Apps Script POSTs the answers (and any uploaded
files) to the bot's signed `/event-intake` endpoint; the bot then creates a forum
post in **#event-post** with **Accept / Decline** buttons, pings the Events Team,
and escalates daily after 72 h with no decision.

### One-time setup

1. **Make `#event-post` a forum** with the tags `Changes Required`, `Accepted`,
   `Declined`.
2. **Expose the bot publicly** — Railway → the bot service → *Settings →
   Networking → Generate Domain*. Railway sets `PORT`; the bot binds to it.
3. **Add secrets** (Railway → Variables):
   - `EVENT_FORM_SECRET` — a long random string (shared with the form).
4. **Enable the feature**: `/config set event_intake_enabled true`
   (channel/role default to the IDs already configured; check `/eventintake status`).
5. **Install the Apps Script**: open the Form → ⋮ → *Script editor*, paste
   `event_form.gs`, set `ENDPOINT` (`https://<your-bot>/event-intake`) and
   `SECRET` (same as `EVENT_FORM_SECRET`), run `onEventSubmit` once to authorize,
   then add a trigger: *From form → On form submit → `onEventSubmit`*.

### How requests are secured

Each POST is signed: `X-Signature` = HMAC-SHA256 (hex) of `"<X-Timestamp>.<body>"`
keyed by `EVENT_FORM_SECRET`. The bot rejects bad signatures and timestamps more
than 10 minutes off, and de-duplicates by the form's submission id so retries
never double-post.

### Decisions

- **Accept** → the bot posts a copy-paste-ready Indico **Conference** draft plus
  the create link (assisted — no Indico write access needed), and retags
  *Accepted*.
- **Decline** → a modal collects the reason; the bot posts the applicant's
  contact + reason for staff to follow up, and retags *Declined*.
