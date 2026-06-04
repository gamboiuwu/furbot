"""/events — list upcoming NYFurs events from Indico (events.nyfurs.org).

Uses Indico's HTTP Export API (read-only). The API token is read from the
INDICO_API_TOKEN environment variable (a secret — never stored in the repo or
the settings store). The instance URL, category, and look-ahead window are
tunable via /config (indico_url, indico_category_id, events_days).
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import re
import time
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.events")

EVENTS_POSTED = "events_posted"      # {indico_event_id: {"se": scheduled_event_id, "url": event_url}}
EVENTS_REMINDED = "events_reminded"  # [scheduled_event_id] already reminded (24h before)
REG_PROFILES = "reg_profiles"        # {user_id: {email, name, updated_at, registrations: {eid: {...}}}}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegistrationModal(discord.ui.Modal):
    """Collects (and pre-fills from saved details) the info needed to register."""

    def __init__(self, cog: "Events", eid: str, event_name: str, url: str, profile: dict | None) -> None:
        super().__init__(title=("Register: " + event_name)[:45])  # Discord caps titles at 45 chars
        self.cog = cog
        self.eid = eid
        self.event_name = event_name
        self.url = url
        prof = profile or {}
        self.email = discord.ui.TextInput(
            label="Email", placeholder="you@example.com",
            default=prof.get("email"), required=True, max_length=200,
        )
        self.full_name = discord.ui.TextInput(
            label="Full name", default=prof.get("name"), required=True, max_length=200,
        )
        self.add_item(self.email)
        self.add_item(self.full_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        email = self.email.value.strip()
        name = self.full_name.value.strip()
        if not _EMAIL_RE.match(email):
            await interaction.response.send_message(
                "That email doesn't look right — tap the button and try again.", ephemeral=True
            )
            return
        await self.cog._save_registration(interaction.user.id, email, name, self.eid, self.event_name, self.url)
        msg = await self.cog._submit_or_link(interaction.user.id, self.eid, self.event_name, self.url, email, name)
        await interaction.response.send_message(msg, ephemeral=True)


class RegisterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"reg:v1:(?P<eid>\d+)"):
    """Persistent 'Register' button shown on the Interested DM (survives restarts;
    the event id is encoded in the custom_id and looked up in the store)."""

    def __init__(self, eid: str) -> None:
        self.eid = str(eid)
        super().__init__(
            discord.ui.Button(
                label="Register / save my info", emoji="📝",
                style=discord.ButtonStyle.primary, custom_id=f"reg:v1:{eid}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["eid"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Events | None" = interaction.client.get_cog("Events")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.open_registration_modal(interaction, self.eid)


class ForgetView(discord.ui.View):
    def __init__(self, cog: "Events", user_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="Forget my saved info", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def forget(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        await self.cog._forget_profile(self.user_id)
        button.disabled = True
        await interaction.response.edit_message(content="🗑️ Cleared your saved registration details.", view=self)

_IMAGE_KEYS = ("logo_url", "logoURL", "logo", "cover_url", "image_url")
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def _strip_html(text: str | None, limit: int = 240) -> str:
    if not text:
        return ""
    clean = html.unescape(_TAG_RE.sub(" ", text))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _event_epoch(date_obj: dict | None) -> int | None:
    """Indico dates look like {"date": "2025-06-10", "time": "18:00:00", "tz": "..."}."""
    if not isinstance(date_obj, dict) or not date_obj.get("date"):
        return None
    try:
        dt = datetime.datetime.fromisoformat(f"{date_obj['date']}T{date_obj.get('time', '00:00:00')}")
        tz = date_obj.get("tz")
        if tz:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _abs_url(url: str, base: str) -> str:
    url = url.strip()
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return base + "/" + url


def _event_image(event: dict, base: str) -> str | None:
    """Find an image that lives on the event: a logo/cover field, or the first
    image embedded in the event's description. Relative URLs are made absolute."""
    for k in _IMAGE_KEYS:
        v = event.get(k)
        if isinstance(v, str) and v.strip():
            return _abs_url(v, base)
    m = _IMG_SRC_RE.search(event.get("description") or "")
    if m:
        return _abs_url(m.group(1), base)
    return None


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings
        self._interest_dm: dict[tuple, float] = {}  # (user_id, se_id, kind) -> last DM epoch

    async def cog_load(self) -> None:
        self.sync_loop.start()
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.sync_loop.cancel()
        self.reminder_loop.cancel()

    def _s(self, key: str):
        return self.settings.get(key)

    def _guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

    def _base(self) -> str:
        return (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")

    async def _fetch_events(self) -> list[dict]:
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        cat = self._s("indico_category_id") or 0
        days = self._s("events_days") or 30
        url = f"{base}/export/categ/{cat}.json"
        params = {
            "from": "today",
            "to": f"{days}d",
            "detail": "events",
            "order": "start",
            "limit": "30",
            "tz": "America/New_York",
        }
        headers = {
            "Authorization": f"Bearer {self.config.indico_api_token}",
            "Accept": "application/json",
            "User-Agent": "FurBot/1.0 (+https://nyfurs.org)",
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"Indico returned HTTP {resp.status}: {body}")
                data = await resp.json(content_type=None)
        results = data.get("results") if isinstance(data, dict) else None
        return results or []

    def _build_embeds(self, events: list[dict]) -> list[discord.Embed]:
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        embeds: list[discord.Embed] = []
        for ev in events[:10]:  # Discord allows up to 10 embeds per message
            title = ev.get("title") or "Untitled event"
            ev_url = ev.get("url") or (f"{base}/event/{ev.get('id')}/" if ev.get("id") else None)
            embed = discord.Embed(title=title[:256], url=ev_url, color=discord.Color.blurple())

            epoch = _event_epoch(ev.get("startDate"))
            when = f"<t:{epoch}:F> (<t:{epoch}:R>)" if epoch else "Date TBA"
            embed.add_field(name="🗓️ When", value=when, inline=False)

            location = ev.get("location") or ev.get("room")
            if location:
                embed.add_field(name="📍 Where", value=str(location)[:200], inline=False)

            desc = _strip_html(ev.get("description"))
            if desc:
                embed.description = desc

            img = _event_image(ev, base)
            if img:
                embed.set_thumbnail(url=img)  # shows on the side of the embed
            embeds.append(embed)
        return embeds

    @app_commands.command(name="events", description="Show NYFurs events coming up in the next 30 days.")
    async def events(self, interaction: discord.Interaction) -> None:
        if not self.config.indico_api_token:
            await interaction.response.send_message(
                "⚠️ The events feed isn't set up yet — an admin needs to set the `INDICO_API_TOKEN` "
                "environment variable.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            events = await self._fetch_events()
        except Exception as exc:  # network / API / parse errors
            log.exception("Failed to fetch Indico events")
            await interaction.followup.send(
                f"Sorry, I couldn't reach the events calendar right now. ({type(exc).__name__})",
                ephemeral=True,
            )
            return

        days = self._s("events_days") or 30
        base = (self._s("indico_url") or "https://events.nyfurs.org").rstrip("/")
        if not events:
            await interaction.followup.send(
                f"📅 No events scheduled in the next **{days} days**. Check back soon, or see {base}/"
            )
            return

        embeds = self._build_embeds(events)
        extra = len(events) - len(embeds)
        content = f"📅 **Upcoming NYFurs events** (next {days} days)"
        if extra > 0:
            content += f" — showing {len(embeds)} of {len(events)}. More at {base}/"
        await interaction.followup.send(content=content, embeds=embeds)

    # ---- sync to Discord's Scheduled Events page ------------------------

    def _scheduled_description(self, ev: dict, url: str) -> str:
        parts = []
        desc = _strip_html(ev.get("description"), limit=500)
        if desc:
            parts.append(desc)
        parts.append(f"🔗 Details & registration: {url}")
        parts.append(
            "⚠️ Note: marking yourself as \"Interested\" here does **not** register you for the "
            "event. Please register at the link above."
        )
        return "\n\n".join(parts)[:1000]

    async def _fetch_event_detail(self, event_id) -> dict | None:
        """Fetch one event's full export record (its description usually has more
        HTML — and images — than the category summary feed)."""
        base = self._base()
        url = f"{base}/export/event/{event_id}.json"
        headers = {
            "Authorization": f"Bearer {self.config.indico_api_token}",
            "Accept": "application/json",
            "User-Agent": "FurBot/1.0 (+https://nyfurs.org)",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params={"detail": "events"}, headers=headers) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            results = data.get("results") if isinstance(data, dict) else None
            return results[0] if results else None
        except Exception:
            log.debug("Could not fetch event detail for %s", event_id, exc_info=True)
            return None

    async def _resolve_image(self, ev: dict, base: str) -> str | None:
        """Find a banner image: a logo/cover field, the topmost image in the
        description (summary feed, then the fuller event detail), and finally the
        event's conventional Indico logo URL."""
        img = _event_image(ev, base)
        if img:
            return img
        eid = ev.get("id")
        if eid:
            full = await self._fetch_event_detail(eid)
            if full:
                img = _event_image(full, base)
                if img:
                    return img
            # Last resort: the event's logo endpoint (validated on download).
            return f"{base}/event/{eid}/logo"
        return None

    @staticmethod
    def _looks_like_image(data: bytes) -> bool:
        sig = data[:12]
        return (
            sig.startswith(b"\x89PNG")          # PNG
            or sig.startswith(b"\xff\xd8\xff")  # JPEG
            or sig[:4] in (b"GIF8",)            # GIF
            or (sig[:4] == b"RIFF" and sig[8:12] == b"WEBP")  # WEBP
        )

    async def _download_image(self, url: str | None) -> bytes | None:
        data, _ = await self._fetch_image(url)
        return data

    async def _fetch_image(self, url: str | None) -> tuple[bytes | None, str]:
        """Download + validate an image. Tries with the API token and without it
        (Indico's web/attachment layer often rejects the API token), following
        redirects. Returns (bytes_or_None, debug_string)."""
        if not url:
            return None, "no url"
        base = self._base()
        token = self.config.indico_api_token
        # Attempt with auth (for indico-hosted URLs) then without.
        variants: list[dict] = []
        if url.startswith(base) and token:
            variants.append({"Authorization": f"Bearer {token}"})
        variants.append({})
        last = "no attempt"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for hv in variants:
                    headers = {"User-Agent": "FurBot/1.0 (+https://nyfurs.org)", **hv}
                    try:
                        async with session.get(url, headers=headers, allow_redirects=True) as resp:
                            data = await resp.read()
                            ctype = resp.headers.get("Content-Type", "?")
                            auth = "auth" if hv else "noauth"
                            last = f"status={resp.status} type={ctype} bytes={len(data)} ({auth})"
                            if (resp.status == 200 and 0 < len(data) <= 8 * 1024 * 1024
                                    and self._looks_like_image(data)):
                                return data, last
                    except Exception as exc:  # noqa: BLE001
                        last = f"error={type(exc).__name__} ({'auth' if hv else 'noauth'})"
        except Exception as exc:  # noqa: BLE001
            return None, f"error={type(exc).__name__}"
        return None, last

    async def _ensure_banner(self, se: discord.ScheduledEvent | None, ev: dict, base: str) -> bool:
        """If an existing scheduled event has no cover banner, try to add one."""
        if se is None:
            return False
        if getattr(se, "cover_image", None) is not None or getattr(se, "image", None) is not None:
            return False  # already has a banner
        image = await self._download_image(await self._resolve_image(ev, base))
        if not image:
            return False
        try:
            await se.edit(image=image, reason="Backfill event banner from events.nyfurs.org")
            return True
        except discord.HTTPException:
            log.exception("Failed to update banner for scheduled event %s", se.id)
            return False

    async def _sync_scheduled_events(self, guild: discord.Guild) -> tuple[int, int, int]:
        """Create scheduled events for upcoming Indico events, and backfill a
        banner on already-posted events that are missing one. Returns
        (created, updated, failed)."""
        events = await self._fetch_events()
        posted = dict(self.store.get(EVENTS_POSTED, {}))
        base = self._base()
        now = discord.utils.utcnow()

        try:
            existing = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            existing = list(guild.scheduled_events)
        by_name = {se.name.lower(): se.id for se in existing}
        by_id = {se.id: se for se in existing}

        created = updated = failed = 0
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid:
                continue
            name = (ev.get("title") or "NYFurs Event")[:100]
            url = ev.get("url") or f"{base}/event/{eid}/"
            if eid in posted:  # already posted — make sure it has a banner
                info = posted[eid]
                sid = info.get("se") if isinstance(info, dict) else info
                if await self._ensure_banner(by_id.get(sid), ev, base):
                    updated += 1
                continue
            if name.lower() in by_name:  # someone already added it manually
                posted[eid] = {"se": by_name[name.lower()], "url": url, "name": name}
                if await self._ensure_banner(by_id.get(by_name[name.lower()]), ev, base):
                    updated += 1
                continue
            start_epoch = _event_epoch(ev.get("startDate"))
            if not start_epoch:
                continue
            start = datetime.datetime.fromtimestamp(start_epoch, tz=datetime.timezone.utc)
            if start <= now:
                continue  # Discord only allows scheduling future events
            end_epoch = _event_epoch(ev.get("endDate"))
            end = (datetime.datetime.fromtimestamp(end_epoch, tz=datetime.timezone.utc)
                   if end_epoch and end_epoch > start_epoch else start + datetime.timedelta(hours=2))
            location = (ev.get("location") or "").strip()[:100] or "See registration link"
            kwargs = dict(
                name=name,
                description=self._scheduled_description(ev, url),
                start_time=start,
                end_time=end,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=location,
                reason="Synced from events.nyfurs.org",
            )
            image = await self._download_image(await self._resolve_image(ev, base))
            if image:
                kwargs["image"] = image
            try:
                se = await guild.create_scheduled_event(**kwargs)
                posted[eid] = {"se": se.id, "url": url, "name": name}
                created += 1
            except discord.Forbidden:
                log.warning("Missing Manage Events permission — cannot create scheduled events")
                failed += 1
                break
            except discord.HTTPException:
                log.exception("Failed to create scheduled event for Indico event %s", eid)
                failed += 1

        await self.store.set(EVENTS_POSTED, posted)
        return created, updated, failed

    @tasks.loop(hours=6)
    async def sync_loop(self) -> None:
        interval = max(1, self._s("events_sync_interval_hours") or 6)
        if self.sync_loop.hours != interval:
            self.sync_loop.change_interval(hours=interval)
        if not self._s("events_sync_enabled") or not self.config.indico_api_token:
            return
        guild = self._guild()
        if guild is None:
            return
        try:
            created, updated, failed = await self._sync_scheduled_events(guild)
            if created or updated:
                log.info("Indico sync: %d new event(s), %d banner(s) backfilled", created, updated)
        except Exception:
            log.exception("Scheduled-events sync failed")

    @sync_loop.before_loop
    async def _before_sync(self) -> None:
        await self.bot.wait_until_ready()

    # ---- interest DMs + 24h reminders -----------------------------------

    def _our_se_ids(self) -> set[int]:
        ids = set()
        for info in self.store.get(EVENTS_POSTED, {}).values():
            ids.add(info.get("se") if isinstance(info, dict) else info)
        return {i for i in ids if i}

    def _event_url_for(self, se_id: int) -> str | None:
        base = self._base()
        for eid, info in self.store.get(EVENTS_POSTED, {}).items():
            sid = info.get("se") if isinstance(info, dict) else info
            if sid == se_id:
                if isinstance(info, dict) and info.get("url"):
                    return info["url"]
                return f"{base}/event/{eid}/"
        return None

    def _indico_id_for(self, se_id: int) -> str | None:
        for eid, info in self.store.get(EVENTS_POSTED, {}).items():
            sid = info.get("se") if isinstance(info, dict) else info
            if sid == se_id:
                return str(eid)
        return None

    # ---- registration details (remembered per user) --------------------

    def _get_profile(self, user_id: int) -> dict | None:
        return self.store.get(REG_PROFILES, {}).get(str(user_id))

    async def open_registration_modal(self, interaction: discord.Interaction, eid: str) -> None:
        info = self.store.get(EVENTS_POSTED, {}).get(str(eid))
        name = (info.get("name") if isinstance(info, dict) else None) or "this event"
        url = (info.get("url") if isinstance(info, dict) else None) or f"{self._base()}/event/{eid}/"
        try:
            await interaction.response.send_modal(
                RegistrationModal(self, str(eid), name, url, self._get_profile(interaction.user.id))
            )
        except discord.HTTPException:
            log.exception("Failed to open registration modal for event %s", eid)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Couldn't open the registration form — please try again in a moment.", ephemeral=True
                )

    async def _save_registration(self, user_id: int, email: str, name: str,
                                 eid: str, event_name: str, url: str) -> None:
        def mut(d: dict) -> None:
            prof = d.setdefault(REG_PROFILES, {}).setdefault(str(user_id), {})
            prof["email"] = email
            prof["name"] = name
            prof["updated_at"] = int(time.time())
            regs = prof.setdefault("registrations", {})
            regs[str(eid)] = {"event": event_name, "url": url, "at": int(time.time())}

        await self.store.update(mut)

    async def _forget_profile(self, user_id: int) -> None:
        await self.store.update(lambda d: d.get(REG_PROFILES, {}).pop(str(user_id), None))

    async def _submit_or_link(self, user_id: int, eid: str, event_name: str,
                              url: str, email: str, name: str) -> str:
        """Submit the registration to Indico if the API is verified+enabled;
        otherwise save the details and hand back the link to finish on the site."""
        if self._s("events_register_submit_enabled"):
            ok, detail = await self._submit_to_indico(eid, email, name)
            if ok:
                return f"✅ You're registered for **{event_name}**! I've saved your details for next time. 🐾"
            return (
                f"⚠️ I saved your details, but couldn't auto-complete the registration ({detail}). "
                f"Please finish it here: {url}"
            )
        return (
            f"✅ Saved your details for **{event_name}** (I'll remember them next time).\n"
            f"To finish registering, complete it here — your info is ready to paste in:\n🔗 {url}"
        )

    async def _submit_to_indico(self, eid: str, email: str, name: str) -> tuple[bool, str]:
        """Placeholder for the real Indico registration POST. Disabled until the
        registration endpoint is verified against the live instance (see notes).
        Returns (success, detail)."""
        # TODO: wire the verified Indico registration endpoint here once confirmed.
        return False, "registration API not configured yet"

    @app_commands.command(name="myregistration", description="View or clear the registration details FurBot saved for you.")
    async def myregistration(self, interaction: discord.Interaction) -> None:
        prof = self._get_profile(interaction.user.id)
        if not prof:
            await interaction.response.send_message(
                "I don't have any saved registration details for you yet. Mark *Interested* on an "
                "event and tap **Register** to set them up.",
                ephemeral=True,
            )
            return
        lines = [f"**Email:** {prof.get('email', '—')}", f"**Name:** {prof.get('name', '—')}"]
        regs = prof.get("registrations", {})
        if regs:
            lines.append("\n**Events you've registered through me:**")
            for r in list(regs.values())[:10]:
                lines.append(f"• {r.get('event', 'event')}")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, view=ForgetView(self, interaction.user.id)
        )

    def _interest_cooldown_ok(self, user_id: int, se_id: int, kind: str, hours: int = 6) -> bool:
        key = (user_id, se_id, kind)
        now = time.time()
        if now - self._interest_dm.get(key, 0) < hours * 3600:
            return False
        self._interest_dm[key] = now
        return True

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        if user.bot or not self._s("events_interest_dm_enabled"):
            return
        url = self._event_url_for(event.id)
        if url is None or not self._interest_cooldown_ok(user.id, event.id, "add"):
            return
        starts = f"<t:{int(event.start_time.timestamp())}:R>" if event.start_time else "soon"
        text = (
            f"👋 Thanks for your interest in **{event.name}**!\n\n"
            "Marking *Interested* here doesn't sign you up. Tap **Register** below and I'll take your "
            "details (and remember them for next time), or register directly on the event page:\n"
            f"🔗 {url}\n\n"
            f"It starts {starts}. Hope to see you there! 🐾"
        )
        view = discord.utils.MISSING
        if self._s("events_dm_register_enabled"):
            eid = self._indico_id_for(event.id) or ""
            if eid.isdigit():
                v = discord.ui.View(timeout=None)
                v.add_item(RegisterButton(eid))
                view = v
        try:
            await user.send(text, view=view)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        if user.bot or not self._s("events_interest_dm_enabled"):
            return
        url = self._event_url_for(event.id)
        if url is None or not self._interest_cooldown_ok(user.id, event.id, "remove"):
            return
        try:
            await user.send(
                f"Got it — you're no longer marked interested in **{event.name}**. "
                f"If you change your mind, you can still register anytime here: {url}"
            )
        except discord.HTTPException:
            pass

    @tasks.loop(minutes=30)
    async def reminder_loop(self) -> None:
        if not self._s("events_reminder_enabled"):
            return
        guild = self._guild()
        if guild is None:
            return
        hours = self._s("events_reminder_hours") or 24
        now = discord.utils.utcnow()
        our = self._our_se_ids()
        reminded = set(self.store.get(EVENTS_REMINDED, []))
        try:
            sched = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            sched = list(guild.scheduled_events)
        changed = False
        for se in sched:
            if se.id not in our or se.id in reminded or not se.start_time:
                continue
            delta = (se.start_time - now).total_seconds()
            if 0 < delta <= hours * 3600:
                await self._remind_interested(se)
                reminded.add(se.id)
                changed = True
        # Keep the reminded list from growing forever — drop events we no longer track.
        pruned = {sid for sid in reminded if sid in our}
        if changed or pruned != reminded:
            await self.store.set(EVENTS_REMINDED, list(pruned))

    @reminder_loop.before_loop
    async def _before_reminder(self) -> None:
        await self.bot.wait_until_ready()

    async def _remind_interested(self, se: discord.ScheduledEvent) -> None:
        ts = int(se.start_time.timestamp())
        url = self._event_url_for(se.id)
        loc = f"\n📍 {se.location}" if se.location else ""
        link = f"\n🔗 Details & registration: {url}" if url else ""
        text = (
            f"⏰ **Reminder: {se.name} is coming up!**\n"
            f"It starts <t:{ts}:F> (<t:{ts}:R>).{loc}{link}\n\n"
            "If you haven't registered yet, please do so at the link above — marking *Interested* on "
            "Discord doesn't register you. See you there! 🐾"
        )
        count = 0
        try:
            async for user in se.users():
                if getattr(user, "bot", False):
                    continue
                try:
                    await user.send(text)
                except discord.HTTPException:
                    pass
                count += 1
                if count % 5 == 0:
                    await asyncio.sleep(1)  # gentle rate limiting
        except discord.HTTPException:
            log.exception("Failed to fetch interested users for event %s", se.id)

    @app_commands.command(name="syncevents", description="(Staff) Post upcoming events to the server's Events page.")
    @is_staff()
    async def syncevents(self, interaction: discord.Interaction) -> None:
        if not self.config.indico_api_token:
            await interaction.response.send_message(
                "⚠️ `INDICO_API_TOKEN` isn't set, so I can't fetch events.", ephemeral=True
            )
            return
        guild = interaction.guild or self._guild()
        if guild is None:
            await interaction.response.send_message("No server available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            created, updated, failed = await self._sync_scheduled_events(guild)
        except Exception as exc:
            log.exception("Manual event sync failed")
            await interaction.followup.send(f"Couldn't sync events. ({type(exc).__name__})", ephemeral=True)
            return
        bits = []
        if created:
            bits.append(f"posted **{created}** new event(s)")
        if updated:
            bits.append(f"added banners to **{updated}** existing event(s)")
        if failed:
            bits.append(f"**{failed}** failed (check my **Manage Events** permission)")
        msg = ("✅ " + ", ".join(bits) + ".") if bits else "Nothing to do — the Events page is already up to date."
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="eventsdebug", description="(Staff) Diagnose the events feed and banner sync.")
    @app_commands.describe(event_id="An Indico event id to inspect (e.g. 93).")
    @is_staff()
    async def eventsdebug(self, interaction: discord.Interaction, event_id: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        base = self._base()
        out = [
            f"**Base:** {base}",
            f"**Category:** `{self._s('indico_category_id')}` · **Days:** {self._s('events_days')}",
            f"**Token set:** {bool(self.config.indico_api_token)}",
        ]
        try:
            evs = await self._fetch_events()
            sample = ", ".join(f"{e.get('id')}:{(e.get('title') or '')[:18]}" for e in evs[:8])
            out.append(f"**Feed returned:** {len(evs)} event(s){' — ' + sample if sample else ''}")
        except Exception as exc:
            out.append(f"**Feed error:** {type(exc).__name__}: {exc}")
            evs = []

        if event_id:
            target = next((e for e in evs if str(e.get("id")) == str(event_id)), None)
            if target is None:
                target = await self._fetch_event_detail(event_id)
                out.append(f"Event {event_id} not in feed window — fetched detail directly: {'ok' if target else 'FAILED'}")
            if target:
                present = {k: target.get(k) for k in _IMAGE_KEYS if target.get(k)}
                m = _IMG_SRC_RE.search(target.get("description") or "")
                resolved = await self._resolve_image(target, base)
                data, dbg = await self._fetch_image(resolved)
                out += [
                    f"**Image fields:** {present or 'none'}",
                    f"**<img> in description:** {m.group(1) if m else 'none'}",
                    f"**Resolved image URL:** {resolved or 'none'}",
                    f"**Download:** {'OK ' + str(len(data)) + ' bytes' if data else 'FAILED'} — `{dbg}`",
                ]
        await interaction.followup.send("\n".join(out)[:1900], ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Events command error", exc_info=error)
            msg = "Something went wrong with that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
