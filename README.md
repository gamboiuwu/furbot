# FurBot 🐾

A Discord bot for the **NYFurs** server and staff team, built with
[discord.py](https://discordpy.readthedocs.io/).

> 🎨 **Profile picture art by [@NyaswitchNya](https://x.com/NyaswitchNya).**

## What it does today

- **Reaction verification** — in the verification channel, a staff member
  reacts to a new member's message to act on it:
  - ✅ (`APPROVAL_EMOJI`) → gives the member the **Floofs** role + welcome DM
  - ❌ (`REJECT_EMOJI`) → DMs the member, then temp-bans them for a cooldown
    (default 24h) and auto-unbans when it expires
  - ⚠️ (`WARN_EMOJI`) → DMs the member that something was wrong with how they
    verified and to try again
  All actions are logged to the log channel (no pings).
- **`/verify @member`** — a manual fallback for staff to verify someone
  directly (requires the *Manage Roles* permission).
- **`/userinfo @member`** — account age, join date, roles, and verified
  status; flags accounts under a week old (handy for vetting).
- **`/stats`** — verification totals (verified / rejected / warned).
- **`/help`** — lists everything the bot can do.
- **`/floofcount`** — shows how many members have the Floofs role.
- **`/ping`** — quick health check.
- **Welcome Points** — when a member is verified, the first person to say
  *welcome* to them earns a point (⭐ reaction; a one-time explainer DM). Only the
  first welcome counts; every 50 points earns a no-ping shoutout. Check with
  **`/welcomepoints`**.

- **`/config view·set·reset`** — change bot settings from Discord; values are
  saved to your Nextcloud store and apply live (no redeploy).
- **`/onboarding_sweep`** — scan unverified members and post a staff-confirm summary.

All commands are **staff only** (configured staff role, or Manage Roles).

### Unverified-member onboarding

When enabled, the bot helps clear the verification backlog without anyone acting
by hand — but it **never DMs or kicks on its own**. A background sweep posts a
summary to the log channel; staff click to confirm in capped, rate-limited batches.

- Members unverified for ~12h get a reminder DM with an **"✋ I'm waiting to get
  verified"** button.
- Tapping it DMs a **random moderator** a full vetting card (account age, join
  date, roles) plus **Approve ✅ / Needs more info ⚠️ / Deny & kick 👢** buttons.
- Members still unverified at ~24h become kick-eligible (removed, can rejoin).

Turn it on from Discord (no Railway editing): `/config set onboarding_enabled true`.
Tune with `/config set onboarding_reminder_hours 12` etc. The bot needs the
**Kick Members** permission. All buttons keep working after the bot restarts.

### Storage / persistence

The bot keeps a small amount of data — pending cooldowns, an audit log of
staff actions, and stats. By default this lives in local files under
`DATA_DIR`. To make it survive Railway redeploys, point it at your own
Nextcloud server over WebDAV by setting `WEBDAV_URL`, `WEBDAV_USERNAME`, and
`WEBDAV_PASSWORD` (use a Nextcloud **app password**, not your account
password). If Nextcloud is unreachable, the bot logs it and keeps working
off the local copy.

The code is organized into "cogs" (feature modules) under `cogs/`, so adding
new staff tools later is straightforward.

---

## 1. Create the bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Open the **Bot** tab → **Reset Token** → copy the token (this is your `DISCORD_TOKEN`).
3. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
4. Open the **OAuth2 → URL Generator** tab:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **Manage Roles**, **Ban Members** (for the reject
     cooldown), **Read Messages/View Channels**, **Send Messages**,
     **Read Message History**, **Add Reactions**.
5. Open the generated URL and invite the bot to the NYFurs server.

> **Important:** in your server's role list, drag the bot's role **above** the
> Floofs role. A bot can only assign roles that sit below its own.

## 2. Gather the IDs

Turn on **Developer Mode** (User Settings → Advanced → Developer Mode), then
right-click to **Copy ID** for:

- The server → `GUILD_ID`
- The verification channel → `VERIFICATION_CHANNEL_ID`
- The Floofs role (right-click it in Server Settings → Roles) → `FLOOFS_ROLE_ID`
- The staff role → `STAFF_ROLE_ID`
- (Optional) a log channel → `LOG_CHANNEL_ID`

## 3. Run it locally (to test)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and fill in your values
python bot.py
```

You should see `Logged in as FurBot...` in the console. Post a test message in
the verification channel and react to it with ✅ as a staff member — the author
should get the Floofs role.

---

## 4. Host it 24/7 (so it doesn't run on your computer)

The repo ships with a `Dockerfile`, so it runs anywhere that runs containers.
Whichever host you pick, set the same variables from `.env.example` as
**environment variables / secrets** in that host's dashboard — **never commit
your real `.env`.**

### Option A — Railway (easiest)
1. Push this repo to GitHub.
2. On [railway.app](https://railway.app): **New Project → Deploy from GitHub repo**.
3. In the service's **Variables** tab, add `DISCORD_TOKEN`, `GUILD_ID`, etc.
4. Railway detects the Dockerfile and deploys. Check the **Deploy Logs** for
   `Logged in as FurBot...`.

### Option B — Fly.io
```bash
fly launch --no-deploy          # creates fly.toml from the Dockerfile
fly secrets set DISCORD_TOKEN=... GUILD_ID=... VERIFICATION_CHANNEL_ID=... \
  FLOOFS_ROLE_ID=... STAFF_ROLE_ID=... APPROVAL_EMOJI=✅
fly deploy
```

### Option C — A VPS (DigitalOcean, etc.)
Install Docker, copy the repo over, then:
```bash
docker build -t furbot .
docker run -d --restart=unless-stopped --env-file .env --name furbot furbot
```

---

## Adding new features

Create a new file in `cogs/` modeled on `cogs/general.py`, then add its module
path to `INITIAL_COGS` in `bot.py`. Restart (or redeploy) and the new slash
commands sync automatically.
