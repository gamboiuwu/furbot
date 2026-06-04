"""Join risk scoring: spot likely scam/bot accounts and warn staff.

A small, self-contained scorer (no external services, no heavy deps — fine on a
Raspberry Pi). It turns a member's account into a handful of binary signals,
combines them with learned weights into a 0–1 risk score, and remembers the
signal snapshot so it can learn from the eventual outcome:
  * verified  -> train toward "legit" (0)
  * denied / spam-kicked -> train toward "spam" (1)
Over time the weights adapt to which signals actually predict denials here,
which keeps false positives down. Sensible hand-tuned defaults make it useful
before it has learned anything.

NOTE: keep all staff/member-facing wording free of any mention of how this is
powered — it is presented purely as "security checks".
"""

from __future__ import annotations

import math
import re
import time

import discord

# Store keys.
RISK_WEIGHTS = "risk_weights"   # {feature: weight, "_bias": float}
RISK_PENDING = "risk_pending"   # {user_id: {"f": {feat: 1.0}, "t": epoch, "dmed": bool}}
RISK_JOINS = "risk_joins"       # [epoch, ...] recent joins, for raid-burst detection

_PENDING_CAP = 2000

SCAM_KEYWORDS = (
    "free nitro", "nitro free", "free gift", "giveaway", "steam gift", "steamcommunity",
    "onlyfans", "0nlyfans", "leaks", "promo", "airdrop", "crypto", "investor",
    "earn $", "make money", "@everyone", "@here",
)
LINK_RE = re.compile(r"(https?://|discord\.gg/|discord\.com/invite|t\.me/|\bwww\.)", re.I)
IMPERSONATION = (
    "discord", "moderator", "admin", "administrator", "staff", "official",
    "support", "system", "mod team", "nitro",
)
# Zero-width / bidi / control characters often used to hide or fake names.
WEIRD_UNICODE_RE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")

# feature -> default weight. Positive = more suspicious.
DEFAULT_WEIGHTS: dict[str, float] = {
    "_bias": -3.8,
    "acct_new": 1.6,            # < 7 days old
    "acct_very_new": 1.8,       # < 24 hours old (stacks with acct_new)
    "created_join_gap": 1.4,    # account created within 24h of joining
    "default_avatar": 1.1,      # no custom profile picture
    "spammer_flag": 4.5,        # Discord's own "likely spammer" flag — very strong
    "name_trailing_digits": 0.9,
    "name_random": 1.0,
    "name_has_link": 2.2,
    "name_scam_kw": 2.4,
    "name_impersonation": 1.6,
    "name_weird_unicode": 1.5,
    "raid_burst": 1.7,
}

# feature -> human-readable reason shown to staff (no mention of how it's powered).
REASONS: dict[str, str] = {
    "spammer_flag": "Account is flagged as a likely spam account",
    "acct_very_new": "Account was created in the last 24 hours",
    "acct_new": "Account is less than a week old",
    "created_join_gap": "Account was created right before joining",
    "default_avatar": "No profile picture (default avatar)",
    "name_has_link": "A link or server invite appears in their name",
    "name_scam_kw": "Scam-related wording in their name",
    "name_impersonation": "Name imitates staff / Discord / an official account",
    "name_weird_unicode": "Hidden or look-alike characters in their name",
    "name_trailing_digits": "Username ends in a long run of digits",
    "name_random": "Username looks randomly generated",
    "raid_burst": "Joined during a burst of rapid joins",
}

_WEIGHT_CLAMP = 8.0
_LR = 0.3
_L2 = 1e-3


def _looks_random(name: str) -> bool:
    """Very conservative random-string check (furry names get creative, so this
    only fires on long, vowel-starved, digit-mixed handles)."""
    s = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(s) < 9:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    vowels = sum(c in "aeiou" for c in letters)
    has_digit = any(c.isdigit() for c in s)
    return (vowels / len(letters) < 0.18) and has_digit


