"""Event Application intake: Google Form -> #event-post forum -> Accept/Decline.

A Google Apps Script on the NYFurs Event Application form POSTs each submission
(answers + uploaded files) to this bot's signed HTTP endpoint. The bot then:

  * creates a forum post in #event-post with every answer formatted and all
    uploaded images/files re-attached, tagged "Changes Required",
  * pings the Events Team and shows **Accept** / **Decline** buttons,
  * on Accept, posts a copy-paste-ready Indico "Conference" draft + the create
    link (assisted draft — no Indico write access needed),
  * on Decline, collects a reason and hands staff the applicant's contact so
    they can reach out,
  * and re-pings the Events Team daily if no decision is made within 72 hours.

The web server only starts when EVENT_FORM_SECRET is set (an env-only secret).
Submissions are HMAC-signed (with a timestamp to stop replays) and de-duplicated
by submission id, so retries never double-post.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff
from integrations.indico_create import IndicoCreateError, IndicoEventCreator

log = logging.getLogger("furbot.events_intake")

# Bump on each deploy-worthy change so /healthz reveals exactly what's running.
# (Lets us confirm a Railway redeploy actually picked up new code.)
BUILD = "2026-06-06.all-questions"

APPS = "event_applications"   # {submission_id: {thread_id, status, created, last_ping, mapped...}}
MAX_FILE_BYTES = 8 * 1024 * 1024   # keep within the default Discord upload limit
MAX_FILES = 10                     # Discord caps attachments per message
SIG_TOLERANCE = 600                # seconds of clock skew allowed on signed requests


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _find(answers: list[dict], *needles: str) -> str:
    """First answer whose (normalized) question contains any needle."""
    keys = [_norm(n) for n in needles]
    for a in answers:
        q = _norm(a.get("q", ""))
        if any(k in q for k in keys):
            val = a.get("a")
            return str(val).strip() if val is not None else ""
    return ""


# ============================ persistent buttons ==============================

class EventDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"evapp:(?P<sid>[A-Za-z0-9_\-]+):(?P<act>accept|decline)",
):
    _SPEC = {
        "accept": ("✅ Accept", discord.ButtonStyle.success),
        "decline": ("✖️ Decline", discord.ButtonStyle.danger),
    }

    def __init__(self, sid: str, act: str) -> None:
        self.sid = sid
        self.act = act
        label, style = self._SPEC[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"evapp:{sid}:{act}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "EventsIntake | None" = interaction.client.get_cog("EventsIntake")
        if cog is None:
            await interaction.response.send_message("Event intake isn't available right now.", ephemeral=True)
            return
        await cog.on_decision(interaction, self.sid, self.act)


def build_decision_view(sid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EventDecisionButton(sid, "accept"))
    view.add_item(EventDecisionButton(sid, "decline"))
    return view


class DeclineModal(discord.ui.Modal, title="Decline event application"):
    def __init__(self, cog: "EventsIntake", sid: str) -> None:
        super().__init__()
        self.cog = cog
        self.sid = sid
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=True, max_length=1000,
            placeholder="Why is this being declined? This is shared with staff so they can explain it to the applicant.",
        )
        self.add_item(discord.ui.Label(text="Reason for declining", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.finish_decline(interaction, self.sid, self.reason.value.strip())


# ================================= cog ========================================

class EventsIntake(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings
        self._runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        await self._start_server()
        self.escalation_loop.start()

    async def cog_unload(self) -> None:
        self.escalation_loop.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _s(self, key: str):
        return self.settings.get(key)

    def _autocreate_status(self) -> str:
        if not self._s("event_autocreate_enabled"):
            return "off (manual paste-draft)"
        have_creds = bool(getattr(self.config, "indico_username", None)
                          and getattr(self.config, "indico_password", None))
        if not have_creds:
            return "⚠️ on, but INDICO_USERNAME/INDICO_PASSWORD not set"
        return "✅ on (unlisted drafts)"

    # ---- web server ------------------------------------------------------

    async def _start_server(self) -> None:
        secret = getattr(self.config, "event_form_secret", None)
        if not secret:
            log.info("Event intake web server not started (EVENT_FORM_SECRET unset).")
            return
        port = int(os.environ.get("PORT") or os.environ.get("EVENT_INTAKE_PORT") or 8080)
        path = self._s("event_intake_path") or "/event-intake"
        app = web.Application()
        app.router.add_post(path, self._handle_intake)
        app.router.add_post("/event-intake-debug", self._handle_debug)
        app.router.add_get("/healthz", lambda r: web.json_response({"ok": True, "build": BUILD}))
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=port)
        try:
            await site.start()
            log.info("Event intake listening on :%d%s", port, path)
        except OSError:
            log.exception("Could not bind event intake server on port %d", port)
            self._runner = None

    def _verify(self, raw: bytes, headers) -> bool:
        secret = getattr(self.config, "event_form_secret", None)
        if not secret:
            return False
        sig = headers.get("X-Signature", "")
        ts = headers.get("X-Timestamp", "")
        if not sig or not ts:
            return False
        try:
            if abs(time.time() - int(ts)) > SIG_TOLERANCE:
                return False
        except ValueError:
            return False
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        provided = sig.split("=", 1)[-1].strip()  # tolerate "sha256=<hex>"
        return hmac.compare_digest(mac, provided)

    async def _handle_intake(self, request: web.Request) -> web.Response:
        raw = await request.read()
        if not self._verify(raw, request.headers):
            log.warning("Rejected event intake POST (bad/missing signature).")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if not self._s("event_intake_enabled"):
            return web.json_response({"ok": False, "error": "disabled"}, status=503)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        sid = str(data.get("submission_id") or "").strip()
        sid = re.sub(r"[^A-Za-z0-9_\-]", "", sid)[:80]
        if not sid:
            return web.json_response({"ok": False, "error": "missing submission_id"}, status=400)

        apps = self.store.get(APPS, {})
        if sid in apps:
            return web.json_response({"ok": True, "thread_id": apps[sid].get("thread_id"), "dedup": True})

        await self.bot.wait_until_ready()
        try:
            thread_id = await self._post_application(sid, data)
        except Exception:
            log.exception("Failed to post event application %s", sid)
            return web.json_response({"ok": False, "error": "post failed"}, status=500)
        return web.json_response({"ok": True, "thread_id": thread_id})

    async def _handle_debug(self, request: web.Request) -> web.Response:
        """TEMPORARY signed debug hook for diagnosing Indico auto-create. Uses
        the bot's own Indico credentials (server-side, never exposed). Remove
        once auto-create is confirmed working.

        Body: {"action": "form"|"rawpost", "category_id": int,
               "fields": [[k, v], ...]}  (fields only for rawpost)
        """
        raw = await request.read()
        if not self._verify(raw, request.headers):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        user = getattr(self.config, "indico_username", None)
        pw = getattr(self.config, "indico_password", None)
        if not user or not pw:
            return web.json_response({"ok": False, "error": "no indico creds"}, status=503)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        from integrations.indico_create import IndicoEventCreator
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        cat = int(data.get("category_id", self._s("indico_category_id") or 0))
        creator = IndicoEventCreator(base, user, pw)
        action = data.get("action", "form")
        try:
            if action == "form":
                out = await creator.debug_fetch_form(cat)
            elif action == "get":
                out = await creator.debug_get(str(data.get("path", "/")))
            elif action == "post":
                fields = [(str(k), str(v)) for k, v in data.get("fields", [])]
                out = await creator.debug_post(str(data.get("path", "/")), fields)
            elif action == "createfull":
                d = await creator.create_meeting(
                    category_id=cat,
                    title=data.get("title", "[TEST] Full create"),
                    start=data.get("start", "2026-07-25 13:00"),
                    end=data.get("end", "2026-07-25 16:00"),
                    unlisted=bool(data.get("unlisted", True)),
                    event_type=data.get("event_type", "conference"),
                    description_html=data.get("description_html"),
                    location=data.get("location"),
                    contacts=data.get("contacts"),
                )
                out = {"url": d.url, "event_id": d.event_id}
            elif action == "rawpost":
                fields = [(str(k), str(v)) for k, v in data.get("fields", [])]
                out = await creator.debug_raw_create(cat, fields)
            else:
                out = {"error": f"unknown action {action!r}"}
            return web.json_response({"ok": True, **out})
        except Exception as e:
            log.exception("debug hook failed")
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)

    # ---- posting the application -----------------------------------------

    def _forum(self) -> discord.ForumChannel | None:
        ch = self.bot.get_channel(int(self._s("event_post_channel_id") or 0))
        return ch if isinstance(ch, discord.ForumChannel) else None

    def _tag(self, forum: discord.ForumChannel, name: str) -> discord.ForumTag | None:
        return next((t for t in forum.available_tags if t.name.lower() == (name or "").lower()), None)

    @staticmethod
    def _map_fields(answers: list[dict]) -> dict:
        return {
            "title": _find(answers, "title") or "Untitled event",
            "host": _find(answers, "host"),
            "start": _find(answers, "eventstarttime", "starttime"),
            "end": _find(answers, "eventendtime", "endtime"),
            "repeats": _find(answers, "repeats"),
            "loc_name": _find(answers, "nameoflocation"),
            "loc_addr": _find(answers, "addressoflocation"),
            "loc_link": _find(answers, "linktolocation"),
            "description": _find(answers, "description"),
            "contact": _find(answers, "contactinformation") or _find(answers, "emailaddress"),
            "accent": _find(answers, "accentcolor"),
            "cost": _find(answers, "cost"),
            "minors": _find(answers, "minorsallowed"),
            "fursuit": _find(answers, "fursuitfriendly"),
            "public": _find(answers, "publicorprivate"),
        }

    async def _post_application(self, sid: str, data: dict) -> int:
        forum = self._forum()
        if forum is None:
            raise RuntimeError("event_post_channel_id is not a forum channel")
        answers: list[dict] = data.get("answers") or []
        m = self._map_fields(answers)

        # Build the headline embed from the key event details.
        embed = discord.Embed(
            title=f"🎫 {m['title']}"[:256],
            description=(m["description"][:4000] or "_(no description provided)_"),
            color=discord.Color.orange(),
        )
        when = " → ".join(x for x in (m["start"], m["end"]) if x) or "—"
        loc = " · ".join(x for x in (m["loc_name"], m["loc_addr"]) if x) or "—"
        if m["loc_link"]:
            loc += f"\n{m['loc_link']}"
        for name, val in (
            ("Host", m["host"]), ("When", when), ("Repeats", m["repeats"]),
            ("Location", loc), ("Cost", m["cost"]), ("Minors allowed", m["minors"]),
            ("Fursuit friendly", m["fursuit"]), ("Public/Private", m["public"]),
            ("Contact", m["contact"]),
        ):
            if val and val != "—":
                embed.add_field(name=name, value=str(val)[:1024], inline=True)
        embed.set_footer(text=f"Application {sid} · review and Accept / Decline below")

        # Re-attach uploaded files (decode base64), within Discord's limits.
        files, skipped = self._decode_files(data.get("files") or [])

        role_id = int(self._s("event_team_role_id") or 0)
        ping_ok = bool(self._s("event_ping_enabled"))
        ping = f"<@&{role_id}> " if (role_id and ping_ok) else ""
        content = f"{ping}**New event application** from **{m['host'] or 'someone'}** — needs review."
        if skipped:
            content += "\n⚠️ Some files were too large/many to attach: " + ", ".join(skipped)

        pending_tag = self._tag(forum, self._s("event_tag_pending") or "Changes Required")
        created = await forum.create_thread(
            name=m["title"][:100] or f"Event application {sid}",
            content=content,
            embed=embed,
            files=files,
            applied_tags=[pending_tag] if pending_tag else discord.utils.MISSING,
            view=build_decision_view(sid),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        thread = created.thread

        # Post the FULL questionnaire so nothing is lost, chunked under 2000 chars.
        await self._dump_answers(thread, answers)

        record = {
            "submission_id": sid, "thread_id": thread.id, "status": "pending",
            "created": int(time.time()), "last_ping": 0,
            # Persist EVERY raw question/answer so the full form (not just the
            # mapped subset) can be carried into the Indico event on Accept.
            "answers": [{"q": str(a.get("q", "")).strip(), "a": str(a.get("a", "")).strip()}
                        for a in answers if str(a.get("q", "")).strip()],
            **m,
        }

        def _mut(store: dict) -> None:
            store.setdefault(APPS, {})[sid] = record
        await self.store.update(_mut)
        log.info("Posted event application %s as thread %s", sid, thread.id)
        return thread.id

    def _decode_files(self, raw_files: list[dict]) -> tuple[list[discord.File], list[str]]:
        files: list[discord.File] = []
        skipped: list[str] = []
        for f in raw_files:
            name = str(f.get("filename") or "attachment")
            b64 = f.get("b64") or ""
            if len(files) >= MAX_FILES:
                skipped.append(name)
                continue
            try:
                blob = base64.b64decode(b64)
            except Exception:
                skipped.append(name)
                continue
            if len(blob) > MAX_FILE_BYTES or not blob:
                skipped.append(name)
                continue
            files.append(discord.File(io.BytesIO(blob), filename=name[:120]))
        return files, skipped

    @staticmethod
    async def _dump_answers(thread: discord.Thread, answers: list[dict]) -> None:
        lines = ["**Full application**"]
        for a in answers:
            q = str(a.get("q", "")).strip()
            v = str(a.get("a", "")).strip()
            if q and v:
                lines.append(f"**{q}:** {v}")
        buf = ""
        for line in lines:
            line = line[:1900]
            if len(buf) + len(line) + 1 > 1900:
                try:
                    await thread.send(buf)
                except discord.HTTPException:
                    pass
                buf = ""
            buf += line + "\n"
        if buf.strip():
            try:
                await thread.send(buf)
            except discord.HTTPException:
                pass

    # ---- decisions -------------------------------------------------------

    def _can_decide(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        role_id = int(self._s("event_team_role_id") or 0)
        if role_id and any(r.id == role_id for r in getattr(member, "roles", [])):
            return True
        staff = int(getattr(self.config, "staff_role_id", 0) or 0)
        return bool(staff) and any(r.id == staff for r in getattr(member, "roles", []))

    async def on_decision(self, interaction: discord.Interaction, sid: str, act: str) -> None:
        app = self.store.get(APPS, {}).get(sid)
        if not app:
            await interaction.response.send_message("This application is no longer tracked.", ephemeral=True)
            return
        if not self._can_decide(interaction.user if isinstance(interaction.user, discord.Member) else None):
            await interaction.response.send_message("Only the Events Team can decide on applications.", ephemeral=True)
            return
        if app.get("status") != "pending":
            await interaction.response.send_message(
                f"This was already **{app.get('status')}**.", ephemeral=True
            )
            return
        if act == "decline":
            await interaction.response.send_modal(DeclineModal(self, sid))
            return
        # Accept
        await interaction.response.defer()
        await self._finish_accept(interaction, sid, app)

    async def _retag(self, thread: discord.Thread, remove: str, add: str) -> None:
        forum = thread.parent
        if not isinstance(forum, discord.ForumChannel):
            return
        keep = [t for t in thread.applied_tags if t.name.lower() != (remove or "").lower()]
        add_tag = self._tag(forum, add)
        if add_tag and add_tag.id not in {t.id for t in keep}:
            keep.append(add_tag)
        try:
            await thread.edit(applied_tags=keep[:5])
        except discord.HTTPException:
            log.info("Could not retag thread %s", thread.id)

    async def _finish_accept(self, interaction: discord.Interaction, sid: str, app: dict) -> None:
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        cat = self._s("indico_category_id") or 0
        create_link = f"{base}/category/{cat}/" if cat else f"{base}/"

        # ── map fields ────────────────────────────────────────────────────────
        title       = app.get("title") or "Event"
        when_start  = app.get("start", "")
        when_end    = app.get("end", "")
        when        = " – ".join(x for x in (when_start, when_end) if x) or "—"
        loc_name    = app.get("loc_name", "")
        loc_addr    = app.get("loc_addr", "")
        loc_link    = app.get("loc_link", "")
        loc_parts   = [x for x in (loc_name, loc_addr) if x]
        loc_val     = "\n".join(loc_parts) + (f"\n{loc_link}" if loc_link else "")
        host        = app.get("host", "")
        description = app.get("description", "")
        repeats     = app.get("repeats", "")
        cost        = app.get("cost", "")
        minors      = app.get("minors", "")
        fursuit     = app.get("fursuit", "")
        public_     = app.get("public", "")
        contact     = app.get("contact", "")
        accent      = app.get("accent", "")

        # ── preview embed styled like the Indico event-page layout ────────────
        embed = discord.Embed(
            title=title[:256],
            description=(description[:4000] if description else None),
            color=discord.Color.green(),
        )
        embed.add_field(name="📅 Date & Time", value=when, inline=True)
        if host:
            embed.add_field(name="👤 Organizer", value=host, inline=True)
        if public_:
            embed.add_field(name="🔓 Type", value=public_, inline=True)
        if loc_val.strip():
            embed.add_field(name="📍 Location", value=loc_val.strip()[:1024], inline=False)
        if repeats:
            embed.add_field(name="🔁 Recurring", value=repeats, inline=True)
        if cost:
            embed.add_field(name="💰 Cost", value=cost, inline=True)
        info_parts = []
        if minors:
            info_parts.append(f"Minors: {minors}")
        if fursuit:
            info_parts.append(f"Fursuits: {fursuit}")
        if info_parts:
            embed.add_field(name="ℹ️ Info", value=" · ".join(info_parts), inline=True)
        if contact:
            embed.add_field(name="📬 Contact", value=contact[:1024], inline=False)
        embed.set_footer(text=f"✅ Accepted · Ref: {sid}")

        # ── description body for Indico's rich-text editor ────────────────────
        # Real NYFurs events (FurFlix, Sayonara Summer, etc.) open with the
        # main description paragraph, then a details block at the end.
        extra_lines: list[str] = []
        if loc_parts:
            extra_lines.append(f"📍 Location: {', '.join(loc_parts)}")
        if loc_link:
            extra_lines.append(f"🗺️ {loc_link}")
        if cost:
            extra_lines.append(f"💰 Cost: {cost}")
        if repeats:
            extra_lines.append(f"🔁 Recurring: {repeats}")
        if minors:
            extra_lines.append(f"👶 Minors allowed: {minors}")
        if fursuit:
            extra_lines.append(f"🦊 Fursuit-friendly: {fursuit}")
        if public_:
            extra_lines.append(f"🔓 {public_}")
        if contact:
            extra_lines.append(f"📬 Contact: {contact}")
        desc_for_indico = (
            (description.rstrip() + "\n\n" + "\n".join(extra_lines)).lstrip()
            if extra_lines else description
        )

        # ── form-fields cheat-sheet (for the Indico create wizard) ────────────
        fields_block = (
            f"Type:       Conference\n"
            f"Title:      {title}\n"
            f"Start:      {when_start}\n"
            f"End:        {when_end}\n"
            f"Venue:      {loc_name}\n"
            f"Address:    {loc_addr}\n"
            f"Organizer:  {host}\n"
            f"Accent:     {accent}"
        )

        paste_msg = (
            f"✅ **Accepted!** Create the Indico event here (type = **Conference**):\n"
            f"<{create_link}>\n\n"
            f"**Indico form fields:**\n```\n{fields_block}\n```\n"
            f"**Description — paste into the rich-text editor:**\n"
            f"```\n{desc_for_indico[:1800]}\n```\n"
            f"_Drop the live event link here once it's up._"
        )

        thread = interaction.channel

        # Try to have Indico create the draft itself; fall back to the
        # copy-paste draft above if anything goes wrong (so staff aren't stuck).
        draft = None
        autocreate_err = ""
        if self._s("event_autocreate_enabled"):
            draft, autocreate_err = await self._try_autocreate(title, when_start, when_end, app)

        if draft is not None:
            success_msg = (
                f"✅ **Accepted — draft created on the site!**\n"
                f"📝 {draft.url}\n"
                f"It's **unlisted** (a private draft) until staff publish it. The "
                f"meeting form only takes title + date, so please add the rest in "
                f"Indico:\n```\n{desc_for_indico[:1700]}\n```"
            )
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(content=success_msg, embed=embed)
                except discord.HTTPException:
                    pass
        else:
            msg = paste_msg
            if self._s("event_autocreate_enabled") and autocreate_err:
                msg = (
                    f"⚠️ Couldn't auto-create the draft ({autocreate_err}). "
                    f"Here's the manual draft instead:\n\n" + paste_msg
                )
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(content=msg, embed=embed)
                except discord.HTTPException:
                    pass

        if isinstance(thread, discord.Thread):
            await self._retag(thread, self._s("event_tag_pending") or "Changes Required",
                              self._s("event_tag_accepted") or "Accepted")
        await self._record_decision(
            sid, "accepted", interaction.user.id,
            indico_event_id=(draft.event_id if draft else ""),
            indico_url=(draft.url if draft else ""),
        )
        try:
            await interaction.message.edit(view=None)
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.followup.send("Accepted — draft details posted in the thread. ^w^", ephemeral=True)

    async def _try_autocreate(self, title: str, start: str, end: str, app: dict | None = None):
        """Attempt a real Indico draft. Returns (IndicoDraft|None, error_str)."""
        user = getattr(self.config, "indico_username", None)
        pw = getattr(self.config, "indico_password", None)
        if not user or not pw:
            return None, "INDICO_USERNAME / INDICO_PASSWORD not set"
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        cat = int(self._s("indico_category_id") or 0)
        tz = self._s("event_autocreate_timezone") or "America/New_York"
        etype = (self._s("event_autocreate_type") or "conference").strip().lower()
        app = app or {}
        description_html = self._build_description_html(app)
        location = {"venue_name": app.get("loc_name", ""), "address": app.get("loc_addr", "")}
        emails = [e for e in re.split(r"[,\s]+", app.get("contact", "")) if "@" in e]
        contacts = {"title": "Contact", "emails": emails, "phones": []}
        creator = IndicoEventCreator(base, user, pw)
        try:
            draft = await creator.create_meeting(
                category_id=cat, title=title, start=start, end=end,
                timezone=tz, unlisted=True, event_type=etype,
                description_html=description_html, location=location, contacts=contacts,
            )
            log.info("Auto-created Indico draft %s (%s)", draft.event_id, draft.url)
            return draft, ""
        except IndicoCreateError as e:
            log.warning("Indico auto-create failed: %s", e)
            return None, str(e)
        except Exception as e:  # network, parsing, anything — never block staff
            log.exception("Unexpected Indico auto-create error")
            return None, f"unexpected error: {e}"

    @staticmethod
    def _build_description_html(app: dict) -> str:
        """Rich-text event description mirroring how NYFurs events present info:
        the applicant's blurb, then a details block (location, cost, contact…)."""
        import html as _html

        def esc(v) -> str:
            return _html.escape(str(v or "").strip())

        parts: list[str] = []
        desc = esc(app.get("description", "")).replace("\n", "<br>")
        if desc:
            parts.append(f"<p>{desc}</p>")

        when = " – ".join(x for x in (app.get("start", ""), app.get("end", "")) if x)
        loc_bits = [x for x in (app.get("loc_name", ""), app.get("loc_addr", "")) if x]
        loc = ", ".join(esc(x) for x in loc_bits)
        if app.get("loc_link"):
            link = f'<a href="{esc(app["loc_link"])}">map</a>'
            loc = f"{loc} ({link})" if loc else link

        rows = [
            ("📅 Date &amp; Time", esc(when)),
            ("🔁 Repeats", esc(app.get("repeats", ""))),
            ("📍 Location", loc),
            ("💰 Cost", esc(app.get("cost", ""))),
            ("👤 Host", esc(app.get("host", ""))),
            ("🔓 Public/Private", esc(app.get("public", ""))),
            ("👶 Minors allowed", esc(app.get("minors", ""))),
            ("🦊 Fursuit-friendly", esc(app.get("fursuit", ""))),
            ("📬 Contact", esc(app.get("contact", ""))),
        ]
        detail = "<br>".join(f"<strong>{label}:</strong> {val}" for label, val in rows if val)
        if detail:
            parts.append(f"<p>{detail}</p>")

        # Include EVERY question from the application form, so nothing the
        # applicant answered is lost — even questions the bot doesn't map to a
        # specific Indico field. (Skip the main description, already shown above.)
        answers = app.get("answers") or []
        extra = []
        for a in answers:
            q = esc(a.get("q", ""))
            v = esc(a.get("a", "")).replace("\n", "<br>")
            if not q or not v:
                continue
            if _norm(a.get("q", "")) in ("description",):
                continue
            extra.append(f"<strong>{q}:</strong> {v}")
        if extra:
            parts.append("<hr><p><strong>📋 Full application responses</strong></p>")
            parts.append("<p>" + "<br>".join(extra) + "</p>")
        return "\n".join(parts)

    async def finish_decline(self, interaction: discord.Interaction, sid: str, reason: str) -> None:
        app = self.store.get(APPS, {}).get(sid)
        if not app or app.get("status") != "pending":
            await interaction.response.send_message("This application is no longer pending.", ephemeral=True)
            return
        await interaction.response.defer()
        contact = app.get("contact") or "_(no contact captured — check the full application above)_"
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(
                    f"✖️ **Declined** by {interaction.user.mention}.\n"
                    f"**Reason:** {reason}\n"
                    f"**Reach out to the applicant:** {contact}\n"
                    "Please contact them to explain the decision."
                )
            except discord.HTTPException:
                pass
            await self._retag(thread, self._s("event_tag_pending") or "Changes Required",
                              self._s("event_tag_declined") or "Declined")
        await self._record_decision(sid, "declined", interaction.user.id)
        # Remove the buttons from the application's starter message.
        try:
            if isinstance(thread, discord.Thread):
                starter = thread.starter_message or await thread.fetch_message(thread.id)
                if starter:
                    await starter.edit(view=None)
        except discord.HTTPException:
            pass
        await interaction.followup.send("Declined — the applicant's contact and your reason are in the thread.", ephemeral=True)

    async def _record_decision(self, sid: str, status: str, by_id: int,
                               indico_event_id: str = "", indico_url: str = "") -> None:
        def _mut(store: dict) -> None:
            rec = store.get(APPS, {}).get(sid)
            if rec:
                rec["status"] = status
                rec["decided_by"] = by_id
                rec["decided_at"] = int(time.time())
                if indico_event_id:
                    rec["indico_event_id"] = indico_event_id
                    rec["indico_url"] = indico_url
                    rec["indico_live"] = False
                    rec["publish_last_ping"] = 0
        await self.store.update(_mut)

    # ---- escalation ------------------------------------------------------

    @tasks.loop(hours=1)
    async def escalation_loop(self) -> None:
        if not self._s("event_intake_enabled"):
            return
        apps = self.store.get(APPS, {})
        if not apps:
            return
        escalate = int(self._s("event_escalate_hours") or 72) * 3600
        repeat = int(self._s("event_escalate_repeat_hours") or 24) * 3600
        role_id = int(self._s("event_team_role_id") or 0)
        now = time.time()
        changed = False
        for sid, rec in list(apps.items()):
            if rec.get("status") != "pending":
                continue
            age = now - rec.get("created", now)
            if age < escalate:
                continue
            if now - rec.get("last_ping", 0) < repeat:
                continue
            thread = self.bot.get_channel(int(rec.get("thread_id") or 0))
            if not isinstance(thread, discord.Thread):
                continue
            ping_ok = bool(self._s("event_ping_enabled"))
            mention = f"<@&{role_id}> " if (role_id and ping_ok) else ""
            try:
                await thread.send(
                    f"{mention}⏰ This event application has been waiting **over "
                    f"{int(escalate // 3600)}h** with no decision. Please **Accept** or **Decline** it.",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                rec["last_ping"] = int(now)
                changed = True
            except discord.HTTPException:
                log.info("Could not escalate application %s", sid)
        # Publish check: after an event is accepted+created, make sure it actually
        # goes live (listed in the category). If it's still not live N hours after
        # acceptance, ping the Events Team daily until it is.
        if await self._publish_check_pass(apps, now):
            changed = True
        if changed:
            await self.store.set(APPS, apps)

    async def _publish_check_pass(self, apps: dict, now: float) -> bool:
        check_after = int(self._s("event_publish_check_hours") or 24) * 3600
        repeat = int(self._s("event_publish_ping_hours") or 24) * 3600
        role_id = int(self._s("event_team_role_id") or 0)
        ping_ok = bool(self._s("event_ping_enabled"))
        changed = False
        for sid, rec in list(apps.items()):
            if rec.get("status") != "accepted" or not rec.get("indico_event_id"):
                continue
            if rec.get("indico_live"):
                continue
            if now - rec.get("decided_at", now) < check_after:
                continue
            # Is it live now? (listed in the public category)
            if await self._indico_event_listed(rec["indico_event_id"]):
                rec["indico_live"] = True
                changed = True
                thread = self.bot.get_channel(int(rec.get("thread_id") or 0))
                if isinstance(thread, discord.Thread):
                    try:
                        await thread.send("🎉 This event is now **live** on events.nyfurs.org. Nice work!")
                    except discord.HTTPException:
                        pass
                continue
            if now - rec.get("publish_last_ping", 0) < repeat:
                continue
            thread = self.bot.get_channel(int(rec.get("thread_id") or 0))
            if not isinstance(thread, discord.Thread):
                continue
            mention = f"<@&{role_id}> " if (role_id and ping_ok) else ""
            url = rec.get("indico_url", "")
            try:
                await thread.send(
                    f"{mention}📣 This event was **approved over "
                    f"{int(check_after // 3600)}h ago but isn't live yet** on the site. "
                    f"Please finish setting it up and **publish it**: {url}",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                rec["publish_last_ping"] = int(now)
                changed = True
            except discord.HTTPException:
                log.info("Could not send publish reminder for %s", sid)
        return changed

    async def _indico_event_listed(self, event_id: str) -> bool:
        """True if the event is published/listed in the configured category."""
        token = getattr(self.config, "indico_api_token", None)
        if not token:
            return False
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        cat = int(self._s("indico_category_id") or 0)
        url = f"{base}/export/categ/{cat}.json"
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, params={"limit": "500"},
                                 headers={"Authorization": f"Bearer {token}"}) as r:
                    if r.status != 200:
                        return False
                    data = await r.json()
            results = data.get("results") or []
            eid = str(event_id)
            return any(str(ev.get("id")) == eid for ev in results)
        except Exception:
            log.info("publish-check: could not query Indico category export")
            return False

    @escalation_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- staff command ---------------------------------------------------

    group = app_commands.Group(name="eventintake", description="Event Application intake (staff).")

    @group.command(name="status", description="(Staff) Show event-intake status.")
    @is_staff()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        apps = self.store.get(APPS, {})
        pending = sum(1 for a in apps.values() if a.get("status") == "pending")
        secret_set = bool(getattr(self.config, "event_form_secret", None))
        ch = int(self._s("event_post_channel_id") or 0)
        forum_ok = isinstance(self._forum(), discord.ForumChannel)
        lines = [
            f"**Enabled:** {bool(self._s('event_intake_enabled'))}",
            f"**Webhook secret set:** {secret_set}  ·  **Server running:** {self._runner is not None}",
            f"**Post channel:** {f'<#{ch}>' if ch else '_not set_'} ({'forum ✅' if forum_ok else 'not a forum ⚠️'})",
            f"**Events Team role:** <@&{int(self._s('event_team_role_id') or 0)}>",
            f"**Role pings:** {'✅ on' if self._s('event_ping_enabled') else '🔇 off (test mode)'}",
            f"**Auto-create drafts:** {self._autocreate_status()}",
            f"**Applications:** {len(apps)} total · {pending} pending",
            f"**Escalation:** after {self._s('event_escalate_hours')}h, repeat every {self._s('event_escalate_repeat_hours')}h",
            f"**Intake path:** `{self._s('event_intake_path')}`",
            f"**Build:** `{BUILD}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Events intake command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsIntake(bot))
