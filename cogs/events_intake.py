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

log = logging.getLogger("furbot.events_intake")

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
        app.router.add_get("/healthz", lambda r: web.json_response({"ok": True}))
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
        ping = f"<@&{role_id}> " if role_id else ""
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
            "created": int(time.time()), "last_ping": 0, **m,
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
        draft = (
            "✅ **Accepted — here's the Indico draft to create** (mark it as **Conference**):\n"
            f"```\n"
            f"Type:        Conference\n"
            f"Title:       {app.get('title','')}\n"
            f"Start:       {app.get('start','')}\n"
            f"End:         {app.get('end','')}\n"
            f"Location:    {(app.get('loc_name','') + ' — ' + app.get('loc_addr','')).strip(' —')}\n"
            f"Host:        {app.get('host','')}\n"
            f"Accent:      {app.get('accent','')}\n"
            f"Description:\n{app.get('description','')}\n"
            f"```\n"
            f"Create it here: {create_link}\n"
            f"(Once it's live, drop the event link in this thread.)"
        )
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(draft)
            except discord.HTTPException:
                pass
            await self._retag(thread, self._s("event_tag_pending") or "Changes Required",
                              self._s("event_tag_accepted") or "Accepted")
        await self._record_decision(sid, "accepted", interaction.user.id)
        try:
            await interaction.message.edit(view=None)  # remove the buttons
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.followup.send("Accepted — draft details posted in the thread. ^w^", ephemeral=True)

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

    async def _record_decision(self, sid: str, status: str, by_id: int) -> None:
        def _mut(store: dict) -> None:
            rec = store.get(APPS, {}).get(sid)
            if rec:
                rec["status"] = status
                rec["decided_by"] = by_id
                rec["decided_at"] = int(time.time())
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
            mention = f"<@&{role_id}> " if role_id else ""
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
        if changed:
            await self.store.set(APPS, apps)

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
            f"**Applications:** {len(apps)} total · {pending} pending",
            f"**Escalation:** after {self._s('event_escalate_hours')}h, repeat every {self._s('event_escalate_repeat_hours')}h",
            f"**Intake path:** `{self._s('event_intake_path')}`",
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
