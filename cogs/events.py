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
        """Find a banner image: logo/cover field or topmost image in the
        description. If none in the summary feed, fetch the full event detail
        and scan its description's topmost image too."""
        img = _event_image(ev, base)
        if img:
            return img
        eid = ev.get("id")
        if eid:
            full = await self._fetch_event_detail(eid)
            if full:
                return _event_image(full, base)
        return None

    async def _download_image(self, url: str | None) -> bytes | None:
        if not url:
            return None
        headers = {"User-Agent": "FurBot/1.0 (+https://nyfurs.org)"}
        if url.startswith(self._base()) and self.config.indico_api_token:
            headers["Authorization"] = f"Bearer {self.config.indico_api_token}"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200 or "image" not in resp.headers.get("Content-Type", ""):
                        return None
                    data = await resp.read()
                    return data if 0 < len(data) <= 8 * 1024 * 1024 else None
        except Exception:
            log.debug("Could not download event image %s", url, exc_info=True)
            return None

    async def _sync_scheduled_events(self, guild: discord.Guild) -> tuple[int, int]:
        """Create Discord scheduled events for upcoming Indico events not yet
        posted. Returns (created, failed). Deduped via the EVENTS_POSTED map and
        by matching existing event names."""
        events = await self._fetch_events()
        posted = dict(self.store.get(EVENTS_POSTED, {}))
        base = self._base()
        now = discord.utils.utcnow()

        try:
            existing = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            existing = list(guild.scheduled_events)
        by_name = {se.name.lower(): se.id for se in existing}

        created = failed = 0
        for ev in events:
            eid = str(ev.get("id") or "")
            if not eid or eid in posted:
                continue
            name = (ev.get("title") or "NYFurs Event")[:100]
            url = ev.get("url") or f"{base}/event/{eid}/"
            if name.lower() in by_name:  # someone already added it manually
                posted[eid] = {"se": by_name[name.lower()], "url": url}
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
                posted[eid] = {"se": se.id, "url": url}
                created += 1
            except discord.Forbidden:
                log.warning("Missing Manage Events permission — cannot create scheduled events")
                failed += 1
                break
            except discord.HTTPException:
                log.exception("Failed to create scheduled event for Indico event %s", eid)
                failed += 1

        await self.store.set(EVENTS_POSTED, posted)
        return created, failed

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
            created, failed = await self._sync_scheduled_events(guild)
            if created:
                log.info("Posted %d new scheduled event(s) from Indico", created)
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
            "Quick heads-up: marking *Interested* here doesn't sign you up. To actually register — "
            "and answer any registration questions the event may have — finish on the event page:\n"
            f"🔗 {url}\n\n"
            f"It starts {starts}. Hope to see you there! 🐾"
        )
        try:
            await user.send(text)
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
            created, failed = await self._sync_scheduled_events(guild)
        except Exception as exc:
            log.exception("Manual event sync failed")
            await interaction.followup.send(f"Couldn't sync events. ({type(exc).__name__})", ephemeral=True)
            return
        msg = f"✅ Posted **{created}** new event(s) to the server's Events page."
        if failed:
            msg += f" **{failed}** failed — check that I have the **Manage Events** permission."
        if not created and not failed:
            msg = "Nothing new to post — the Events page is already up to date."
        await interaction.followup.send(msg, ephemeral=True)

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
