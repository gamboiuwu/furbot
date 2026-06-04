"""/events — list upcoming NYFurs events from Indico (events.nyfurs.org).

Uses Indico's HTTP Export API (read-only). The API token is read from the
INDICO_API_TOKEN environment variable (a secret — never stored in the repo or
the settings store). The instance URL, category, and look-ahead window are
tunable via /config (indico_url, indico_category_id, events_days).
"""

from __future__ import annotations

import datetime
import html
import logging
import re
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("furbot.events")

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
        self.settings = bot.settings

    def _s(self, key: str):
        return self.settings.get(key)

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

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Events command error", exc_info=error)
        msg = "Something went wrong fetching events."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
