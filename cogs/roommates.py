"""Con Roommate Finder — privately match NYFurs members who want to share a
hotel room at a convention.

Design goals (see /roommate hub message for the user-facing version):
  • 18+ only. Every entry point is gated behind an age-verified role
    (roommate_adult_role_id, falling back to the server's verified-member role),
    plus an explicit "I am 18+" acknowledgement at registration.
  • Double-blind & consensual. Nobody's identity is ever shown until BOTH
    people say yes. When someone reaches out, the other person is notified —
    still anonymously — and chooses for themselves.
  • Everything (open room listings, in-flight offers, archives) is persisted to
    the shared WebDAV store so it survives restarts/redeploys.

We deliberately store NO sensitive PII — only the Discord user id (needed to
DM) and the room preferences the member typed. Names/handles are revealed to
each other only on mutual consent.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

from checks import NotStaff, is_staff

log = logging.getLogger("furbot.roommates")

# ---- store keys (each becomes a JSON file on WebDAV) --------------------------
LISTINGS = "roommate_listings"   # {listing_id: {...}}
OFFERS = "roommate_offers"       # {offer_id: {...}} — only in-flight offers live here
DECLINED = "roommate_declined"   # {pair_key: ts} — pairs that already passed (don't re-suggest)
OPTOUT = "roommate_optout"       # [user_id] — members who asked to stop being matched
ARCHIVE = "roommate_archive"     # [listing snapshots] — cancelled/closed listings
HUBMSG = "roommate_hub_msg"      # {"channel": id, "message": id}

PARTY_OPTIONS = [
    "2 total (just one more)", "3 total", "4 total", "5+ total",
]
BED_OPTIONS = [
    "1 bed (share)", "2 beds", "2+ beds / suite",
    "Couch / floor is fine", "Air mattress", "No preference",
]

_SAFETY = (
    "\n\n⚠️ *Stay safe:* I only share what each of you typed. Chat first, plan "
    "carefully, never send money or personal/sensitive info to someone you don't "
    "fully trust, and tell NYFurs staff if anything ever feels off."
)


def _short() -> str:
    return uuid.uuid4().hex[:8]


# ============================ persistent UI ===================================

class OfferButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rm:v1:(?P<oid>[a-f0-9]+):(?P<act>yes|no|stop)",
):
    """A Yes / Pass / Stop button on an anonymous match offer (survives restarts —
    the offer id is encoded in the custom_id and looked up in the store)."""

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
    """The persistent buttons under the channel hub message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    def _cog(interaction: discord.Interaction) -> "Roommates | None":
        return interaction.client.get_cog("Roommates")

    @discord.ui.button(label="🛏️ Register / Update", style=discord.ButtonStyle.primary, custom_id="rm:hub:register")
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._cog(interaction)
        if cog:
            await cog.hub_register(interaction)

    @discord.ui.button(label="📋 Browse rooms", style=discord.ButtonStyle.secondary, custom_id="rm:hub:browse")
    async def browse(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._cog(interaction)
        if cog:
            await cog.hub_browse(interaction)

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="rm:hub:search")
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._cog(interaction)
        if cog:
            await cog.hub_search(interaction)

    @discord.ui.button(label="📊 My status", style=discord.ButtonStyle.secondary, custom_id="rm:hub:status")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._cog(interaction)
        if cog:
            await cog.hub_status(interaction)


# ============================ registration flow ===============================

class _FieldSelect(discord.ui.Select):
    def __init__(self, field: str, placeholder: str, options: list[str], row: int) -> None:
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, row=row,
            options=[discord.SelectOption(label=o[:100], value=o[:100]) for o in options[:25]],
        )
        self.field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RegistrationView" = self.view  # type: ignore[assignment]
        view.sel[self.field] = self.values[0]
        for opt in self.options:
            opt.default = opt.value == self.values[0]
        await interaction.response.edit_message(content=view.render(), view=view)


