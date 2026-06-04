"""Tiny, self-contained classifier for new-member commission soliciting.

This is a plain logistic-regression text classifier (bag of words + bigrams +
a few engineered flags) trained on the labeled examples in commission_dataset.py.
It is NOT an LLM and makes no network calls. Training is bounded (a fixed number
of passes over ~200 short examples) and happens exactly once, cached in memory —
so the runtime cost is a fraction of a second at startup and microseconds per
check thereafter. There is no background work and nothing runs continuously.

Decision (`is_commission_ad`): deterministic guards first — questions about the
rules are never flagged and blatant sell-phrases are always flagged — and the
trained model decides the fuzzy middle. Built to minimize false positives.
"""

from __future__ import annotations

import math
import re

from commission_dataset import NEGATIVES, POSITIVES

# Blatant first-person selling — always flagged (high precision).
STRONG = (
    "commissions are open", "commissions open", "comms open", "comms are open",
    "open for commissions", "open for comms", "taking commissions", "taking comms",
    "accepting commissions", "accepting comms", "selling commissions",
    "commission slots", "comm slots", "buy my art", "art for sale", "commission me",
    "dm me for comms", "dm me for commissions", "dm for commissions",
)
# If it reads like a question/asking-about-the-rules, it's never flagged.
QUESTION = (
    "allow", "can i", "could i", "am i", "is it", "are we", "are comm", "do you allow",
    "permit", "rule", "ok to", "okay to", "able to", "is this", "where can",
    "what about", "is there", "how much", "how do", "who's", "whos", "etiquette",
)

_PAY_PLATFORMS = ("paypal", "ko-fi", "kofi", "venmo", "cashapp", "cash app", "throne")
_SOLICIT = ("dm me", "dms open", "dm's open", "pm me", "hmu", "hit me up", "inbox me",
            "msg me", "message me", "dm to", "dm for")
_ART = ("commission", "comms", "comm ", "sketch", "ych", "ref sheet", "reference sheet",
        "headshot", "fullbody", "full body", "chibi", "icon", "badge", "sticker",
        "lineart", "adopt", "emote")
_BUYER = ("i want", "looking for", "looking to", "recommend", "i got", "i paid",
          "i bought", "saving up", "afford", "i bid", "i hope", "i won", "congrats",
          "i commissioned", "i need to find", "i'd love to", "i wish", "just paid",
          "just commissioned", "their", "that artist")
_FREE = ("free", "art trade", "art trades", "giveaway", "collab", "no payment",
         "no purchase", "for fun")

_WORD = re.compile(r"[a-z0-9$€£]+")


def _engineered(low: str) -> list[str]:
    feats = []
    if "$" in low:
        feats.append("e:dollar")
    if re.search(r"\$\s?\d", low) or re.search(r"\d+\s?(usd|per character|/char)", low):
        feats.append("e:price")
    if any(p in low for p in _PAY_PLATFORMS):
        feats.append("e:payment")
    if any(s in low for s in _SOLICIT):
        feats.append("e:solicit")
    if any(a in low for a in _ART):
        feats.append("e:art")
    if "?" in low:
        feats.append("e:question")
    if any(w in low for w in _FREE):
        feats.append("e:free")
    if any(w in low for w in _BUYER):
        feats.append("e:buyer")
    return feats


def featurize(text: str) -> dict[str, float]:
    low = text.lower()
    toks = _WORD.findall(low)
    feats: dict[str, float] = {}
    for t in toks:
        feats["w:" + t] = 1.0
    for a, b in zip(toks, toks[1:]):
        feats["b:" + a + "|" + b] = 1.0
    for e in _engineered(low):
        feats[e] = 1.0
    return feats


class _Model:
    """Logistic regression trained once on the bundled dataset (bounded epochs)."""

    EPOCHS = 250
    LR = 0.5
    L2 = 1e-4

    def __init__(self) -> None:
        self.w: dict[str, float] = {}
        self.trained = False

    def _prob_feats(self, feats: dict[str, float]) -> float:
        z = self.w.get("_bias", 0.0)
        for k, v in feats.items():
            z += self.w.get(k, 0.0) * v
        z = max(-60.0, min(60.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def train(self) -> None:
        data = [(featurize(t), 1) for t in POSITIVES] + [(featurize(t), 0) for t in NEGATIVES]
        for _ in range(self.EPOCHS):  # bounded — no open-ended looping
            for feats, y in data:
                err = self._prob_feats(feats) - y
                self.w["_bias"] = self.w.get("_bias", 0.0) - self.LR * err
                for k, v in feats.items():
                    cur = self.w.get(k, 0.0)
                    self.w[k] = cur - self.LR * (err * v + self.L2 * cur)
        self.trained = True

    def ensure_trained(self) -> None:
        if not self.trained:
            self.train()

    def prob(self, text: str) -> float:
        self.ensure_trained()
        return self._prob_feats(featurize(text))


_model = _Model()


def warmup() -> None:
    """Train once up front (e.g. at cog load) so the first real check is instant."""
    _model.ensure_trained()


def model_prob(text: str) -> float:
    return _model.prob(text)


def is_commission_ad(text: str, threshold: float = 0.6) -> bool:
    """True if `text` is advertising/soliciting the author's own commissions."""
    low = (text or "").lower().strip()
    if not low:
        return False
    if "?" in low and any(q in low for q in QUESTION):
        return False  # asking about the rules / buying — not advertising
    if any(p in low for p in STRONG):
        return True
    return _model.prob(text) >= threshold
