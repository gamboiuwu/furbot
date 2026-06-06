"""Create an event draft on a self-hosted Indico (events.nyfurs.org).

Indico's HTTP **Export** API is read-only — there is no supported endpoint to
*create* an event (confirmed by the Indico community). The only way to make the
site create the event itself is to drive the web UI the way a browser does:
log in with a real account, grab the CSRF token, and POST the create-event form.

This module does exactly that, deliberately conservatively:

  * it logs in via the **local** username/password provider (`/login/indico/`),
  * creates the event as a **meeting** (a single-page event, like the existing
    NYFurs event pages),
  * marks it **unlisted** (`listing=false`) so it is a private draft until staff
    review and publish it — nothing goes public automatically,
  * and only sets the handful of fields the create form accepts (title, start,
    end, timezone). Description / location / hosts are added by staff afterwards
    (the meeting/conference create form doesn't take them anyway).

Everything is best-effort: any failure raises `IndicoCreateError`, and the
caller falls back to the copy-paste draft so staff are never blocked.

The login and create routes were taken from Indico's own source:
  * login:  GET/POST ``/login/<provider>/``           (auth.login)
  * create: GET/POST ``/event/create/<event_type>``   (events.create)
The datetime fields are parsed by ``dateutil`` on Indico's side, and submitted
as a (date, time) pair under the same field name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp

try:  # dateutil ships with discord.py's deps, but guard anyway.
    from dateutil import parser as _dateparser
except Exception:  # pragma: no cover
    _dateparser = None

log = logging.getLogger("furbot.indico_create")

# Pull the CSRF token out of a rendered Indico page. Indico renders it as a
# <meta name="csrf-token" content="..."> tag and/or a hidden <input
# name="csrf_token" value="...">. Indico ALSO minifies its HTML and strips the
# quotes around simple attribute values (e.g. `value=de1d9858-...`), so every
# attribute matcher must tolerate quoted *and* unquoted values, in any order.
_META_CSRF_TAG_RE = re.compile(
    r"""<meta\b[^>]*\bname=["']?csrf-token["']?[^>]*>""", re.IGNORECASE
)
_CSRF_INPUT_TAG_RE = re.compile(
    r"""<input\b[^>]*\bname=["']?csrf_token["']?[^>]*>""", re.IGNORECASE
)
# value="x" | value='x' | value=x  (unquoted runs until whitespace or '>')
_VALUE_ATTR_RE = re.compile(
    r"""\bvalue=(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", re.IGNORECASE
)
_CONTENT_ATTR_RE = re.compile(
    r"""\bcontent=(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", re.IGNORECASE
)
# The create form's JSON response carries the new event's management URL.
_REDIRECT_RE = re.compile(r'"redirect"\s*:\s*"([^"]+)"')
# WTForms/Indico render per-field validation errors in elements whose class
# contains "error" — e.g. <span class="form-field-error">This field is required.</span>
_ERROR_TAG_RE = re.compile(
    r"""<(?:span|div|p)[^>]*class=["']?[^"'>]*error[^"'>]*["']?[^>]*>(.*?)</(?:span|div|p)>""",
    re.IGNORECASE | re.DOTALL,
)
_TAGS_RE = re.compile(r"<[^>]+>")


def _form_errors(body: str) -> str:
    """Pull human-readable validation errors out of Indico's re-rendered form.

    The create endpoint returns JSON like {"html": "<form>...</form>"} with the
    HTML JSON-escaped; un-escape it, then collect text from error elements.
    """
    html = body.replace('\\"', '"').replace("\\/", "/").replace("\\n", " ").replace("\\t", " ")
    seen: list[str] = []
    for m in _ERROR_TAG_RE.findall(html):
        text = _TAGS_RE.sub("", m).strip()
        text = re.sub(r"\s+", " ", text)
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen)[:400]


class IndicoCreateError(RuntimeError):
    """Raised when the draft could not be created for any reason."""


@dataclass
class IndicoDraft:
    url: str          # public/management URL of the new event
    event_id: str     # numeric id, best-effort


def _attr(match: re.Match | None) -> str | None:
    """First non-empty group from a quoted/unquoted attribute match."""
    if not match:
        return None
    return next((g for g in match.groups() if g), None)


def _extract_csrf(html: str) -> str | None:
    tag = _META_CSRF_TAG_RE.search(html)
    if tag:
        v = _attr(_CONTENT_ATTR_RE.search(tag.group(0)))
        if v:
            return v
    tag = _CSRF_INPUT_TAG_RE.search(html)
    if tag:
        v = _attr(_VALUE_ATTR_RE.search(tag.group(0)))
        if v:
            return v
    return None