class RegistrationView(discord.ui.View):
    def __init__(self, cog: "Roommates", user_id: int, cons: list[str]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.sel: dict[str, str | None] = {"con": None, "party": None, "bed": None}
        self.add_item(_FieldSelect("con", "Which convention?", cons, row=0))
        self.add_item(_FieldSelect("party", "How big should the room be?", PARTY_OPTIONS, row=1))
        self.add_item(_FieldSelect("bed", "Bed setup?", BED_OPTIONS, row=2))

    def render(self) -> str:
        def show(v):
            return f"**{v}**" if v else "_—_"
        return (
            "🛏️ **Set up your room search** (1/2)\n"
            f"🎪 Con: {show(self.sel['con'])}\n"
            f"👥 Group size: {show(self.sel['party'])}\n"
            f"🛏️ Bed setup: {show(self.sel['bed'])}\n\n"
            "Pick all three, then tap **Continue** for budget & notes."
        )

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.success, row=3)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form isn't yours.", ephemeral=True)
            return
        if not all(self.sel.values()):
            await interaction.response.send_message(
                "Please choose a con, group size, and bed setup first.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal(self.cog, self.user_id, dict(self.sel)))


class RegistrationModal(discord.ui.Modal):
    def __init__(self, cog: "Roommates", user_id: int, sel: dict) -> None:
        super().__init__(title="Room details & 18+ confirmation")
        self.cog = cog
        self.user_id = user_id
        self.sel = sel
        self.nights = discord.ui.TextInput(
            label="Nights / dates (optional)", required=False, max_length=100,
            placeholder="e.g. Fri–Sun, Jan 16–19",
        )
        self.budget = discord.ui.TextInput(
            label="Budget per person (optional)", required=False, max_length=60,
            placeholder="e.g. ~$120 total / split evenly",
        )
        self.notes = discord.ui.TextInput(
            label="Anything else? (smoking, sleep, vibe…)",
            style=discord.TextStyle.paragraph, required=False, max_length=400,
        )
        self.confirm = discord.ui.TextInput(
            label="Type 18 to confirm you are 18 or older",
            required=True, max_length=20, placeholder="I am 18+",
        )
        for item in (self.nights, self.budget, self.notes, self.confirm):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if "18" not in self.confirm.value:
            await interaction.response.send_message(
                "I couldn't confirm your age from that — your listing wasn't saved. "
                "Please try again and type something containing **18** to confirm you're 18+.",
                ephemeral=True,
            )
            return
        await self.cog.finalize_listing(
            interaction, self.user_id, self.sel,
            self.nights.value.strip(), self.budget.value.strip(), self.notes.value.strip(),
        )


# ============================ browse / search =================================

class _BrowseConSelect(discord.ui.Select):
    def __init__(self, cog: "Roommates", user_id: int, cons: list[str]) -> None:
        super().__init__(
            placeholder="Which con's rooms do you want to see?", min_values=1, max_values=1, row=0,
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
            options.append(discord.SelectOption(
                label=f"Room {code} · {lst['party']}"[:100],
                description=f"{lst['bed']}"[:100],
                value=lst["id"],
            ))
        super().__init__(placeholder="Express interest in a room…", min_values=1, max_values=1, row=1, options=options)
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.express_interest(interaction, self.user_id, self.values[0])


class SearchModal(discord.ui.Modal):
    def __init__(self, cog: "Roommates", user_id: int) -> None:
        super().__init__(title="Search rooms")
        self.cog = cog
        self.user_id = user_id
        self.query = discord.ui.TextInput(
            label="Con name or keyword", required=True, max_length=80,
            placeholder="e.g. Anthrocon, 2 beds, non-smoking",
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.show_search_results(interaction, self.user_id, self.query.value.strip())


# ================================= cog ========================================

class Roommates(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config
        self.store = bot.store
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.match_loop.start()

    async def cog_unload(self) -> None:
        self.match_loop.cancel()

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

    def _adult_role_id(self) -> int:
        return int(self._s("roommate_adult_role_id") or 0) or int(self.config.floofs_role_id or 0)

    def _is_adult(self, member: discord.Member | None) -> bool:
        rid = self._adult_role_id()
        if not rid or member is None:
            return False
        return any(r.id == rid for r in getattr(member, "roles", []))

    def _cons(self) -> list[str]:
        raw = self._s("roommate_cons") or ""
        parts = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
        return [p for p in parts if p]

    async def _gate(self, interaction: discord.Interaction) -> tuple[discord.Member | None, str | None]:
        """Returns (member, error_message). Member is None when the user may not use the feature."""
        if not self._s("roommate_enabled"):
            return None, "The roommate finder isn't switched on right now."
        if not self._adult_role_id():
            return None, "The roommate finder isn't fully set up yet — an admin still needs to set the 18+ role."
        member = interaction.user if isinstance(interaction.user, discord.Member) else await self._member(interaction.user.id)
        if not self._is_adult(member):
            return None, "🔞 The roommate finder is limited to age-verified (18+) members."
        return member, None

    # ---- listing storage -------------------------------------------------

    def _all_listings(self) -> dict:
        return self.store.get(LISTINGS, {})

    def _open_listings(self) -> list[dict]:
        return [l for l in self._all_listings().values() if l.get("status") == "open"]

    def _user_listings(self, user_id: int) -> list[dict]:
        return [l for l in self._open_listings() if l.get("user_id") == user_id]

    def _listing(self, user_id: int, con: str) -> dict | None:
        for l in self._open_listings():
            if l.get("user_id") == user_id and l.get("con") == con:
                return l
        return None

    def _anon_summary(self, lst: dict) -> str:
        bits = [
            f"🎪 Con: **{lst['con']}**",
            f"👥 Group size: {lst['party']}",
            f"🛏️ Bed setup: {lst['bed']}",
        ]
        if lst.get("nights"):
            bits.append(f"📅 Nights/dates: {lst['nights']}")
        if lst.get("budget"):
            bits.append(f"💵 Budget/person: {lst['budget']}")
        if lst.get("notes"):
            bits.append(f"📝 Notes: {lst['notes']}")
        return "\n".join(bits)

    @staticmethod
    def _pair_key(a: int, b: int, con: str) -> str:
        lo, hi = sorted((a, b))
        return f"{lo}-{hi}-{con}"

    async def finalize_listing(self, interaction, user_id, sel, nights, budget, notes) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        con = sel["con"]
        listings = dict(self._all_listings())
        existing = self._listing(user_id, con)
        if existing:
            lid = existing["id"]
        else:
            lid = _short()
        listings[lid] = {
            "id": lid, "user_id": user_id, "con": con,
            "party": sel["party"], "bed": sel["bed"],
            "nights": nights, "budget": budget, "notes": notes,
            "status": "open",
            "created_at": existing["created_at"] if existing else int(time.time()),
            "updated_at": int(time.time()),
        }
        await self.store.set(LISTINGS, listings)
        # Confirm + verify their DMs are open (match offers arrive by DM).
        dm_ok = True
        try:
            await interaction.user.send(
                f"✅ Your room search for **{con}** is saved. I'll quietly look for a "
                "compatible roommate and reach out here — privately, no names — if I find one. 🐾"
            )
        except discord.HTTPException:
            dm_ok = False
        msg = (
            f"✅ Saved your room search for **{con}**!\n"
            "I'll look for a compatible roommate and reach out **privately** — I never share "
            "anyone's name until you both agree."
        )
        if not dm_ok:
            msg += ("\n\n⚠️ Your DMs appear to be closed, so I can't send you matches. Please enable "
                    "**Direct Messages** from server members so I can reach you.")
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- hub button handlers --------------------------------------------

    async def hub_register(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        cons = self._cons()
        if not cons:
            await interaction.response.send_message(
                "No conventions are set up yet. An admin can add them with "
                "`/config set roommate_cons \"Anthrocon, FurDU, …\"`.", ephemeral=True,
            )
            return
        view = RegistrationView(self, interaction.user.id, cons)
        await interaction.response.send_message(view.render(), view=view, ephemeral=True)

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
            "📋 Pick a con to see who's looking for a room (names stay hidden):",
            view=view, ephemeral=True,
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

    # ---- browse / search results ----------------------------------------

    async def show_browse_results(self, interaction: discord.Interaction, user_id: int, con: str) -> None:
        listings = [l for l in self._open_listings() if l["con"] == con and l["user_id"] != user_id]
        if not listings:
            await interaction.response.edit_message(
                content=f"No one else is looking for a room at **{con}** yet — register and I'll "
                        "watch for new matches! 🐾", view=None,
            )
            return
        listings.sort(key=lambda l: l["created_at"])
        lines = [f"📋 **{len(listings)} open room search(es) for {con}** (names hidden):\n"]
        for l in listings[:25]:
            lines.append(f"• **Room {l['id'][:4].upper()}** — {l['party']}, {l['bed']}"
                         + (f", {l['budget']}" if l.get("budget") else ""))
        lines.append("\nPick one below to express interest. They'll get an anonymous heads-up with "
                     "your answers; if you both say yes, I'll introduce you.")
        view = discord.ui.View(timeout=600)
        view.add_item(_InterestSelect(self, user_id, listings))
        await interaction.response.edit_message(content="\n".join(lines)[:1900], view=view)

    async def show_search_results(self, interaction: discord.Interaction, user_id: int, query: str) -> None:
        q = query.lower()
        listings = [
            l for l in self._open_listings()
            if l["user_id"] != user_id and (q in l["con"].lower() or q in (l.get("notes") or "").lower()
                                            or q in l["bed"].lower() or q in l["party"].lower())
        ]
        if not listings:
            await interaction.response.send_message(
                f"No open rooms matched **{query}**. Try browsing, or register your own search.", ephemeral=True,
            )
            return
        listings.sort(key=lambda l: l["created_at"])
        lines = [f"🔎 **{len(listings)} match(es) for “{query}”** (names hidden):\n"]
        for l in listings[:25]:
            lines.append(f"• **Room {l['id'][:4].upper()}** — {l['con']}, {l['party']}, {l['bed']}")
        lines.append("\nPick one below to express interest (you'll need your own listing for that con).")
        view = discord.ui.View(timeout=600)
        view.add_item(_InterestSelect(self, user_id, listings))
        await interaction.response.send_message("\n".join(lines)[:1900], view=view, ephemeral=True)

    # ---- expressing interest (user-initiated reach-out) -----------------

    async def express_interest(self, interaction: discord.Interaction, user_id: int, listing_id: str) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        target = self._all_listings().get(listing_id)
        if not target or target.get("status") != "open":
            await interaction.response.send_message("That room is no longer available.", ephemeral=True)
            return
        owner = target["user_id"]
        con = target["con"]
        if owner == user_id:
            await interaction.response.send_message("That's your own listing. 🙂", ephemeral=True)
            return
        mine = self._listing(user_id, con)
        if not mine:
            await interaction.response.send_message(
                f"You need your own room search for **{con}** first so they can see what *you're* "
                "looking for. Tap **🛏️ Register / Update**, then try again.", ephemeral=True,
            )
            return
        pk = self._pair_key(user_id, owner, con)
        if pk in self.store.get(DECLINED, {}):
            await interaction.response.send_message(
                "You two have already passed on each other for this con.", ephemeral=True
            )
            return
        for o in self.store.get(OFFERS, {}).values():
            if o["con"] == con and {o["u1"], o["u2"]} == {user_id, owner}:
                await interaction.response.send_message(
                    "There's already a connection in progress between you two for this con. 🐾", ephemeral=True
                )
                return
        if owner in set(self.store.get(OPTOUT, [])):
            await interaction.response.send_message(
                "That member isn't taking new roommate suggestions right now.", ephemeral=True
            )
            return
        # Create a browse-initiated offer: the initiator has already opted in.
        oid = _short()
        offers = dict(self.store.get(OFFERS, {}))
        offers[oid] = {
            "id": oid, "con": con, "u1": user_id, "u2": owner,
            "s1": "yes", "s2": "pending", "asked1": True, "asked2": False,
            "revealed": False, "origin": "browse", "created_at": int(time.time()),
        }
        await self.store.set(OFFERS, offers)
        await interaction.response.send_message(
            "📨 Sent! They'll get an **anonymous** heads-up with your room answers. If they're in "
            "too, I'll introduce you both — and I won't share your name unless you both say yes. 🐾",
            ephemeral=True,
        )
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
        lst = self._listing(other, o["con"])
        if not lst:  # the other person cancelled — nothing to offer
            await self._close_offer(o["id"])
            return
        member = await self._member(uid)
        if member is None or not self._is_adult(member):
            return
        text = (
            "👋 Hey! I think I found you a possible **roommate match** for "
            f"**{o['con']}** here in NYFurs. Here's what they're looking for — "
            "I'm keeping names private for now:\n\n"
            f"{self._anon_summary(lst)}\n\n"
            "Want me to connect you? If you **both** say yes I'll introduce you; "
            "otherwise nobody finds out. 🐾"
        )
        try:
            await member.send(text, view=build_offer_view(o["id"]))
        except discord.HTTPException:
            log.info("Could not DM %s for roommate offer %s (DMs closed?)", uid, o["id"])

    async def _reveal(self, o: dict) -> None:
        con = o["con"]
        m1 = await self._member(o["u1"])
        m2 = await self._member(o["u2"])
        if m1:
            who = f"{m2.mention} (`{m2}`)" if m2 else f"<@{o['u2']}>"
            try:
                await m1.send(
                    f"🎉 It's a match for **{con}**! You both said yes. Meet your potential "
                    f"roommate: {who}. Say hi and sort out the details together!" + _SAFETY
                )
            except discord.HTTPException:
                pass
        if m2:
            who = f"{m1.mention} (`{m1}`)" if m1 else f"<@{o['u1']}>"
            try:
                await m2.send(
                    f"🎉 It's a match for **{con}**! You both said yes. Meet your potential "
                    f"roommate: {who}. Say hi and sort out the details together!" + _SAFETY
                )
            except discord.HTTPException:
                pass

    async def _close_offer(self, oid: str, *, declined: bool = False, matched: bool = False) -> None:
        offers = dict(self.store.get(OFFERS, {}))
        o = offers.pop(oid, None)
        await self.store.set(OFFERS, offers)
        if not o:
            return
        if declined:
            decl = dict(self.store.get(DECLINED, {}))
            decl[self._pair_key(o["u1"], o["u2"], o["con"])] = int(time.time())
            await self.store.set(DECLINED, decl)
        if matched:
            await self._set_status(o["u1"], o["con"], "matched")
            await self._set_status(o["u2"], o["con"], "matched")

    async def _set_status(self, user_id: int, con: str, status: str) -> None:
        listings = dict(self._all_listings())
        for lid, l in listings.items():
            if l.get("user_id") == user_id and l.get("con") == con and l.get("status") == "open":
                l["status"] = status
                l["updated_at"] = int(time.time())
                await self.store.set(LISTINGS, listings)
                return

    async def _optout(self, user_id: int) -> None:
        opt = list(self.store.get(OPTOUT, []))
        if user_id not in opt:
            opt.append(user_id)
            await self.store.set(OPTOUT, opt)

    # ---- responding to an offer (DM or status buttons) ------------------

    async def handle_offer(self, interaction: discord.Interaction, oid: str, act: str) -> None:
        offers = dict(self.store.get(OFFERS, {}))
        o = offers.get(oid)
        if not o:
            await self._safe_edit(interaction, "This match is no longer active.")
            return
        uid = interaction.user.id
        if uid == o["u1"]:
            side = 1
        elif uid == o["u2"]:
            side = 2
        else:
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        if o[f"s{side}"] != "pending":
            await self._safe_edit(interaction, "You've already responded to this one. 🐾")
            return

        if act == "stop":
            await self._optout(uid)
            o[f"s{side}"] = "no"
            await self.store.set(OFFERS, offers)
            await self._safe_edit(
                interaction,
                "🚫 Okay — I won't suggest roommates to you again. Run `/roommate find` anytime to opt back in.",
            )
            await self._advance(oid)
            return
        if act == "no":
            o[f"s{side}"] = "no"
            await self.store.set(OFFERS, offers)
            await self._safe_edit(
                interaction,
                "👍 No worries — I won't connect you two. Your own listing stays active for other matches.",
            )
            await self._advance(oid)
            return

        # act == "yes" — re-check age before any reveal can happen.
        member = await self._member(uid)
        if not self._is_adult(member):
            await self._safe_edit(interaction, "I can only connect age-verified (18+) members.")
            return
        o[f"s{side}"] = "yes"
        await self.store.set(OFFERS, offers)
        if o["s1"] == "yes" and o["s2"] == "yes":
            await self._safe_edit(interaction, "🎉 It's a match — check your DMs, I'm introducing you now!")
        else:
            await self._safe_edit(
                interaction,
                "✅ Great! I've reached out to them anonymously. If they're in too, I'll introduce "
                "you both — still no names until you both agree. 🐾",
            )
        await self._advance(oid)

    @staticmethod
    async def _safe_edit(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.edit_message(content=content, view=None)
        except discord.HTTPException:
            try:
                await interaction.response.send_message(content, ephemeral=True)
            except discord.HTTPException:
                pass

    # ---- status rendering ------------------------------------------------

    async def _render_status(self, interaction: discord.Interaction, user_id: int) -> None:
        listings = self._user_listings(user_id)
        lines = ["📊 **Your roommate finder status**\n"]
        if listings:
            lines.append("**Your open room searches:**")
            for l in listings:
                lines.append(f"• **{l['con']}** — {l['party']}, {l['bed']}"
                             + (f", {l['budget']}" if l.get("budget") else ""))
        else:
            lines.append("_You have no open room searches. Tap **🛏️ Register / Update** to add one._")
        if user_id in set(self.store.get(OPTOUT, [])):
            lines.append("\n🚫 You're opted out of new suggestions (registering again opts you back in).")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        # Surface any offers waiting on this member, with live buttons (in case a DM was missed).
        awaiting = []
        for o in self.store.get(OFFERS, {}).values():
            if o["s1"] == "pending" and o["u1"] == user_id:
                awaiting.append((o, o["u2"]))
            elif o["s1"] == "yes" and o["s2"] == "pending" and o["u2"] == user_id:
                awaiting.append((o, o["u1"]))
        for o, other in awaiting[:5]:
            lst = self._listing(other, o["con"])
            if not lst:
                continue
            await interaction.followup.send(
                "👋 **Someone may be a good roommate match** (name hidden):\n\n"
                f"{self._anon_summary(lst)}\n\nConnect?",
                view=build_offer_view(o["id"]), ephemeral=True,
            )

    # ---- background matcher ----------------------------------------------

    @tasks.loop(hours=24)
    async def match_loop(self) -> None:
        interval = max(1, int(self._s("roommate_match_interval_hours") or 24))
        if self.match_loop.hours != interval:
            self.match_loop.change_interval(hours=interval)
        if not self._s("roommate_enabled") or not self._adult_role_id():
            return
        guild = self._guild()
        if guild is None:
            return
        try:
            await self._run_matcher(guild)
        except Exception:
            log.exception("Roommate matcher failed")

    @match_loop.before_loop
    async def _before_match(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_matcher(self, guild: discord.Guild) -> None:
        listings = self._open_listings()
        if len(listings) < 2:
            return
        optout = set(self.store.get(OPTOUT, []))
        declined = self.store.get(DECLINED, {})
        offers = dict(self.store.get(OFFERS, {}))
        busy = {o["u1"] for o in offers.values()} | {o["u2"] for o in offers.values()}

        by_con: dict[str, list[dict]] = {}
        for l in listings:
            by_con.setdefault(l["con"], []).append(l)

        new_offers: list[str] = []
        for con, items in by_con.items():
            items.sort(key=lambda l: l["created_at"])  # older listings get first dibs
            for i in range(len(items)):
                a = items[i]
                ua = a["user_id"]
                if ua in optout or ua in busy:
                    continue
                for b in items[i + 1:]:
                    ub = b["user_id"]
                    if ub in optout or ub in busy or ua == ub:
                        continue
                    if self._pair_key(ua, ub, con) in declined:
                        continue
                    ma = await self._member(ua)
                    mb = await self._member(ub)
                    if not self._is_adult(ma) or not self._is_adult(mb):
                        continue
                    oid = _short()
                    offers[oid] = {
                        "id": oid, "con": con, "u1": ua, "u2": ub,
                        "s1": "pending", "s2": "pending", "asked1": False, "asked2": False,
                        "revealed": False, "origin": "auto", "created_at": int(time.time()),
                    }
                    new_offers.append(oid)
                    busy.add(ua)
                    busy.add(ub)
                    break  # at most one new suggestion per member per cycle
        if new_offers:
            await self.store.set(OFFERS, offers)
            for oid in new_offers:
                await self._advance(oid)
            log.info("Roommate matcher: opened %d new suggestion(s).", len(new_offers))

    # ============================ slash commands =========================

    group = app_commands.Group(name="roommate", description="Find someone to share a con hotel room with (18+).")

    @group.command(name="find", description="Register or update your room search.")
    async def find(self, interaction: discord.Interaction) -> None:
        await self.hub_register(interaction)

    @group.command(name="browse", description="Browse open room searches (names hidden).")
    async def browse(self, interaction: discord.Interaction) -> None:
        await self.hub_browse(interaction)

    @group.command(name="status", description="See your listings and any pending matches.")
    async def status(self, interaction: discord.Interaction) -> None:
        await self.hub_status(interaction)

    @group.command(name="cancel", description="Cancel one of your room searches.")
    async def cancel(self, interaction: discord.Interaction) -> None:
        member, err = await self._gate(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        mine = self._user_listings(interaction.user.id)
        if not mine:
            await interaction.response.send_message("You have no open room searches.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        select = discord.ui.Select(
            placeholder="Which search do you want to cancel?",
            options=[discord.SelectOption(label=f"{l['con']} — {l['party']}"[:100], value=l["id"]) for l in mine[:25]],
        )

        async def _cb(i: discord.Interaction) -> None:
            await self._cancel_listing(i, interaction.user.id, select.values[0])

        select.callback = _cb
        view.add_item(select)
        await interaction.response.send_message("Pick the search to cancel:", view=view, ephemeral=True)

    async def _cancel_listing(self, interaction: discord.Interaction, user_id: int, listing_id: str) -> None:
        listings = dict(self._all_listings())
        lst = listings.get(listing_id)
        if not lst or lst.get("user_id") != user_id:
            await interaction.response.edit_message(content="That listing isn't yours or is already gone.", view=None)
            return
        con = lst["con"]
        listings.pop(listing_id, None)
        await self.store.set(LISTINGS, listings)
        archive = list(self.store.get(ARCHIVE, []))
        archive.append({**lst, "status": "cancelled", "closed_at": int(time.time())})
        await self.store.set(ARCHIVE, archive)
        # Close any in-flight offers tied to this user + con (not yet matched).
        offers = dict(self.store.get(OFFERS, {}))
        for oid in [k for k, o in offers.items()
                    if o["con"] == con and user_id in (o["u1"], o["u2"]) and not o.get("revealed")]:
            offers.pop(oid, None)
        await self.store.set(OFFERS, offers)
        await interaction.response.edit_message(
            content=f"🗑️ Cancelled your room search for **{con}**.", view=None
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
                await interaction.followup.send("I can't post in that channel — check my permissions.", ephemeral=True)
                return
        await self.store.set(HUBMSG, {"channel": channel.id, "message": msg.id})
        await interaction.followup.send(f"✅ Roommate-finder hub posted in {channel.mention}.", ephemeral=True)

    @group.command(name="stats", description="(Staff) Show roommate-finder activity.")
    @is_staff()
    async def stats_cmd(self, interaction: discord.Interaction) -> None:
        listings = self._open_listings()
        by_con: dict[str, int] = {}
        for l in listings:
            by_con[l["con"]] = by_con.get(l["con"], 0) + 1
        cons = ", ".join(f"{c}: {n}" for c, n in sorted(by_con.items())) or "none"
        out = [
            f"**Enabled:** {bool(self._s('roommate_enabled'))} · **18+ role set:** {bool(self._adult_role_id())}",
            f"**Open listings:** {len(listings)} ({cons})",
            f"**Active offers:** {len(self.store.get(OFFERS, {}))}",
            f"**Opted out:** {len(self.store.get(OPTOUT, []))} · **Archived:** {len(self.store.get(ARCHIVE, []))}",
            f"**Cons configured:** {', '.join(self._cons()) or 'none'}",
        ]
        await interaction.response.send_message("\n".join(out), ephemeral=True)

    def _hub_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🛏️ NYFurs Con Roommate Finder",
            description=(
                "Heading to a convention and want to split a hotel room? I can match you with another "
                "NYFurs member looking for the same thing — **privately and safely**.\n\n"
                "**How it works**\n"
                "1. **Register** your room search — con, group size, bed setup, budget & notes.\n"
                "2. I quietly look for members wanting a similar room at the same con.\n"
                "3. When there's a possible fit, I reach out with **their answers — never their name**.\n"
                "4. Only when you **both say yes** do I introduce you. No identities are shared before that.\n\n"
                "🔒 **Privacy:** no names until both people agree. If someone reaches out to you, you're "
                "notified — still anonymously — and you choose.\n"
                "🔞 **18+ only:** limited to age-verified members.\n\n"
                "Tap a button to get started 👇"
            ),
            color=discord.Color.blurple(),
        )
        return embed

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, (NotStaff, app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🔒 This command is for staff only."
        else:
            log.exception("Roommates command error", exc_info=error)
            msg = "Something went wrong with that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roommates(bot))