def extract_features(member: discord.Member, *, raid: bool = False) -> dict[str, float]:
    feats: dict[str, float] = {}
    now = discord.utils.utcnow()
    age_days = (now - member.created_at).total_seconds() / 86400
    if age_days < 7:
        feats["acct_new"] = 1.0
    if age_days < 1:
        feats["acct_very_new"] = 1.0
    if member.joined_at and (member.joined_at - member.created_at).total_seconds() < 86400:
        feats["created_join_gap"] = 1.0
    if member.avatar is None:
        feats["default_avatar"] = 1.0
    flags = getattr(member, "public_flags", None)
    if flags is not None and getattr(flags, "spammer", False):
        feats["spammer_flag"] = 1.0

    uname = member.name or ""
    blob = f"{uname} {member.display_name or ''}"
    low = blob.lower()
    if re.search(r"\d{4,}$", uname):
        feats["name_trailing_digits"] = 1.0
    if _looks_random(uname):
        feats["name_random"] = 1.0
    if LINK_RE.search(low):
        feats["name_has_link"] = 1.0
    if any(k in low for k in SCAM_KEYWORDS):
        feats["name_scam_kw"] = 1.0
    if any(k in low for k in IMPERSONATION):
        feats["name_impersonation"] = 1.0
    if WEIRD_UNICODE_RE.search(blob):
        feats["name_weird_unicode"] = 1.0
    if raid:
        feats["raid_burst"] = 1.0
    return feats


def _score(weights: dict[str, float], feats: dict[str, float]) -> float:
    z = weights.get("_bias", DEFAULT_WEIGHTS["_bias"])
    for k, v in feats.items():
        z += weights.get(k, DEFAULT_WEIGHTS.get(k, 0.0)) * v
    # Guard against overflow on extreme scores.
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def reasons_for(feats: dict[str, float]) -> list[str]:
    """Triggered reasons, ordered by default weight (strongest first)."""
    triggered = [k for k in feats if k in REASONS]
    triggered.sort(key=lambda k: DEFAULT_WEIGHTS.get(k, 0.0), reverse=True)
    return [REASONS[k] for k in triggered]


class RiskScorer:
    """Holds the learned weights (in the shared store) and the pending snapshots."""

    def __init__(self, store) -> None:
        self.store = store

    def _weights(self) -> dict[str, float]:
        w = dict(DEFAULT_WEIGHTS)
        w.update(self.store.get(RISK_WEIGHTS, {}) or {})
        return w

    def assess(self, member: discord.Member, *, raid: bool = False) -> tuple[dict[str, float], float, list[str]]:
        feats = extract_features(member, raid=raid)
        return feats, _score(self._weights(), feats), reasons_for(feats)

    async def record(self, user_id: int, feats: dict[str, float]) -> None:
        """Remember the signal snapshot so we can learn from the outcome later."""
        def mut(d: dict) -> None:
            pend = d.setdefault(RISK_PENDING, {})
            pend[str(user_id)] = {"f": feats, "t": int(time.time()), "dmed": False}
            if len(pend) > _PENDING_CAP:  # drop oldest
                for k in sorted(pend, key=lambda k: pend[k].get("t", 0))[: len(pend) - _PENDING_CAP]:
                    del pend[k]

        await self.store.update(mut)

    async def mark_dmed(self, user_id: int) -> None:
        await self.store.update(
            lambda d: d.get(RISK_PENDING, {}).get(str(user_id), {}).__setitem__("dmed", True)
            if str(user_id) in d.get(RISK_PENDING, {}) else None
        )

    async def forget(self, user_id: int) -> None:
        await self.store.update(lambda d: d.get(RISK_PENDING, {}).pop(str(user_id), None))

    async def resolve(self, user_id: int, label: int) -> None:
        """Learn from an outcome: label 0 = legit (verified), 1 = spam (denied).
        One regularized logistic-regression SGD step on the snapshot's signals."""
        pend = self.store.get(RISK_PENDING, {}).get(str(user_id))
        await self.forget(user_id)
        if not pend:
            return
        feats = pend.get("f") or {}
        w = self._weights()
        err = _score(w, feats) - label
        w["_bias"] = max(-_WEIGHT_CLAMP, min(_WEIGHT_CLAMP, w.get("_bias", 0.0) - _LR * err))
        for k, v in feats.items():
            cur = w.get(k, DEFAULT_WEIGHTS.get(k, 0.0))
            updated = cur - _LR * (err * v + _L2 * cur)
            w[k] = max(-_WEIGHT_CLAMP, min(_WEIGHT_CLAMP, updated))
        await self.store.set(RISK_WEIGHTS, w)

    async def note_join_and_check_raid(self, *, window_minutes: int, min_joins: int) -> bool:
        """Record this join and report whether we're in a raid burst."""
        now = int(time.time())
        cutoff = now - window_minutes * 60
        recent_count = 0

        def mut(d: dict) -> None:
            nonlocal recent_count
            joins = [t for t in d.get(RISK_JOINS, []) if t >= cutoff]
            joins.append(now)
            d[RISK_JOINS] = joins[-200:]
            recent_count = len(joins)

        await self.store.update(mut)
        return recent_count >= min_joins
