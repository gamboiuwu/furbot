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
# name="csrf_token" value="...">. Match either, tolerating attribute order.
_CSRF_META_RE = re.compile(
    r"""<meta[^>]*\bname=["']csrf-token["'][^>]*\bcontent=["']([^"']+)["']""",
    re.IGNORECASE,
)
_CSRF_META_REV_RE = re.compile(
    r"""<meta[^>]*\bcontent=["']([^"']+)["'][^>]*\bname=["']csrf-token["']""",
    re.IGNORECASE,
)
# Any <input ... name="csrf_token" ...>, with value on either side of name.
_CSRF_INPUT_TAG_RE = re.compile(
    r"""<input\b[^>]*\bname=["']csrf_token["'][^>]*>""", re.IGNORECASE
)
_VALUE_ATTR_RE = re.compile(r"""\bvalue=["']([^"']+)["']""", re.IGNORECASE)
# The create form's JSON response carries the new event's management URL.
_REDIRECT_RE = re.compile(r'"redirect"\s*:\s*"([^"]+)"')


class IndicoCreateError(RuntimeError):
    """Raised when the draft could not be created for any reason."""


@dataclass
class IndicoDraft:
    url: str          # public/management URL of the new event
    event_id: str     # numeric id, best-effort


def _extract_csrf(html: str) -> str | None:
    m = _CSRF_META_RE.search(html) or _CSRF_META_REV_RE.search(html)
    if m:
        return m.group(1)
    tag = _CSRF_INPUT_TAG_RE.search(html)
    if tag:
        v = _VALUE_ATTR_RE.search(tag.group(0))
        if v:
            return v.group(1)
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
        if not category_id:
            raise IndicoCreateError(
                "no Indico category configured (set indico_category_id) — "
                "events can't be created at the root."
            )
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
        login_url = f"{self.base}/login/indico/"
        async with s.get(login_url) as r:
            html = await r.text()
            if r.status >= 400:
                raise IndicoCreateError(f"login page HTTP {r.status}")
        csrf = _extract_csrf(html)
        # Indico's local-login form posts username/password (+ csrf).
        data = {"username": self.username, "password": self.password}
        if csrf:
            data["csrf_token"] = csrf
        post_headers = {"X-CSRF-Token": csrf} if csrf else {}
        async with s.post(login_url, data=data, headers=post_headers, allow_redirects=True) as r:
            body = await r.text()
            # A successful login redirects away from /login/; a failed one
            # re-renders the form (often with an "invalid" message).
            failed = (
                r.status >= 400
                or "/login" in str(r.url)
                and ("invalid" in body.lower() or "incorrect" in body.lower()
                     or 'name="password"' in body.lower())
            )
            if failed:
                raise IndicoCreateError(
                    "Indico login failed — check INDICO_USERNAME / INDICO_PASSWORD."
                )

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
        create_url = f"{self.base}/event/create/meeting"
        # GET the create dialog to obtain a fresh, session-bound CSRF token.
        async with s.get(create_url) as r:
            page = await r.text()
            if r.status == 403:
                raise IndicoCreateError(
                    "this Indico account can't create events in that category "
                    "(403). Use an account with management rights."
                )
            if r.status >= 400:
                raise IndicoCreateError(f"create page HTTP {r.status}")
        csrf = _extract_csrf(page)
        if not csrf:
            raise IndicoCreateError("couldn't find a CSRF token on the create page")

        # IndicoDateTimeField reads a (date, time) pair submitted under the same
        # name, so we pass each field twice.
        form: list[tuple[str, str]] = [
            ("csrf_token", csrf),
            ("category_id", str(category_id)),
            ("category", str(category_id)),
            ("title", title[:1000] or "Untitled event"),
            ("timezone", timezone),
            ("start_dt", start[0]), ("start_dt", start[1]),
            ("end_dt", end[0]), ("end_dt", end[1]),
            ("protection_mode", "inheriting"),
            # Empty string is a WTForms "false" value -> unlisted private draft.
            ("listing", "" if unlisted else "true"),
        ]
        post_headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        async with s.post(create_url, data=form, headers=post_headers, allow_redirects=False) as r:
            body = await r.text()
            loc = r.headers.get("Location", "")
            if r.status in (301, 302, 303, 307) and loc:
                return self._draft_from(loc)
            if r.status >= 400:
                raise IndicoCreateError(f"create POST HTTP {r.status}: {body[:200]}")
            # Indico answers the AJAX create with JSON containing a redirect URL.
            m = _REDIRECT_RE.search(body)
            if m:
                return self._draft_from(m.group(1).replace("\\/", "/"))
            raise IndicoCreateError(
                "create POST returned no redirect — the form may have rejected "
                f"a field. First 200 chars: {body[:200]}"
            )

    def _draft_from(self, location: str) -> IndicoDraft:
        if location.startswith("/"):
            location = self.base + location
        m = re.search(r"/event/(\d+)", location)
        event_id = m.group(1) if m else ""
        # Prefer the public event page over the deep management URL.
        url = f"{self.base}/event/{event_id}/" if event_id else location
        return IndicoDraft(url=url, event_id=event_id)
