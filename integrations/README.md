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

- **Accept** → the bot retags *Accepted* and either:
  - **auto-creates the draft on the site** (if auto-create is enabled — see
    below), posting the new event's link, or
  - posts a copy-paste-ready Indico draft plus the create link (the default,
    assisted mode — no special access needed).
- **Decline** → a modal collects the reason; the bot posts the applicant's
  contact + reason for staff to follow up, and retags *Declined*.

### Optional: auto-create the Indico draft on Accept

Indico's HTTP API is **read-only** — there's no supported endpoint to create an
event. To make the site create it anyway, the bot drives the web UI like a
browser: it logs in, grabs the CSRF token, and submits the create-event form.

It creates the event as an **unlisted meeting** (a private draft, not shown in
the category until staff publish it). The meeting form only accepts title +
date/time, so the bot posts the description, location, host, etc. in the thread
for staff to paste into Indico after the draft exists.

To turn it on:

1. Add Railway variables (env-only secrets — never stored in the repo):
   - `INDICO_USERNAME` — an Indico account that can create events in the
     category (local-login username; SSO-only accounts won't work).
   - `INDICO_PASSWORD` — that account's password.
2. Make sure `indico_category_id` is set (events can't be created at the root):
   `/config set indico_category_id <id>`
3. Enable it: `/config set event_autocreate_enabled true`
4. Check `/eventintake status` — *Auto-create drafts* should read
   **✅ on (unlisted drafts)**.

It's best-effort: if login fails, the form rejects a field, or anything else
goes wrong, the bot automatically falls back to the copy-paste draft and notes
why, so staff are never blocked.