def _split_dt(value: str, *, fallback: datetime | None = None) -> tuple[str, str]:
    """Parse free-text date/time into ('YYYY-MM-DD', 'HH:MM').

    Indico parses these with dateutil, so ISO is always safe. Raises
    IndicoCreateError if we can't get a confident date+time.
    """
    value = (value or "").strip()
    dt: datetime | None = None
    if value and _dateparser is not None:
        try:
            dt = _dateparser.parse(value, default=fallback)
        except (ValueError, OverflowError):
            dt = None
    if dt is None and fallback is not None:
        dt = fallback
    if dt is None:
        raise IndicoCreateError(f"could not understand date/time: {value!r}")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


class IndicoEventCreator:
    """One-shot client: open a session, log in, create one event, done."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password

    async def create_meeting(
        self,
        *,
        category_id: int,
        title: str,
        start: str,
        end: str,
        timezone: str = "America/New_York",
        unlisted: bool = True,
    ) -> IndicoDraft:
        # category_id 0 is the root category on this instance — a valid target.
        start_date, start_time = _split_dt(start)
        # If the end is blank/unparseable, default to two hours after the start.
        try:
            end_date, end_time = _split_dt(end)
        except IndicoCreateError:
            base_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
            end_dt = base_dt + timedelta(hours=2)
            end_date, end_time = end_dt.strftime("%Y-%m-%d"), end_dt.strftime("%H:%M")

        headers = {
            "User-Agent": "FurBot/1.0 (+event-intake)",
            "Referer": self.base + "/",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=jar) as s:
            await self._login(s)
            return await self._post_create(
                s,
                category_id=category_id,
                title=title,
                start=(start_date, start_time),
                end=(end_date, end_time),
                timezone=timezone,
                unlisted=unlisted,
            )

    async def _login(self, s: aiohttp.ClientSession) -> None:
        # The local-login form lives on /login/ itself (not /login/<provider>/,
        # which only handles external/OAuth providers and 404s on GET). Its
        # fields are: identifier (username/email), password, _provider=indico,
        # csrf_token.
        login_url = f"{self.base}/login/"
        async with s.get(login_url) as r:
            html = await r.text()
            if r.status >= 400:
                raise IndicoCreateError(f"login page HTTP {r.status}")
        csrf = _extract_csrf(html)
        data = {
            "identifier": self.username,
            "password": self.password,
            "_provider": "indico",
        }
        if csrf:
            data["csrf_token"] = csrf
        post_headers = {"X-CSRF-Token": csrf} if csrf else {}
        async with s.post(login_url, data=data, headers=post_headers, allow_redirects=True) as r:
            final = str(r.url)
            if r.status >= 400:
                raise IndicoCreateError(f"login POST HTTP {r.status}")
            # A successful login redirects away from /login/; a failed one
            # re-renders the login form (final URL still under /login).
            if "/login" in final.rsplit(self.base, 1)[-1]:
                raise IndicoCreateError(
                    "Indico login failed — check INDICO_USERNAME / INDICO_PASSWORD."
                )

    async def _session_csrf(self, s: aiohttp.ClientSession) -> str:
        """Fresh, session-bound CSRF token from an authenticated full page.

        The token rotates on login, so it must be read AFTER logging in. Every
        full Indico page embeds it in a <meta name="csrf-token"> tag.
        """
        for path in ("/", "/user/dashboard/"):
            async with s.get(f"{self.base}{path}") as r:
                if r.status >= 400:
                    continue
                tok = _extract_csrf(await r.text())
                if tok:
                    return tok
        raise IndicoCreateError("couldn't obtain a CSRF token after login")

    async def _post_create(
        self,
        s: aiohttp.ClientSession,
        *,
        category_id: int,
        title: str,
        start: tuple[str, str],
        end: tuple[str, str],
        timezone: str,
        unlisted: bool,
    ) -> IndicoDraft:
        # The category is read from the `category_id` query arg (RHCreateEvent),
        # and every form field carries WTForms' `event-creation-` prefix.
        create_url = f"{self.base}/event/create/meeting?category_id={category_id}"
        csrf = await self._session_csrf(s)
        P = "event-creation-"

        # IndicoDateTimeField reads a (date, time) pair submitted under the same
        # name, so we pass each datetime field twice.
        form: list[tuple[str, str]] = [
            ("csrf_token", csrf),
            (P + "category", str(category_id)),
            (P + "title", title[:1000] or "Untitled event"),
            (P + "timezone", timezone),
            (P + "start_dt", start[0]), (P + "start_dt", start[1]),
            (P + "end_dt", end[0]), (P + "end_dt", end[1]),
            (P + "protection_mode", "inheriting"),
        ]
        # `listing` is a boolean toggle: present/truthy -> listed in the
        # category; omitted -> unlisted private draft.
        if not unlisted:
            form.append((P + "listing", "on"))

        post_headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        async with s.post(create_url, data=form, headers=post_headers, allow_redirects=False) as r:
            body = await r.text()
            loc = r.headers.get("Location", "")
            if r.status == 403:
                raise IndicoCreateError(
                    "Indico refused the create (403) — the account lacks "
                    "event-creation rights in this category."
                )
            if r.status in (301, 302, 303, 307) and loc:
                return self._draft_from(loc)
            if r.status >= 400:
                raise IndicoCreateError(f"create POST HTTP {r.status}: {body[:300]}")
            # Indico answers the AJAX create with JSON: a redirect on success, or
            # the re-rendered form (with field errors) on validation failure.
            m = _REDIRECT_RE.search(body)
            if m:
                return self._draft_from(m.group(1).replace("\\/", "/"))
            errors = _form_errors(body)
            detail = f" Indico said: {errors}" if errors else f" First 300 chars: {body[:300]}"
            raise IndicoCreateError("Indico rejected the event form." + detail)

    # ---- temporary debug helpers (remove once auto-create works) ----------

    def _debug_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "FurBot/1.0 (+event-intake)", "Referer": self.base + "/"},
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

    async def debug_get(self, path: str) -> dict:
        """Log in and GET an arbitrary path; return status + body (diagnostics)."""
        async with self._debug_session() as s:
            await self._login(s)
            async with s.get(f"{self.base}{path}",
                             headers={"X-Requested-With": "XMLHttpRequest",
                                      "Accept": "application/json, text/html, */*"}) as r:
                body = await r.text()
        return {"status": r.status, "body": body[:8000]}

    async def debug_fetch_form(self, category_id: int) -> dict:
        """Log in and return the real authenticated create-form HTML + field names."""
        async with self._debug_session() as s:
            await self._login(s)
            csrf = await self._session_csrf(s)
            url = f"{self.base}/event/create/meeting?category_id={category_id}"
            async with s.get(url, headers={"X-Requested-With": "XMLHttpRequest"}) as r:
                body = await r.text()
        html = body.replace('\\"', '"').replace("\\/", "/").replace("\\n", "\n")
        names = sorted(set(re.findall(r'name=["\']?([A-Za-z0-9_\-]+)["\']?', html)))
        return {"status": r.status, "csrf_present": bool(csrf),
                "field_names": names, "html": html[:6000]}

    async def debug_raw_create(self, category_id: int, fields: list[tuple[str, str]]) -> dict:
        """Log in and POST arbitrary fields to the create URL; return raw result."""
        async with self._debug_session() as s:
            await self._login(s)
            csrf = await self._session_csrf(s)
            url = f"{self.base}/event/create/meeting?category_id={category_id}"
            data = [("csrf_token", csrf)] + list(fields)
            headers = {
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
            async with s.post(url, data=data, headers=headers, allow_redirects=False) as r:
                body = await r.text()
                loc = r.headers.get("Location", "")
        html = body.replace('\\"', '"').replace("\\/", "/").replace("\\n", "\n").replace("\\t", "\t")
        # Pull only markup that carries a real validation error, skipping the
        # room-booking widget's static Angular templates.
        real = []
        for m in re.finditer(r'class="[^"]*\b(?:has-error|form-field-error|i-form-field-error)\b[^"]*"[^>]*>(.{0,200}?)<', html):
            t = _TAGS_RE.sub("", m.group(1)).strip()
            if t:
                real.append(t)
        return {"status": r.status, "location": loc,
                "errors": _form_errors(body), "real_errors": real[:20],
                "title_roundtrip": "Auto-Create Probe" in html, "body_len": len(html),
                "body": html[:16000]}

    def _draft_from(self, location: str) -> IndicoDraft:
        if location.startswith("/"):
            location = self.base + location
        m = re.search(r"/event/(\d+)", location)
        event_id = m.group(1) if m else ""
        # Prefer the public event page over the deep management URL.
        url = f"{self.base}/event/{event_id}/" if event_id else location
        return IndicoDraft(url=url, event_id=event_id)
