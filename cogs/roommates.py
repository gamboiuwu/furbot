"""Con Roommate & Carpool Finder, fluff edition.

Privately match NYFurs members who want to share a hotel room or a car ride to
a convention.

Design / safety (mirrors the in-app rules, see _rules_text and the hub message):
  * 18+ everywhere. Entry is gated behind the server's age-range roles (holding
    any one proves 18+). Extra age gates per the rules:
        - Host a hotel room: 18+
        - Host a carpool:    21+  (must hold the 20-29 band or older, plus confirm)
        - Join either:       18+ (the server's floor; the broader 16+ rule can't
                                  be enforced because there's no sub-18 role)
  * Age-range matching. Members say which age range(s) they want to room/ride
    with, and matches must satisfy BOTH people's ranges.
  * Double-blind and consensual. Nobody's identity is shown until BOTH say yes.
    Hosts have the final say and may decline freely, no arguing.
  * Interest-gathering only. We store NO addresses or personal info, real
    coordination happens in DMs/private group chats after a match.
  * The bot reaches out the moment it sniffs out a compatible match (most
    compatible first), not on a fixed daily timer.

Everything persists to the WebDAV store. We keep only the Discord user id (to
DM) and the preferences members typed.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.roommates")

# ---- store keys (each becomes a JSON file on WebDAV) --------------------------
LISTINGS = "roommate_listings"
OFFERS = "roommate_offers"
DECLINED = "roommate_declined"
OPTOUT = "roommate_optout"
ARCHIVE = "roommate_archive"
HUBMSG = "roommate_hub_msg"
AGREED = "roommate_agreed"  # user_ids who accepted the rules (gate before any listing)

MIN_CAP, MAX_CAP = 2, 15

# "kind:role" combos shown as one picker (keeps us within Discord's 5-row limit).
COMBO_OPTIONS = [
    ("🏨 Host a hotel room (I've got space)", "hotel:host"),
    ("🏨 Join / share a hotel room", "hotel:seeker"),
    ("🚗 Host a carpool (I'm driving)", "carpool:host"),
    ("🚗 Join a carpool", "carpool:seeker"),
]

_AGE_ANY = "__any__"

# Light guard against pasting addresses / phone numbers (rules: interest only).
_PII_RE = re.compile(
    r"(\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b)"
    r"|(\b\d{1,6}\s+\w+(\s+\w+)*\s+"
    r"(st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|ct|court|way|pl|place|apt|suite|ste)\b)",
    re.I,
)


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# A "specific date": a month name + day, a day + month, MM/DD[/YY], or YYYY-MM-DD.
_SPECIFIC_DATE_RE = re.compile(
    rf"({_MONTH}\s*\.?\s*\d{{1,2}})|(\d{{1,2}}\s*{_MONTH})"
    r"|(\b\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?\b)|(\b\d{4}-\d{1,2}-\d{1,2}\b)",
    re.I,
)


def _has_specific_date(text: str) -> bool:
    return bool(_SPECIFIC_DATE_RE.search(text or ""))


def _extract_dates(text: str, default_year: int) -> list[datetime.date]:
    """Best-effort pull of concrete dates out of free text (stdlib only)."""
    out: list[datetime.date] = []
    t = (text or "").lower()
    for mo in re.finditer(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):  # ISO
        try:
            out.append(datetime.date(int(mo[1]), int(mo[2]), int(mo[3])))
        except ValueError:
            pass
    for mo in re.finditer(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", t):  # MM/DD[/YY]
        mm, dd = int(mo[1]), int(mo[2])
        yy = mo[3]
        year = default_year if not yy else (int(yy) + 2000 if len(yy) == 2 else int(yy))
        try:
            out.append(datetime.date(year, mm, dd))
        except ValueError:
            pass
    for mo in re.finditer(rf"({_MONTH})\s*\.?\s*(\d{{1,2}})(?:\D{{1,4}}(\d{{4}}))?", t):  # Month DD[, YYYY]
        mm = _MONTH_NUM.get(mo[1][:3])
        if mm:
            try:
                out.append(datetime.date(int(mo[3]) if mo[3] else default_year, mm, int(mo[2])))
            except ValueError:
                pass
    return out


def _date_range(text: str) -> tuple[datetime.date, datetime.date] | None:
    """Return (earliest, latest) date extracted from free text, trying this year then next."""
    today = datetime.date.today()
    for year in (today.year, today.year + 1):
        dates = _extract_dates(text or "", year)
        if dates:
            return min(dates), max(dates)
    return None


# NYC/Northeast pickup areas used for carpool proximity scoring.
_PICKUP_AREAS: list[frozenset[str]] = [
    frozenset({"brooklyn", "bk"}),
    frozenset({"queens"}),
    frozenset({"manhattan", "nyc", "midtown", "downtown"}),
    frozenset({"bronx"}),
    frozenset({"staten island", "si"}),
    frozenset({"jersey city", "jc", "hoboken"}),
    frozenset({"newark"}),
    frozenset({"long island", "li", "nassau", "suffolk"}),
    frozenset({"westchester"}),
    frozenset({"connecticut", "ct", "fairfield"}),
    frozenset({"new jersey", "nj"}),
    frozenset({"upstate", "albany", "buffalo", "rochester", "syracuse"}),
]


def _pickup_area(text: str) -> int | None:
    """Return the index of the first recognized pickup area in `text`, or None."""
    t = text.lower()
    for i, terms in enumerate(_PICKUP_AREAS):
        if any(term in t for term in terms):
            return i
    return None


# Generic words stripped when comparing hotel names, so brand/location tokens
# (e.g. "Marriott", "Marquis", "Hilton") drive the match instead of filler.
_HOTEL_FILLER = {
    "hotel", "the", "inn", "suites", "suite", "resort", "spa", "and", "by", "at",
    "a", "an", "tbd", "flexible", "any", "undecided", "unsure", "unknown", "na", "none",
}


def _hotel_tokens(text: str) -> set[str]:
    """Meaningful tokens of a hotel name (brand/location), with filler removed."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {w for w in cleaned.split() if len(w) > 2 and w not in _HOTEL_FILLER}


def _short() -> str:
    return uuid.uuid4().hex[:8]


def _kind_label(kind: str) -> str:
    return "carpool" if kind == "carpool" else "hotel room"


def _cap_str(listing: dict) -> str:
    c = listing.get("cap")
    return f"{c} spots" if isinstance(c, int) else "open spot"


# ============================ persistent UI ===================================

class OfferButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rm:v1:(?P<oid>[a-f0-9]+):(?P<act>yes|no|stop)",
):
    """Yes / Pass / Stop on an anonymous match offer (survives restarts)."""

    _SPEC = {
        "yes": ("✅ Yes, connect us", discord.ButtonStyle.success),
        "no": ("✖️ Pass", discord.ButtonStyle.secondary),
        "stop": ("🚫 Stop matching me", discord.ButtonStyle.danger),
    }

    def __init__(self, oid: str, act: str) -> None:
        self.oid = oid
        self.act = act
        label, style = self._SPEC[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rm:v1:{oid}:{act}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["oid"], match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Roommates | None" = interaction.client.get_cog("Roommates")
        if cog is None:
            await interaction.response.send_message("This isn't available right now.", ephemeral=True)
            return
        await cog.handle_offer(interaction, self.oid, self.act)


def build_offer_view(oid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OfferButton(oid, "yes"))
    view.add_item(OfferButton(oid, "no"))
    view.add_item(OfferButton(oid, "stop"))
    return view


class HubView(discord.ui.View):
    """Persistent buttons under the channel hub message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    def _cog(interaction: discord.Interaction) -> "Roommates | None":
        return interaction.client.get_cog("Roommates")

    @discord.ui.button(label="🛏️ Register", style=discord.ButtonStyle.primary, custom_id="rm:hub:register")
    async def register(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await cog.hub_register(interaction)

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id="rm:hub:edit")
    async def edit(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await cog.hub_edit(interaction)

    @discord.ui.button(label="📋 Browse", style=discord.ButtonStyle.secondary, custom_id="rm:hub:browse")
    async def browse(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await cog.hub_browse(interaction)

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="rm:hub:search")
    async def search(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await cog.hub_search(interaction)

    @discord.ui.button(label="📊 My status", style=discord.ButtonStyle.secondary, custom_id="rm:hub:status", row=1)
    async def status(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await cog.hub_status(interaction)

    @discord.ui.button(label="📜 Rules", style=discord.ButtonStyle.secondary, custom_id="rm:hub:rules", row=1)
    async def rules(self, interaction, button):
        cog = self._cog(interaction)
        if cog:
            await interaction.response.send_message(cog._rules_text(), ephemeral=True)


# ============================ registration flow ===============================

class RulesAgreeView(discord.ui.View):
    """Shown before a member's first listing — they must accept the rules."""

    def __init__(self, cog: "Roommates", user_id: int, existing: dict | None = None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.existing = existing

    @discord.ui.button(label="✅ I agree — let's go", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form isn't yours, friend.", ephemeral=True)
            return
        # Open the form first (acknowledges the click), then persist agreement —
        # the WebDAV write must not delay the interaction response past 3s.
        await self.cog._open_registration(interaction, self.existing, edit=True)
        await self.cog._record_agreement(self.user_id)


class _ChoiceSelect(discord.ui.Select):
    """Single-choice select that writes one field on the parent RegistrationView."""

    def __init__(self, field: str, placeholder: str, options: list[tuple[str, str]], row: int) -> None:
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, row=row,
            options=[discord.SelectOption(label=lbl[:100], value=val[:100]) for lbl, val in options[:25]],
        )
        self.field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        view.sel[self.field] = self.values[0]
        for opt in self.options:
            opt.default = opt.value == self.values[0]
        await interaction.response.edit_message(content=view.render(), view=view)


class _AgePrefSelect(discord.ui.Select):
    """Multi-pick: which age range(s) the member wants to be matched with."""

    def __init__(self, bands: list[dict], row: int) -> None:
        options = [discord.SelectOption(label="No preference (any 18+)", value=_AGE_ANY)]
        options += [discord.SelectOption(label=b["label"][:100], value=b["key"][:100]) for b in bands[:24]]
        super().__init__(
            placeholder="Roommate age range(s) you'd like", row=row,
            min_values=1, max_values=len(options), options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        vals = list(self.values)
        view.sel["age"] = "any" if _AGE_ANY in vals else [v for v in vals if v != _AGE_ANY]
        for opt in self.options:
            opt.default = opt.value in vals
        await interaction.response.edit_message(content=view.render(), view=view)


class RegistrationView(discord.ui.View):
    def __init__(self, cog: "Roommates", user_id: int, cons: list[str], bands: list[dict],
                 existing: dict | None = None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.existing = existing
        self.sel: dict[str, object | None] = {"combo": None, "con": None, "age": None}
        self.add_item(_ChoiceSelect("combo", "What are you after?", COMBO_OPTIONS, row=0))
        self.add_item(_ChoiceSelect("con", "Which convention?", [(c, c) for c in cons], row=1))
        self.add_item(_AgePrefSelect(bands, row=2))
        if existing:
            self._prefill(existing)

    def _prefill(self, e: dict) -> None:
        self.sel["combo"] = f"{e['kind']}:{e['role']}"
        self.sel["con"] = e["con"]
        self.sel["age"] = e.get("age_prefs") or "any"
        for child in self.children:
            if isinstance(child, _ChoiceSelect):
                want = self.sel[child.field]
                for opt in child.options:
                    opt.default = opt.value == want
            elif isinstance(child, _AgePrefSelect):
                age = self.sel["age"]
                for opt in child.options:
                    opt.default = (opt.value == _AGE_ANY) if age == "any" else (opt.value in age)

    def render(self) -> str:
        def show(v):
            return f"**{v}**" if v else "_(pick one)_"
        combo_lbl = next((lbl for lbl, val in COMBO_OPTIONS if val == self.sel["combo"]), None)
        age = self.sel["age"]
        age_str = "**Any (18+)**" if age == "any" else (f"**{', '.join(age)}**" if age else "_(pick one)_")
        head = "✏️ **Edit your listing**" if self.existing else "🛏️ **Set up your room / ride search**"
        return (
            f"{head} (1 of 2)\n"
            f"• Type: {show(combo_lbl)}\n"
            f"• Con: {show(self.sel['con'])}\n"
            f"• Roommate age range: {age_str}\n\n"
            "Pick all three, then tap **Continue** for the headcount, dates and details. 🐾"
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.success, row=3)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form isn't yours, friend.", ephemeral=True)
            return
        if not all(self.sel[k] for k in ("combo", "con", "age")):
            await interaction.response.send_message(
                "Pick a type, a con, and an age range first, then we'll grab the rest. 🐾", ephemeral=True
            )
            return
        kind, role = str(self.sel["combo"]).split(":")
        await interaction.response.send_modal(
            RegistrationModal(self.cog, self.user_id, dict(self.sel), kind, role, self.existing)
        )


class RegistrationModal(discord.ui.Modal):
    """Uses the discord.ui.Label wrapper (the non-deprecated 2.7+ way to label
    modal inputs). Fields adapt to hotel vs carpool, and host vs seeker."""

    def __init__(self, cog: "Roommates", user_id: int, sel: dict, kind: str, role: str,
                 existing: dict | None = None) -> None:
        is_host = role == "host"
        carpool = kind == "carpool"
        hotel_kind = kind == "hotel"
        super().__init__(title="Room details" if is_host else "Your dates & preferences")
        self.cog = cog
        self.user_id = user_id
        self.sel = sel
        self.kind = kind
        self.role = role
        self.existing = existing or {}
        e = self.existing

        # Discord caps modals at 5 fields. For a hotel host that's:
        #   cap, hotel, dates, details, confirm  (budget folded into the vibe note).
        # Hotel seekers have room for budget; carpools have no hotel field.

        # Headcount is only relevant for hosts — seekers take whatever's available.
        self.cap: discord.ui.TextInput | None = None
        if is_host:
            self.cap = discord.ui.TextInput(
                required=True, max_length=2, placeholder="e.g. 4",
                default=str(e["cap"]) if isinstance(e.get("cap"), int) else None,
            )
            self.add_item(discord.ui.Label(
                text=f"How many spots? ({MIN_CAP}-{MAX_CAP} total)",
                description=("Seats in the car, including you." if carpool else "People sharing the room, including you."),
                component=self.cap,
            ))

        # Which hotel — only for hotel listings. Required of hosts (they provide
        # the room), optional for seekers (they may be flexible).
        self.hotel: discord.ui.TextInput | None = None
        if hotel_kind:
            self.hotel = discord.ui.TextInput(
                required=is_host, max_length=100, default=e.get("hotel") or None,
                placeholder=("e.g. Marriott Marquis (or 'TBD')" if is_host
                             else "e.g. the host hotel, or 'flexible'"),
            )
            self.add_item(discord.ui.Label(
                text="Which hotel?" if is_host else "Preferred hotel (optional)",
                description=("The hotel you've booked or plan to book (or 'TBD'). Name only, no room numbers."
                             if is_host else "A hotel you'd like to be at, or 'flexible' if you're open."),
                component=self.hotel,
            ))

        # Carpool location — framed by role. A driver describes their route and
        # where they can pick up; a passenger says where they're leaving from
        # (the key signal for whether they're on a driver's route). Migrate any
        # old listings that stored this in `details`.
        carpool_host = carpool and is_host
        self.area: discord.ui.TextInput | None = None
        if carpool:
            area_default = e.get("area") or e.get("details") or None
            self.area = discord.ui.TextInput(
                required=True, max_length=120, default=area_default,
                placeholder=("e.g. leaving Jersey City ~9am, can grab folks near the PATH" if is_host
                             else "e.g. Astoria, Queens"),
            )
            self.add_item(discord.ui.Label(
                text="Your route & pickup area" if is_host else "Where you're coming from",
                description=("Where you'll set off from and roughly where you can pick people up "
                             "(general areas only — NO home address)." if is_host else
                             "The general area you'd need to be picked up from (e.g. Brooklyn, "
                             "Jersey City). Neighborhood/town only — NO home address."),
                component=self.area,
            ))

        self.dates = discord.ui.TextInput(
            required=True, max_length=100, placeholder="e.g. Jul 2-5, 2026",
            default=e.get("dates") or e.get("days") or None,
        )
        self.add_item(discord.ui.Label(
            text="Your travel dates",
            description="Specific dates, not weekday names (e.g. Jul 2-5, 2026).",
            component=self.dates,
        ))

        # Free-text "about you / vibe" field. Carpool drivers skip it (no room
        # in the 5-field modal — their route field carries the key info).
        host_no_budget = hotel_kind and is_host  # hotel hosts drop budget for the hotel field
        self.details: discord.ui.TextInput | None = None
        if not carpool_host:
            if carpool:  # carpool passenger
                details_text = "About you (optional)"
                details_desc = "Non-smoking? Quiet or chatty? Fursuit luggage, music taste, etc."
                details_ph = "e.g. non-smoking, easygoing, a bit of fursuit luggage"
                details_default = (e.get("details") or None) if e.get("area") else None
            elif host_no_budget:  # hotel host (no separate budget field)
                details_text = "Vibe & preferences"
                details_desc = "Sleep schedule, smoking, fursuit-friendly, cost-split ideas, etc. No personal info."
                details_ph = "e.g. chill, non-smoking, night-owl or early riser?"
                details_default = e.get("details") or None
            else:  # hotel seeker / hotel host w/ budget handled elsewhere
                details_text = "Vibe & preferences"
                details_desc = "Sleep schedule, smoking, fursuit-friendly, etc. No personal info."
                details_ph = "e.g. chill, non-smoking, night-owl or early riser?"
                details_default = e.get("details") or None
            self.details = discord.ui.TextInput(
                style=discord.TextStyle.paragraph, required=False, max_length=400,
                default=details_default, placeholder=details_ph,
            )
            self.add_item(discord.ui.Label(text=details_text, description=details_desc, component=self.details))

        # Budget / cost-share for everyone except hotel hosts (no room in the modal).
        self.budget: discord.ui.TextInput | None = None
        if not host_no_budget:
            self.budget = discord.ui.TextInput(
                required=False, max_length=80, default=e.get("budget") or None,
                placeholder=("e.g. split gas + tolls evenly" if carpool_host else
                             "e.g. happy to split gas + tolls" if carpool else "e.g. split evenly"),
            )
            self.add_item(discord.ui.Label(
                text="Gas / cost share (optional)" if carpool else "Budget thoughts (optional)",
                component=self.budget,
            ))

        need21 = carpool and is_host
        self.confirm = discord.ui.TextInput(
            required=True, max_length=20, placeholder="21" if need21 else "18",
        )
        self.add_item(discord.ui.Label(
            text="Type 21, you must be 21+ to drive" if need21 else "Type 18 to confirm you're 18+",
            component=self.confirm,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cap_raw = self.cap.value.strip() if self.cap is not None else ""
        hotel = self.hotel.value.strip() if self.hotel is not None else ""
        area = self.area.value.strip() if self.area is not None else ""
        details = self.details.value.strip() if self.details is not None else ""
        # Hotel hosts have no budget field — keep any value they set previously.
        budget = self.budget.value.strip() if self.budget is not None else self.existing.get("budget", "")
        await self.cog.finalize_listing(
            interaction, self.user_id, self.sel, self.kind, self.role,
            cap_raw, hotel, area, self.dates.value.strip(), details,
            budget, self.confirm.value.strip(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("RegistrationModal.on_submit failed", exc_info=error)
        msg = "Something went wonky saving your listing — please try again in a moment! 🐾"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ============================ browse / search =================================

class _BrowseConSelect(discord.ui.Select):
    def __init__(self, cog: "Roommates", user_id: int, cons: list[str]) -> None:
        super().__init__(
            placeholder="Which con's listings do you want to see?", min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=c[:100], value=c[:100]) for c in cons[:25]],
        )
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.show_browse_results(interaction, self.user_id, self.values[0])


class _InterestSelect(discord.ui.Select):
    def __init__(self, cog: "Roommates", user_id: int, listings: list[dict]) -> None:
        options = []
        for lst in listings[:25]:
            code = lst["id"][:4].upper()
            tag = f"{'🚗' if lst['kind'] == 'carpool' else '🏨'} {lst['role']}"
            band = "/".join(lst.get("own_bands") or []) or "18+"
            # Surface the most useful snippet: hotel for hotels, route/origin for carpools.
            if lst["kind"] == "hotel" and lst.get("hotel"):
                hint = lst["hotel"]
            elif lst["kind"] == "carpool":
                hint = lst.get("area") or lst.get("details") or ""
            else:
                hint = lst.get("details", "")
            options.append(discord.SelectOption(
                label=f"{code} · {tag} · {_cap_str(lst)}"[:100],
                description=f"Age {band} · {hint[:50]}"[:100],
                value=lst["id"],
            ))
        super().__init__(placeholder="Express interest in a listing...", min_values=1, max_values=1, row=1, options=options)
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.express_interest(interaction, self.user_id, self.values[0])


class SearchModal(discord.ui.Modal):
    def __init__(self, cog: "Roommates", user_id: int) -> None:
        super().__init__(title="Search listings")
        self.cog = cog
        self.user_id = user_id
        self.query = discord.ui.TextInput(
            required=True, max_length=80, placeholder="e.g. Anthrocon, carpool, 2 beds",
        )
        self.add_item(discord.ui.Label(text="Con name or keyword", component=self.query))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.show_search_results(interaction, self.user_id, self.query.value.strip())


class _EditPickSelect(discord.ui.Select):
    def __init__(self, cog: "Roommates", user_id: int, listings: list[dict]) -> None:
        super().__init__(
            placeholder="Which listing do you want to edit?", min_values=1, max_values=1,
            options=[discord.SelectOption(
                label=f"{'🚗' if l['kind']=='carpool' else '🏨'} {l['con']} ({l['role']})"[:100], value=l["id"]
            ) for l in listings[:25]],
        )
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.open_edit(interaction, self.user_id, self.values[0])


# ================================= cog ========================================

class Roommates(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.sweep_loop.start()

    async def cog_unload(self) -> None:
        self.sweep_loop.cancel()

    # ---- small helpers ---------------------------------------------------

    def _s(self, key: str):
        return self.settings.get(key)

    def _guild(self) -> discord.Guild | None:
        gid = self.config.guild_id
        if gid:
            return self.bot.get_guild(gid)
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _member(self, user_id: int) -> discord.Member | None:
        g = self._guild()
        if g is None:
            return None
        m = g.get_member(user_id)
        if m is None:
            try:
                m = await g.fetch_member(user_id)
            except discord.HTTPException:
                m = None
        return m

    # ---- age roles / gating ---------------------------------------------

    def _age_bands(self) -> list[dict]:
        raw = self._s("roommate_age_roles") or ""
        bands: list[dict] = []
        for chunk in raw.split("\n"):
            for part in chunk.split(","):
                part = part.strip()
                if not part or ":" not in part:
                    continue
                label, rid = part.rsplit(":", 1)
                label, rid = label.strip(), rid.strip()
                if label and rid.isdigit():
                    bands.append({"key": label, "label": label, "role_id": int(rid), "index": len(bands)})
        return bands

    def _member_bands(self, member: discord.Member | None) -> list[str]:
        if member is None:
            return []
        by_role = {b["role_id"]: b["key"] for b in self._age_bands()}
        return [by_role[r.id] for r in getattr(member, "roles", []) if r.id in by_role]

    def _fallback_adult_role_id(self) -> int:
        return int(self._s("roommate_adult_role_id") or 0) or int(self.config.floofs_role_id or 0)

    def _adult_configured(self) -> bool:
        return bool(self._age_bands()) or bool(self._fallback_adult_role_id())

    def _is_adult(self, member: discord.Member | None) -> bool:
        if member is None:
            return False
        if self._age_bands():
            return bool(self._member_bands(member))
        rid = self._fallback_adult_role_id()
        return bool(rid) and any(r.id == rid for r in getattr(member, "roles", []))

    def _can_host_carpool(self, member: discord.Member | None) -> bool:
        """21+ requirement, approximated as 'holds the 20-29 band or older'."""
        bands = self._age_bands()
        if not bands:
            return self._is_adult(member)
        idx = {b["key"]: b["index"] for b in bands}
        return any(idx.get(k, -1) >= 1 for k in self._member_bands(member))

    @staticmethod
    def _age_pref_ok(prefs, other_bands: list[str]) -> bool:
        if prefs == "any" or not prefs:
            return True
        if not other_bands:
            return True
        return any(b in prefs for b in other_bands)

    def _bands_for(self, listing: dict, member: discord.Member | None) -> list[str]:
        if member is not None:
            live = self._member_bands(member)
            if live:
                return live
        return listing.get("own_bands") or []

    def _age_compatible(self, a: dict, a_bands: list[str], b: dict, b_bands: list[str]) -> bool:
        return (self._age_pref_ok(a.get("age_prefs"), b_bands)
                and self._age_pref_ok(b.get("age_prefs"), a_bands))

    async def _gate(self, interaction: discord.Interaction) -> tuple[discord.Member | None, str | None]:
        if not self._s("roommate_enabled"):
            return None, "The roommate finder is napping right now (not switched on)."
        if not self._adult_configured():
            return None, "The roommate finder isn't fully set up yet, an admin still needs to set the 18+ role(s)."
        member = interaction.user if isinstance(interaction.user, discord.Member) else await self._member(interaction.user.id)
        if not self._is_adult(member):
            return None, "🔞 The roommate finder is for age-verified (18+) fluffs only."
        return member, None

    # ---- listing storage -------------------------------------------------

    def _all_listings(self) -> dict:
        return self.store.get(LISTINGS, {})

    def _open_listings(self) -> list[dict]:
        return [l for l in self._all_listings().values() if l.get("status") == "open"]

    def _user_listings(self, user_id: int) -> list[dict]:
        return [l for l in self._open_listings() if l.get("user_id") == user_id]

    def _user_listing(self, user_id: int, con: str, kind: str | None = None) -> dict | None:
        for l in self._open_listings():
            if l["user_id"] == user_id and l["con"] == con and (kind is None or l["kind"] == kind):
                return l
        return None

    @staticmethod
    def _pair_key(a: int, b: int, con: str, kind: str) -> str:
        lo, hi = sorted((a, b))
        return f"{lo}-{hi}-{kind}-{con}"

    def _anon_summary(self, lst: dict) -> str:
        carpool = lst["kind"] == "carpool"
        band = "/".join(lst.get("own_bands") or []) or "18+"
        role = "Host" if lst["role"] == "host" else "Looking to join"
        bits = [
            f"🏷️ {'🚗 Carpool' if carpool else '🏨 Hotel room'} · **{role}**",
            f"🎪 Con: **{lst['con']}**",
            f"🎂 Their age range: {band}",
        ]
        if lst.get("cap") is not None:
            bits.append(f"👥 {'Seats' if carpool else 'Spots'}: {_cap_str(lst)}")
        if not carpool and lst.get("hotel"):
            bits.append(f"🏩 Hotel: {lst['hotel']}")
        if carpool:
            # `area` is the new home for pickup/route; old listings kept it in `details`.
            pickup = lst.get("area") or (lst.get("details") if not lst.get("area") else "")
            label = "🚏 Route/pickup" if lst["role"] == "host" else "🚏 Coming from"
            if pickup:
                bits.append(f"{label}: {pickup}")
        if lst.get("dates") or lst.get("days"):
            bits.append(f"📅 Dates: {lst.get('dates') or lst.get('days')}")
        if lst.get("budget"):
            bits.append(f"💵 Cost: {lst['budget']}")
        # "About" text: carpool stores it in details only when `area` is set (new format).
        about = lst.get("details") if (not carpool or lst.get("area")) else ""
        if about:
            bits.append(f"📝 About: {about}" if carpool else f"📝 Details: {about}")
        return "\n".join(bits)

    def _con_window(self, con: str) -> tuple[datetime.date, datetime.date] | None:
        raw = self._s("roommate_con_dates") or ""
        for part in re.split(r"[;\n]", raw):
            if ":" not in part:
                continue
            name, rng = part.split(":", 1)
            if name.strip().lower() != con.lower():
                continue
            mr = re.search(r"(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})", rng)
            if mr:
                try:
                    return datetime.date.fromisoformat(mr[1]), datetime.date.fromisoformat(mr[2])
                except ValueError:
                    return None
        return None

    def _validate_dates(self, text: str, con: str) -> str | None:
        """Return an error message if the dates aren't specific / in window, else None."""
        if not _has_specific_date(text):
            return ("📅 Please enter **specific dates** like `Jul 2-5, 2026`, not just weekday names, so I can "
                    "line up everyone's availability. 🐾")
        win = self._con_window(con)
        if win:
            start, end = win
            found = _extract_dates(text, start.year)
            if found and not any(start <= d <= end for d in found):
                return (f"📅 Those dates look outside **{con}** ({start.isoformat()} to {end.isoformat()}). "
                        "Double-check and try again. 🐾")
        return None

    async def finalize_listing(self, interaction, user_id, sel, kind, role,
                               cap_raw, hotel, area, dates, details, budget, confirm) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        con = sel["con"]
        # Headcount is only required from hosts; seekers take whatever's available.
        is_host = role == "host"
        if is_host:
            if not cap_raw.isdigit() or not (MIN_CAP <= int(cap_raw) <= MAX_CAP):
                await interaction.response.send_message(
                    f"Pop in a whole number of spots between **{MIN_CAP} and {MAX_CAP}**, then try again. 🐾",
                    ephemeral=True,
                )
                return
            cap: int | None = int(cap_raw)
        else:
            cap = None
        # Validate dates: require specific calendar dates (not just weekday names),
        # and, if staff configured a window for this con, that they fall inside it.
        date_err = self._validate_dates(dates, con)
        if date_err:
            await interaction.response.send_message(date_err, ephemeral=True)
            return
        # Age gate per the rules.
        need21 = kind == "carpool" and role == "host"
        if need21 and not self._can_host_carpool(member):
            await interaction.response.send_message(
                "🚗 You've gotta be **21+** (in the 20-29 band or older) to **host a carpool**. You can still hop "
                "in as a carpool passenger, or host/join a hotel room. 🐾", ephemeral=True,
            )
            return
        if (need21 and "21" not in confirm) or (not need21 and not ("18" in confirm or "21" in confirm)):
            await interaction.response.send_message(
                "I couldn't confirm your age from that, so nothing got saved. Give it another go and type "
                f"**{'21' if need21 else '18'}** to confirm. 🐾", ephemeral=True,
            )
            return
        # Privacy guard: no addresses / phone numbers (interest gathering only).
        if any(_PII_RE.search(t or "") for t in (dates, details, budget, hotel, area)):
            await interaction.response.send_message(
                "🔒 Keep **addresses and personal info out** of your listing please, these posts are just for "
                "rounding up interest. Swap exact pickup spots and contact details in a private DM or group chat "
                "**after** you match. Scrub those out and try again. 🐾", ephemeral=True,
            )
            return

        # All validation passed — defer NOW so the WebDAV save (which can take
        # a few seconds) doesn't expire the 3-second interaction token.
        await interaction.response.defer(ephemeral=True)

        listings = dict(self._all_listings())
        existing = self._user_listing(user_id, con, kind)
        lid = existing["id"] if existing else _short()
        listings[lid] = {
            "id": lid, "user_id": user_id, "con": con, "kind": kind, "role": role,
            "cap": cap, "age_prefs": sel.get("age") or "any",
            "own_bands": self._member_bands(member),
            "hotel": hotel, "area": area, "dates": dates, "budget": budget, "details": details,
            "status": "open",
            "created_at": existing["created_at"] if existing else int(time.time()),
            "updated_at": int(time.time()),
        }
        await self.store.set(LISTINGS, listings)

        verb = "updated" if existing else "saved"
        await interaction.followup.send(
            f"{'🔄' if existing else '🎉'} {'Updated' if existing else 'Woohoo, you are in!'} "
            f"Your **{_kind_label(kind)}** listing for **{con}** "
            f"({'hosting' if role == 'host' else 'looking to join'}) is {'updated' if existing else 'live'}. "
            "I'll DM you the moment I sniff out a match! "
            "Make sure your **DMs are open** to server members so I can reach you. 🐾",
            ephemeral=True,
        )

        await self._match_for_listing(lid)

    # ---- hub button handlers --------------------------------------------

    def _has_agreed(self, user_id: int) -> bool:
        return user_id in set(self.store.get(AGREED, []))

    async def _record_agreement(self, user_id: int) -> None:
        agreed = list(self.store.get(AGREED, []))
        if user_id not in agreed:
            agreed.append(user_id)
            await self.store.set(AGREED, agreed)

    async def hub_register(self, interaction: discord.Interaction, existing: dict | None = None) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        cons = self._cons()
        if not cons:
            await interaction.response.send_message(
                "No conventions are set up yet. An admin can add them with "
                "`/config set roommate_cons \"Anthrocon, FurDU, ...\"` or `/roommate importcons`.", ephemeral=True,
            )
            return
        # Everyone must agree to the rules once before creating any listing.
        if not self._has_agreed(interaction.user.id):
            await interaction.response.send_message(
                self._rules_text()
                + "\n\nBy tapping **✅ I agree** below you confirm you've read and accept these rules. 🐾",
                view=RulesAgreeView(self, interaction.user.id, existing),
                ephemeral=True,
            )
            return
        await self._open_registration(interaction, existing)

    async def _open_registration(
        self, interaction: discord.Interaction, existing: dict | None = None, *, edit: bool = False
    ) -> None:
        view = RegistrationView(self, interaction.user.id, self._cons(), self._age_bands(), existing)
        if edit:
            await interaction.response.edit_message(content=view.render(), view=view)
        else:
            await interaction.response.send_message(view.render(), view=view, ephemeral=True)

    async def hub_edit(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        mine = self._user_listings(interaction.user.id)
        if not mine:
            await interaction.response.send_message(
                "You don't have any listings to edit yet. Tap **🛏️ Register** to make one! 🐾", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_EditPickSelect(self, interaction.user.id, mine))
        await interaction.response.send_message("Which listing shall we tweak?", view=view, ephemeral=True)

    async def open_edit(self, interaction: discord.Interaction, user_id: int, listing_id: str) -> None:
        lst = self._all_listings().get(listing_id)
        if not lst or lst.get("user_id") != user_id or lst.get("status") != "open":
            await interaction.response.edit_message(content="That listing isn't around anymore.", view=None)
            return
        cons = self._cons()
        view = RegistrationView(self, user_id, cons, self._age_bands(), existing=lst)
        await interaction.response.edit_message(content=view.render(), view=view)

    async def hub_browse(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        cons = self._cons()
        if not cons:
            await interaction.response.send_message("No conventions are set up yet.", ephemeral=True)
            return
        view = discord.ui.View(timeout=600)
        view.add_item(_BrowseConSelect(self, interaction.user.id, cons))
        await interaction.response.send_message(
            "📋 Pick a con to peek at who's looking (names stay hidden):", view=view, ephemeral=True
        )

    async def hub_search(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.send_modal(SearchModal(self, interaction.user.id))

    async def hub_status(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await self._render_status(interaction, interaction.user.id)

    def _cons(self) -> list[str]:
        raw = self._s("roommate_cons") or ""
        parts = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
        return [p for p in parts if p]

    async def _merge_cons(self, names: list[str]) -> list[str]:
        existing = self._cons()
        seen = {c.lower() for c in existing}
        added = []
        for n in names:
            n = " ".join((n or "").split()).strip()
            if not n or n.lower() in seen:
                continue
            seen.add(n.lower())
            existing.append(n)
            added.append(n)
        if added:
            await self.settings.set("roommate_cons", ", ".join(existing))
        return added

    # ---- browse / search results ----------------------------------------

    def _visible_for(self, user_id: int, con: str) -> list[dict]:
        mine = [l for l in self._user_listings(user_id) if l["con"] == con]
        out = []
        for l in self._open_listings():
            if l["con"] != con or l["user_id"] == user_id:
                continue
            if mine and not any(
                self._valid_pair(m, l) and self._age_compatible(
                    m, self._bands_for(m, None), l, self._bands_for(l, None)
                ) for m in mine
            ):
                continue
            out.append(l)
        out.sort(key=lambda l: l["created_at"])
        return out

    async def show_browse_results(self, interaction: discord.Interaction, user_id: int, con: str) -> None:
        listings = self._visible_for(user_id, con)
        if not listings:
            reg_btn = discord.ui.Button(label="Register for this con", style=discord.ButtonStyle.primary, emoji="🛏️")
            cog_ref = self
            async def _go_register(inter: discord.Interaction) -> None:
                await cog_ref.hub_register(inter)
            reg_btn.callback = _go_register
            view = discord.ui.View(timeout=300)
            view.add_item(reg_btn)
            await interaction.response.edit_message(
                content=f"No listings for **{con}** yet — be the first! 🐾\n"
                        "Register below and I'll match you the moment someone else signs up.",
                view=view,
            )
            return
        lines = [f"📋 **{len(listings)} open listing(s) for {con}** (names hidden):\n"]
        for l in listings[:25]:
            band = "/".join(l.get("own_bands") or []) or "18+"
            lines.append(f"• **{l['id'][:4].upper()}** {'🚗' if l['kind']=='carpool' else '🏨'} "
                         f"{'host' if l['role']=='host' else 'join'}, {_cap_str(l)}, age {band}")
        lines.append("\nPick one below to wag your interest. They'll get an anonymous heads up with your answers, "
                     "and if you both say yes I'll introduce you. 🐾")
        view = discord.ui.View(timeout=600)
        view.add_item(_InterestSelect(self, user_id, listings))
        await interaction.response.edit_message(content="\n".join(lines)[:1900], view=view)

    async def show_search_results(self, interaction: discord.Interaction, user_id: int, query: str) -> None:
        q = query.lower().strip()
        hits = []
        for l in self._open_listings():
            if l["user_id"] == user_id:
                continue
            hay = " ".join([l["con"], l["kind"], l["role"], l.get("details", "")]).lower()
            if q in hay:
                hits.append(l)

        if not hits:
            # If the query looks like a con name, redirect to browse for that con.
            matching = [c for c in self._cons() if q in c.lower() or c.lower() in q]
            if matching:
                con = matching[0]
                listings = self._visible_for(user_id, con)
                if not listings:
                    reg_btn = discord.ui.Button(label="Register for this con", style=discord.ButtonStyle.primary, emoji="🛏️")
                    cog_ref = self
                    async def _go_register(inter: discord.Interaction) -> None:
                        await cog_ref.hub_register(inter)
                    reg_btn.callback = _go_register
                    view = discord.ui.View(timeout=300)
                    view.add_item(reg_btn)
                    await interaction.response.send_message(
                        f"No listings for **{con}** yet — be the first! 🐾\n"
                        "Register below and I'll match you the moment someone else signs up.",
                        view=view, ephemeral=True,
                    )
                else:
                    hits = listings
                    lines = [f"📋 **{len(hits)} listing(s) for {con}** (names hidden):\n"]
                    for l in hits[:25]:
                        band = "/".join(l.get("own_bands") or []) or "18+"
                        lines.append(f"• **{l['id'][:4].upper()}** {'🚗' if l['kind']=='carpool' else '🏨'} "
                                     f"{'host' if l['role']=='host' else 'join'}, {_cap_str(l)}, age {band}")
                    lines.append("\nPick one below to wag your interest. 🐾")
                    view = discord.ui.View(timeout=600)
                    view.add_item(_InterestSelect(self, user_id, hits))
                    await interaction.response.send_message("\n".join(lines)[:1900], view=view, ephemeral=True)
                return
            # No con match and no listings — list available cons so the user knows what to search.
            cons = self._cons()
            cons_hint = f"\n\nAvailable cons: {', '.join(cons[:20])}" if cons else ""
            await interaction.response.send_message(
                f"Nothing matched **{query}**. Try a con name (e.g. `Anthrocon`) or register your own listing! 🐾"
                + cons_hint,
                ephemeral=True,
            )
            return

        hits.sort(key=lambda l: l["created_at"])
        lines = [f"🔎 **{len(hits)} match(es) for \"{query}\"** (names hidden):\n"]
        for l in hits[:25]:
            band = "/".join(l.get("own_bands") or []) or "18+"
            lines.append(f"• **{l['id'][:4].upper()}** {l['con']}, {'🚗' if l['kind']=='carpool' else '🏨'} "
                         f"{'host' if l['role']=='host' else 'join'}, age {band}")
        lines.append("\nPick one below to wag your interest (you'll need your own compatible listing for that con).")
        view = discord.ui.View(timeout=600)
        view.add_item(_InterestSelect(self, user_id, hits))
        await interaction.response.send_message("\n".join(lines)[:1900], view=view, ephemeral=True)

    # ---- pairing / scoring ----------------------------------------------

    def _valid_pair(self, a: dict, b: dict):
        if a["kind"] != b["kind"] or a["con"] != b["con"] or a["user_id"] == b["user_id"]:
            return None
        ah, bh = a["role"] == "host", b["role"] == "host"
        if ah and bh:
            return None
        if not ah and not bh:
            if a["kind"] == "carpool":
                return None
            older, newer = (a, b) if a["created_at"] <= b["created_at"] else (b, a)
            return (older, newer)
        host = a if ah else b
        seeker = b if ah else a
        return (seeker, host)

    def _score(self, a: dict, b: dict, a_bands: list[str], b_bands: list[str]) -> int:
        s = 0

        # Age band overlap — strong compatibility signal.
        if set(a_bands) & set(b_bands):
            s += 3

        # Date overlap — most important practical factor.
        ar = _date_range(a.get("dates") or "")
        br = _date_range(b.get("dates") or "")
        if ar and br:
            a0, a1 = ar
            b0, b1 = br
            if a0 <= b1 and b0 <= a1:
                overlap = (min(a1, b1) - max(a0, b0)).days + 1
                s += min(overlap, 4)        # up to +4 for long overlap
            else:
                s -= 8                      # non-overlapping dates: hard to room together
        elif ar or br:
            s -= 1                          # one side has dates, other doesn't

        # Vibe keyword compatibility.
        ad = (a.get("details") or "").lower()
        bd = (b.get("details") or "").lower()

        # Smoking: non-smoker with a smoker is a bad match.
        a_ns = bool(re.search(r"non.?smok|no smok", ad))
        b_ns = bool(re.search(r"non.?smok|no smok", bd))
        a_smok = "smok" in ad and not a_ns
        b_smok = "smok" in bd and not b_ns
        if (a_ns and b_smok) or (b_ns and a_smok):
            s -= 4
        elif a_ns and b_ns:
            s += 1

        # Sleep schedule.
        a_early = bool(re.search(r"early.?(bird|riser|morning|wake)|morning person", ad))
        b_early = bool(re.search(r"early.?(bird|riser|morning|wake)|morning person", bd))
        a_late = bool(re.search(r"night.?owl|late night|late sleeper|stay up|night person", ad))
        b_late = bool(re.search(r"night.?owl|late night|late sleeper|stay up|night person", bd))
        if (a_early and b_early) or (a_late and b_late):
            s += 2
        elif (a_early and b_late) or (a_late and b_early):
            s -= 2

        # Fursuit-friendly — both hauling suits means shared priorities.
        a_suit = bool(re.search(r"fursuit|suit|head\b|suit.?bag|fursuiting", ad))
        b_suit = bool(re.search(r"fursuit|suit|head\b|suit.?bag|fursuiting", bd))
        if a_suit and b_suit:
            s += 1

        # Budget tier: cheap vs luxury is a mismatch.
        def _tier(t: str) -> int:
            t = t.lower()
            if re.search(r"budget|cheap|affordable|low.?cost|split even", t):
                return 0
            if re.search(r"luxury|premium|nice hotel|fancy|upscale|splurge", t):
                return 2
            return 1
        at = _tier(a.get("budget") or "")
        bt = _tier(b.get("budget") or "")
        if at == bt:
            s += 1
        elif abs(at - bt) > 1:
            s -= 2

        # Carpool: pickup area proximity (same borough/region = strong fit).
        # Origin/route lives in `area` now; old listings kept it in `details`.
        if a.get("kind") == "carpool":
            a_loc = f"{a.get('area') or ''} {a.get('details') or ''}"
            b_loc = f"{b.get('area') or ''} {b.get('details') or ''}"
            aa, ba = _pickup_area(a_loc), _pickup_area(b_loc)
            if aa is not None and ba is not None:
                if aa == ba:
                    s += 3
                else:
                    s -= 1

        # Hotel: rooming at the same hotel is the ideal pairing.
        if a.get("kind") == "hotel":
            ha, hb = _hotel_tokens(a.get("hotel") or ""), _hotel_tokens(b.get("hotel") or "")
            if ha and hb:
                if ha & hb:
                    s += 3
                else:
                    s -= 1

        return s

    async def _candidates_for(self, listing: dict) -> list[tuple[int, dict]]:
        optout = set(self.store.get(OPTOUT, []))
        declined = self.store.get(DECLINED, {})
        offers = self.store.get(OFFERS, {})
        busy_pairs = {frozenset((o["u1"], o["u2"])) for o in offers.values()}
        me = await self._member(listing["user_id"])
        my_bands = self._bands_for(listing, me)
        ranked = []
        for cand in self._open_listings():
            if cand["id"] == listing["id"] or cand["user_id"] == listing["user_id"]:
                continue
            if cand["user_id"] in optout:
                continue
            if not self._valid_pair(listing, cand):
                continue
            if self._pair_key(listing["user_id"], cand["user_id"], listing["con"], listing["kind"]) in declined:
                continue
            if frozenset((listing["user_id"], cand["user_id"])) in busy_pairs:
                continue
            cm = await self._member(cand["user_id"])
            if not self._is_adult(cm) or not self._is_adult(me):
                continue
            cb = self._bands_for(cand, cm)
            if not self._age_compatible(listing, my_bands, cand, cb):
                continue
            ranked.append((self._score(listing, cand, my_bands, cb), cand))
        ranked.sort(key=lambda t: (-t[0], t[1]["created_at"]))
        return ranked

    async def _match_for_listing(self, listing_id: str) -> None:
        listing = self._all_listings().get(listing_id)
        if not listing or listing.get("status") != "open":
            return
        if listing["user_id"] in set(self.store.get(OPTOUT, [])):
            return
        if any(listing["user_id"] in (o["u1"], o["u2"]) for o in self.store.get(OFFERS, {}).values()):
            return
        ranked = await self._candidates_for(listing)
        if not ranked:
            return
        pair = self._valid_pair(listing, ranked[0][1])
        if pair:
            await self._open_offer(*pair)

    async def _open_offer(self, initiator: dict, approver: dict) -> None:
        oid = _short()
        host = approver["user_id"] if approver["role"] == "host" else (
            initiator["user_id"] if initiator["role"] == "host" else 0)
        offers = dict(self.store.get(OFFERS, {}))
        offers[oid] = {
            "id": oid, "con": initiator["con"], "kind": initiator["kind"], "host": host,
            "u1": initiator["user_id"], "u2": approver["user_id"],
            "s1": "pending", "s2": "pending", "asked1": False, "asked2": False,
            "revealed": False, "created_at": int(time.time()),
        }
        await self.store.set(OFFERS, offers)
        await self._advance(oid)

    # ---- offer state machine --------------------------------------------

    async def _advance(self, oid: str) -> None:
        offers = dict(self.store.get(OFFERS, {}))
        o = offers.get(oid)
        if not o:
            return
        if o["s1"] == "no" or o["s2"] == "no":
            await self._close_offer(oid, declined=True)
            return
        if o["s1"] == "pending" and not o.get("asked1"):
            o["asked1"] = True
            await self.store.set(OFFERS, offers)
            await self._ask(o, side=1)
            return
        if o["s1"] == "yes" and o["s2"] == "pending" and not o.get("asked2"):
            o["asked2"] = True
            await self.store.set(OFFERS, offers)
            await self._ask(o, side=2)
            return
        if o["s1"] == "yes" and o["s2"] == "yes" and not o.get("revealed"):
            o["revealed"] = True
            await self.store.set(OFFERS, offers)
            await self._reveal(o)
            await self._close_offer(oid, matched=True)

    async def _ask(self, o: dict, side: int) -> None:
        uid = o["u1"] if side == 1 else o["u2"]
        other = o["u2"] if side == 1 else o["u1"]
        lst = self._user_listing(other, o["con"], o["kind"])
        if not lst:
            await self._close_offer(o["id"])
            return
        member = await self._member(uid)
        if member is None or not self._is_adult(member):
            return
        kind = _kind_label(o["kind"])
        if o.get("host") == uid:
            lead = f"🐾 Hey! Exciting news — someone wants to join your **{kind}** for **{o['con']}**! Here's a peek at them (no names yet):"
            tail = ("As the host it's **totally your call** — approve or pass, no pressure and no need to explain. "
                    "If you both say yes, I'll introduce you! 🎉")
        elif o.get("host") == other:
            lead = f"🐾 Hey! I found a **{kind}** host for **{o['con']}** that looks like a great fit! Here's their setup (no names yet):"
            tail = "Interested? Hit Yes and I'll ask the host too — if you're both in, I'll make the intro! 🎉"
        else:
            lead = f"🐾 Hey! I sniffed out a possible **roommate match** for **{o['con']}**! Here's about them (no names yet):"
            tail = "If you're both interested, I'll introduce you! 🎉"
        try:
            await member.send(f"{lead}\n\n{self._anon_summary(lst)}\n\n{tail}", view=build_offer_view(o["id"]))
        except discord.HTTPException:
            log.warning("Could not DM %s for offer %s — closing offer so they aren't stuck.", uid, o["id"])
            await self._close_offer(o["id"])

    async def _reveal(self, o: dict) -> None:
        con, kind = o["con"], _kind_label(o["kind"])
        m1 = await self._member(o["u1"])
        m2 = await self._member(o["u2"])
        coord = (
            "Lock in a **pickup time**, a **public meet-up spot**, and who's chipping in for gas/tolls. "
            if o["kind"] == "carpool" else
            "Sort out **check-in/out days**, the **hotel**, and how you're splitting the room. "
        )
        note = (
            "\n\nNext up: scurry into **DMs or a private group chat** to plan. " + coord +
            "A few reminders: the host has the final say on the group, keep **addresses and personal info out of "
            "public channels**, name your space limit so nobody's squished (mind the fursuit luggage!), and "
            "remember **this server isn't responsible** for planning, mishaps, or theft. Use your best judgement "
            "and keep your tail safe. 🐾"
        )
        if m1:
            who = f"{m2.mention} (`{m2}`)" if m2 else f"<@{o['u2']}>"
            try:
                await m1.send(f"🎉 **It's a match for {con}!** You both said yes — say hi to {who}! 🐾" + note)
            except discord.HTTPException:
                pass
        if m2:
            who = f"{m1.mention} (`{m1}`)" if m1 else f"<@{o['u1']}>"
            try:
                await m2.send(f"🎉 **It's a match for {con}!** You both said yes — say hi to {who}! 🐾" + note)
            except discord.HTTPException:
                pass

    async def _close_offer(self, oid: str, *, declined: bool = False, matched: bool = False) -> None:
        offers = dict(self.store.get(OFFERS, {}))
        o = offers.pop(oid, None)
        await self.store.set(OFFERS, offers)
        if not o:
            return
        if declined:
            # Remember the pass so we never re-suggest these two, then immediately keep
            # looking for OTHER good fits for both of them (so one "no" doesn't end their search).
            decl = dict(self.store.get(DECLINED, {}))
            decl[self._pair_key(o["u1"], o["u2"], o["con"], o["kind"])] = int(time.time())
            await self.store.set(DECLINED, decl)
            await self._reseek(o["u1"])
            await self._reseek(o["u2"])
        if matched:
            await self._set_status(o["u1"], o["con"], o["kind"], "matched")
            await self._set_status(o["u2"], o["con"], o["kind"], "matched")

    async def _reseek(self, user_id: int) -> None:
        """After a pass, try to find a different compatible match for this member."""
        if user_id in set(self.store.get(OPTOUT, [])):
            return
        if any(user_id in (o["u1"], o["u2"]) for o in self.store.get(OFFERS, {}).values()):
            return
        for l in self._user_listings(user_id):
            await self._match_for_listing(l["id"])
            if any(user_id in (o["u1"], o["u2"]) for o in self.store.get(OFFERS, {}).values()):
                break  # got a fresh suggestion; one at a time keeps outreach minimal

    async def _set_status(self, user_id: int, con: str, kind: str, status: str) -> None:
        listings = dict(self._all_listings())
        for l in listings.values():
            if (l.get("user_id") == user_id and l.get("con") == con
                    and l.get("kind") == kind and l.get("status") == "open"):
                l["status"] = status
                l["updated_at"] = int(time.time())
                await self.store.set(LISTINGS, listings)
                return

    async def _optout(self, user_id: int) -> None:
        opt = list(self.store.get(OPTOUT, []))
        if user_id not in opt:
            opt.append(user_id)
            await self.store.set(OPTOUT, opt)

    # ---- expressing interest (user-initiated) ---------------------------

    async def express_interest(self, interaction: discord.Interaction, user_id: int, listing_id: str) -> None:
        # Acknowledge immediately so the WebDAV write near the end can't blow the
        # 3-second interaction window (this was the "interaction failed" cause).
        await interaction.response.defer(ephemeral=True)
        member, err = await self._gate(interaction)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        target = self._all_listings().get(listing_id)
        if not target or target.get("status") != "open":
            await interaction.followup.send("That listing scampered off (no longer available).", ephemeral=True)
            return
        if target["user_id"] == user_id:
            await interaction.followup.send("That's your own listing, silly. 🙂", ephemeral=True)
            return
        if target["user_id"] in set(self.store.get(OPTOUT, [])):
            await interaction.followup.send("That member isn't taking new suggestions right now.", ephemeral=True)
            return
        mine = next((m for m in self._user_listings(user_id) if self._valid_pair(m, target)), None)
        if not mine:
            await interaction.followup.send(
                f"You'll need your own compatible **{_kind_label(target['kind'])}** listing for "
                f"**{target['con']}** first so they can see what you're after. Tap **🛏️ Register**, then try "
                "again. 🐾", ephemeral=True,
            )
            return
        if not self._age_compatible(mine, self._bands_for(mine, member), target, self._bands_for(target, None)):
            await interaction.followup.send(
                "Your age-range picks don't line up with this listing, so I can't connect you here.", ephemeral=True
            )
            return
        if self._pair_key(user_id, target["user_id"], target["con"], target["kind"]) in self.store.get(DECLINED, {}):
            await interaction.followup.send("You two already passed on each other for this one.", ephemeral=True)
            return
        for o in self.store.get(OFFERS, {}).values():
            if {o["u1"], o["u2"]} == {user_id, target["user_id"]} and o["con"] == target["con"] and o["kind"] == target["kind"]:
                await interaction.followup.send("There's already a connection brewing here. 🐾", ephemeral=True)
                return
        oid = _short()
        host = target["user_id"] if target["role"] == "host" else (user_id if mine["role"] == "host" else 0)
        offers = dict(self.store.get(OFFERS, {}))
        offers[oid] = {
            "id": oid, "con": target["con"], "kind": target["kind"], "host": host,
            "u1": user_id, "u2": target["user_id"],
            "s1": "yes", "s2": "pending", "asked1": True, "asked2": False,
            "revealed": False, "created_at": int(time.time()),
        }
        await self.store.set(OFFERS, offers)
        await interaction.followup.send(
            "📨 Sent! They'll get an **anonymous** heads up with your answers. If they're in too, I'll introduce "
            "you both, and I won't share your name unless you both say yes. 🐾", ephemeral=True,
        )
        await self._advance(oid)

    # ---- responding to an offer -----------------------------------------

    async def handle_offer(self, interaction: discord.Interaction, oid: str, act: str) -> None:
        # Acknowledge immediately — the store writes below can take a few seconds
        # on the live bot, which would otherwise blow Discord's 3-second window.
        await interaction.response.defer()
        offers = dict(self.store.get(OFFERS, {}))
        o = offers.get(oid)
        if not o:
            await self._safe_edit(interaction, "This match has wandered off (no longer active).")
            return
        uid = interaction.user.id
        if uid == o["u1"]:
            side = 1
        elif uid == o["u2"]:
            side = 2
        else:
            await interaction.followup.send("This isn't for you, friend.", ephemeral=True)
            return
        if o[f"s{side}"] != "pending":
            await self._safe_edit(interaction, "You've already answered this one. 🐾")
            return

        if act == "stop":
            await self._optout(uid)
            o[f"s{side}"] = "no"
            await self.store.set(OFFERS, offers)
            await self._safe_edit(interaction, "🚫 You got it, I won't suggest matches to you again. Run `/roommate find` anytime to hop back in.")
            await self._advance(oid)
            return
        if act == "no":
            o[f"s{side}"] = "no"
            await self.store.set(OFFERS, offers)
            await self._safe_edit(interaction, "👍 No worries at all, I won't connect you two. Your listing stays up for other matches.")
            await self._advance(oid)
            return

        member = await self._member(uid)
        if not self._is_adult(member):
            await self._safe_edit(interaction, "I can only connect age-verified (18+) fluffs.")
            return
        o[f"s{side}"] = "yes"
        await self.store.set(OFFERS, offers)
        if o["s1"] == "yes" and o["s2"] == "yes":
            await self._safe_edit(interaction, "🎉 It's a match! Check your DMs, I'm introducing you now!")
        else:
            await self._safe_edit(
                interaction,
                "✅ Yay! I've reached out to them anonymously. If they're in too, I'll introduce you both, "
                "still no names until you both say yes. 🐾",
            )
        await self._advance(oid)

    @staticmethod
    async def _safe_edit(interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                # Already acknowledged (e.g. we deferred first) — edit the original.
                await interaction.edit_original_response(content=content, view=None)
            else:
                await interaction.response.edit_message(content=content, view=None)
        except discord.HTTPException:
            try:
                await interaction.followup.send(content, ephemeral=True)
            except discord.HTTPException:
                pass

    # ---- status ----------------------------------------------------------

    async def _render_status(self, interaction: discord.Interaction, user_id: int) -> None:
        listings = self._user_listings(user_id)
        lines = ["📊 **Your roommate / carpool status**\n"]
        if listings:
            lines.append("**Your open listings:**")
            for l in listings:
                lines.append(f"• {'🚗' if l['kind']=='carpool' else '🏨'} **{l['con']}** "
                             f"({'host' if l['role']=='host' else 'join'}, {_cap_str(l)})")
            lines.append("\n_Use **✏️ Edit** to tweak any of these._")
        else:
            lines.append("_No open listings yet. Tap **🛏️ Register** to make one!_")
        if user_id in set(self.store.get(OPTOUT, [])):
            lines.append("\n🚫 You're opted out of new suggestions (registering hops you back in).")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        awaiting = []
        for o in self.store.get(OFFERS, {}).values():
            if o["s1"] == "pending" and o["u1"] == user_id:
                awaiting.append((o, o["u2"]))
            elif o["s1"] == "yes" and o["s2"] == "pending" and o["u2"] == user_id:
                awaiting.append((o, o["u1"]))
        for o, other in awaiting[:5]:
            lst = self._user_listing(other, o["con"], o["kind"])
            if not lst:
                continue
            await interaction.followup.send(
                "👋 **A possible match is waiting on you** (no name yet):\n\n"
                f"{self._anon_summary(lst)}\n\nConnect?",
                view=build_offer_view(o["id"]), ephemeral=True,
            )

    # ---- safety-net sweep (immediate matching is primary) ---------------

    # Offers older than this many seconds are expired so users don't stay stuck.
    _OFFER_TTL = 72 * 3600  # 72 hours

    @tasks.loop(hours=6)
    async def sweep_loop(self) -> None:
        interval = max(1, int(self._s("roommate_match_interval_hours") or 6))
        if self.sweep_loop.hours != interval:
            self.sweep_loop.change_interval(hours=interval)
        if not self._s("roommate_enabled") or not self._adult_configured() or self._guild() is None:
            return
        try:
            # Expire stale offers so users don't stay blocked on an unanswered DM.
            now = time.time()
            for oid, o in list(self.store.get(OFFERS, {}).items()):
                if now - o.get("created_at", now) > self._OFFER_TTL:
                    log.info("Expiring stale offer %s (>72 h old)", oid)
                    await self._close_offer(oid)

            busy = {u for o in self.store.get(OFFERS, {}).values() for u in (o["u1"], o["u2"])}
            for l in self._open_listings():
                if l["user_id"] not in busy:
                    await self._match_for_listing(l["id"])
                    busy = {u for o in self.store.get(OFFERS, {}).values() for u in (o["u1"], o["u2"])}
        except Exception:
            log.exception("Roommate sweep failed")

    @sweep_loop.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ============================ slash commands =========================

    group = app_commands.Group(name="roommate", description="Find a hotel roommate or carpool for a con (18+).")

    @group.command(name="find", description="Register or update a room / carpool listing.")
    async def find(self, interaction: discord.Interaction) -> None:
        await self.hub_register(interaction)

    @group.command(name="edit", description="Edit one of your existing listings.")
    async def edit(self, interaction: discord.Interaction) -> None:
        await self.hub_edit(interaction)

    @group.command(name="browse", description="Browse open listings (names hidden).")
    async def browse(self, interaction: discord.Interaction) -> None:
        await self.hub_browse(interaction)

    @group.command(name="status", description="See your listings and any pending matches.")
    async def status(self, interaction: discord.Interaction) -> None:
        await self.hub_status(interaction)

    @group.command(name="rules", description="Show the roommate / carpool rules.")
    async def rules(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self._rules_text(), ephemeral=True)

    @group.command(name="cancel", description="Cancel one of your listings.")
    async def cancel(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        mine = self._user_listings(interaction.user.id)
        if not mine:
            await interaction.response.send_message("You have no open listings.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        select = discord.ui.Select(
            placeholder="Which listing do you want to cancel?",
            options=[discord.SelectOption(
                label=f"{'🚗' if l['kind']=='carpool' else '🏨'} {l['con']} ({l['role']})"[:100], value=l["id"]
            ) for l in mine[:25]],
        )

        async def _cb(i: discord.Interaction) -> None:
            await self._cancel_listing(i, interaction.user.id, select.values[0])

        select.callback = _cb
        view.add_item(select)
        await interaction.response.send_message("Pick the listing to cancel:", view=view, ephemeral=True)

    async def _cancel_listing(self, interaction: discord.Interaction, user_id: int, listing_id: str) -> None:
        # Acknowledge first — several store writes follow that can exceed 3s live.
        await interaction.response.defer()
        listings = dict(self._all_listings())
        lst = listings.get(listing_id)
        if not lst or lst.get("user_id") != user_id:
            await interaction.edit_original_response(content="That listing isn't yours or is already gone.", view=None)
            return
        con, kind = lst["con"], lst["kind"]
        listings.pop(listing_id, None)
        await self.store.set(LISTINGS, listings)
        archive = list(self.store.get(ARCHIVE, []))
        archive.append({**lst, "status": "cancelled", "closed_at": int(time.time())})
        await self.store.set(ARCHIVE, archive)
        offers = dict(self.store.get(OFFERS, {}))
        for oid in [k for k, o in offers.items()
                    if o["con"] == con and o["kind"] == kind and user_id in (o["u1"], o["u2"]) and not o.get("revealed")]:
            offers.pop(oid, None)
        await self.store.set(OFFERS, offers)
        await interaction.edit_original_response(
            content=f"🗑️ Cancelled your {_kind_label(kind)} listing for **{con}**.", view=None
        )

    @group.command(name="setup", description="(Staff) Post or refresh the roommate-finder hub message.")
    @is_staff()
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cid = int(self._s("roommate_hub_channel_id") or 0)
        channel = interaction.guild.get_channel(cid) if interaction.guild and cid else None
        if channel is None:
            await interaction.followup.send(
                "Set a valid channel first: `/config set roommate_hub_channel_id <channel id>`.", ephemeral=True
            )
            return
        embed = self._hub_embed()
        prev = self.store.get(HUBMSG, {})
        msg = None
        if prev.get("channel") == channel.id and prev.get("message"):
            try:
                msg = await channel.fetch_message(prev["message"])
                await msg.edit(embed=embed, view=HubView())
            except discord.HTTPException:
                msg = None
        if msg is None:
            try:
                msg = await channel.send(embed=embed, view=HubView())
            except discord.Forbidden:
                await interaction.followup.send("I can't post in that channel, check my permissions.", ephemeral=True)
                return
        await self.store.set(HUBMSG, {"channel": channel.id, "message": msg.id})
        await interaction.followup.send(f"✅ Roommate-finder hub posted in {channel.mention}.", ephemeral=True)

    @group.command(name="importcons", description="(Staff) Add cons by scanning a forum/channel's post titles.")
    @app_commands.describe(channel="Channel to scan (defaults to roommate_cons_channel_id).")
    @is_staff()
    async def importcons_cmd(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | discord.ForumChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cid = int(self._s("roommate_cons_channel_id") or 0)
        ch = channel or (interaction.guild.get_channel(cid) if interaction.guild and cid else None)
        if ch is None:
            await interaction.followup.send(
                "Point me at a channel: pass one, or set `/config set roommate_cons_channel_id <id>`.", ephemeral=True
            )
            return
        names: set[str] = set()
        try:
            for th in list(getattr(ch, "threads", []) or []):
                if th.name:
                    names.add(th.name)
            if hasattr(ch, "archived_threads"):
                async for th in ch.archived_threads(limit=None):
                    if th.name:
                        names.add(th.name)
        except discord.HTTPException:
            log.exception("importcons: failed scanning %s", ch.id)
        if not names:
            await interaction.followup.send(
                f"I scanned {ch.mention} but found no post/thread titles to import (is it a forum with posts?).",
                ephemeral=True,
            )
            return
        added = await self._merge_cons(sorted(names))
        preview = ", ".join(added[:20]) + ("..." if len(added) > 20 else "")
        await interaction.followup.send(
            f"✅ Scanned {ch.mention}: found **{len(names)}** title(s), added **{len(added)}** new con(s). "
            f"There are now **{len(self._cons())}** cons total."
            + (f"\n\n**Added:** {preview}" if added else "\n\nNothing new, the list was already up to date. 🐾"),
            ephemeral=True,
        )

    @group.command(name="stats", description="(Staff) Show roommate-finder activity.")
    @is_staff()
    async def stats_cmd(self, interaction: discord.Interaction) -> None:
        listings = self._open_listings()
        hotels = sum(1 for l in listings if l["kind"] == "hotel")
        cars = sum(1 for l in listings if l["kind"] == "carpool")
        bands = ", ".join(b["label"] for b in self._age_bands()) or "none"
        out = [
            f"**Enabled:** {bool(self._s('roommate_enabled'))} · **Adult gate set:** {self._adult_configured()}",
            f"**Open listings:** {len(listings)} (🏨 {hotels} · 🚗 {cars})",
            f"**Active offers:** {len(self.store.get(OFFERS, {}))} · **Opted out:** {len(self.store.get(OPTOUT, []))}",
            f"**Archived:** {len(self.store.get(ARCHIVE, []))}",
            f"**Age bands:** {bands}",
            f"**Cons ({len(self._cons())}):** {', '.join(self._cons()) or 'none'}",
        ]
        await interaction.response.send_message("\n".join(out)[:1900], ephemeral=True)

    # ---- copy ------------------------------------------------------------

    def _rules_text(self) -> str:
        return (
            "📜 **NYFurs Roommate & Carpool Rules**\n\n"
            "🧪 **Heads up — this finder is in beta.** It's still being polished, so you might hit the odd "
            "rough edge. Please flag anything weird to staff!\n\n"
            "**General**\n"
            "• We love seeing fluffs go to cons together, but **this server is not responsible** for any wrongful "
            "planning, incidents, or theft. Use your best judgement, your safety comes first.\n\n"
            "**Joining**\n"
            "• Do **not** share personal info or your address here.\n"
            "• Room and carpool with folks you trust, know, or can verify are a trusted stranger.\n\n"
            "**Hosting**\n"
            "• **18+** to host a hotel, **21+** to host a carpool, **16+** to carpool (with parental consent).\n"
            "• Don't share personal info or addresses here, make a private group chat for that.\n"
            "• Name your space limit so nobody's overcrowded (mind the luggage and fursuit room in a car!).\n\n"
            "**Fine print**\n"
            "• As the host you're in charge of your group and responsible for transport and hotel rentals.\n"
            "• Say how many fluffs you can fit (e.g. \"car holds 5, mind the trunk!\").\n"
            "• Coordinate ahead of time: the days you're staying and the hotel you're sharing.\n"
            "• These posts are for **gathering interest only**, take further planning to a DM or private group chat.\n"
            "• Hosts have the **full right to decline** anyone they're not comfy with, please don't argue. 🐾\n"
        )

    def _hub_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🛏️🚗 NYFurs Con Roommate & Carpool Finder",
            description=(
                "🧪 **Beta:** This finder is brand new and still being polished — things may be a little wobbly. "
                "If anything acts up, please let staff know! 🐾\n\n"
                "Heading to a con? I can privately pair you with another NYFurs fluff to **share a hotel room** or "
                "**carpool**, safely and anonymously. 🐾\n\n"
                "**How it works**\n"
                "1. **Register** your room or ride: hotel or carpool, hosting or joining, con, which hotel, "
                "headcount, age range, dates and details.\n"
                "2. I sniff around for a compatible buddy and **reach out the moment I find one**, sharing their "
                "*answers, never their name*.\n"
                "3. You each tap **Yes / Pass**. Hosts have the final say and can decline freely.\n"
                "4. Only when you **both say yes** do I introduce you, then take it to DMs.\n\n"
                "🎂 **Age matching:** you pick the age range(s) you want, and matches respect both sides.\n"
                "🔞 **18+ only** (21+ to host a carpool). 🔒 **No names until you both agree.**\n"
                "🚫 **Interest only:** keep addresses and personal info out, coordinate privately after matching.\n\n"
                "Tap a button to begin, and give **📜 Rules** a read first! 👇"
            ),
            color=discord.Color.blurple(),
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Roommates command error", exc_info=error)
            msg = "Something went wonky with that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roommates(bot))
