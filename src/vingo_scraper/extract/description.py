"""Pull structured fields out of freeform listing descriptions.

Marketplace has no structured fields for most of what a buyer needs to know --
storage size, battery health, whether the box is included, why it is being
sold. Sellers type it into the description in whatever form they like, so this
is pattern matching over prose, not parsing.

Built the same way as `fb_payload.py`, and for the same reason: try several
spellings of everything, return what matched, and never assume a single form.
"128gb", "128 GB", "128gb variant" and "storage: 128" all appear in real data.

**Expected yield is low.** Measured on the 47-listing Bengaluru fixture, most
descriptions are under 60 characters. This finds what is there; it does not
invent structure that sellers never wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


@dataclass
class DescriptionFacts:
    specs: dict[str, str] = field(default_factory=dict)
    condition_notes: dict[str, str] = field(default_factory=dict)
    sale_reason: str | None = None
    urgency: str | None = None

    def to_dict(self) -> dict:
        out: dict = {}
        if self.specs:
            out["specs"] = self.specs
        if self.condition_notes:
            out["condition"] = self.condition_notes
        if self.sale_reason:
            out["sale_reason"] = self.sale_reason
        if self.urgency:
            out["urgency"] = self.urgency
        return out

    @property
    def chip_count(self) -> int:
        return (
            len(self.specs)
            + len(self.condition_notes)
            + bool(self.sale_reason)
            + bool(self.urgency)
        )


# --- specifications --------------------------------------------------------

_STORAGE = re.compile(
    r"\b(\d{2,4})\s?(gb|tb)\b(?!\s*(?:ram|of\s+ram))", re.I
)
_RAM = re.compile(r"\b(\d{1,3})\s?gb\s*(?:ram)\b|\bram\s*:?\s*(\d{1,3})\s?gb\b", re.I)
_DIMENSIONS = re.compile(
    r"\b(\d{1,4}(?:\.\d+)?)\s*(?:x|×)\s*(\d{1,4}(?:\.\d+)?)"
    r"(?:\s*(?:x|×)\s*(\d{1,4}(?:\.\d+)?))?\s*(cm|mm|inch|inches|in|ft|feet)?\b",
    re.I,
)
_SCREEN = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:inch|\")\b", re.I)
_YEAR = re.compile(r"\b(?:model|year|bought\s+in|purchased\s+in)\s*:?\s*(20\d{2})\b", re.I)
_KM_DRIVEN = re.compile(r"\b([\d,]{3,9})\s*(?:km|kms|kilometers?)\b", re.I)

COLOURS = (
    "black", "white", "silver", "gold", "grey", "gray", "blue", "red", "green",
    "purple", "pink", "yellow", "orange", "brown", "beige", "rose gold",
    "space grey", "space gray", "midnight", "starlight", "graphite", "titanium",
)

# Phrase -> (bucket, label). Ordered: the first match for a bucket wins, so put
# the more specific phrasing first.
_CONDITION_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bno\s+scratch(es)?\b|\bscratch\s*-?\s*less\b|\bscratchless\b", re.I),
     "scratches", "none"),
    (re.compile(r"\bminor\s+scratch|\bfew\s+scratch|\blight\s+scratch", re.I),
     "scratches", "minor"),
    (re.compile(r"\bscratch(es)?\b|\bscratched\b", re.I), "scratches", "present"),
    (re.compile(r"\bno\s+dent", re.I), "dents", "none"),
    (re.compile(r"\bdent(s|ed)?\b", re.I), "dents", "present"),
    (re.compile(r"\bbattery\s*(?:health|life)?\s*:?\s*(\d{2,3})\s*%", re.I),
     "battery_health", r"\1%"),
    (re.compile(r"\bbattery\s+(?:is\s+)?(?:good|excellent|fine|healthy)\b", re.I),
     "battery_health", "seller says good"),
    (re.compile(r"\b(?:battery|batry)\s+(?:is\s+)?(?:weak|poor|bad|dead)\b", re.I),
     "battery_health", "seller says poor"),
    (re.compile(r"\bunder\s+warrant|\bwarranty\s+(?:till|until|valid|remaining|left)",
                re.I), "warranty", "in warranty"),
    (re.compile(r"\bno\s+warrant|\bwarranty\s+(?:over|expired|finished)\b", re.I),
     "warranty", "expired"),
    (re.compile(r"\bwith\s+(?:original\s+)?box\b|\bbox\s+(?:included|available|present)\b"
                r"|\bbox\s+pack(ed)?\b", re.I), "box", "included"),
    (re.compile(r"\bno\s+box\b|\bwithout\s+box\b|\bbox\s+(?:not\s+available|missing)\b",
                re.I), "box", "not included"),
    (re.compile(r"\b(?:all\s+)?accessor(?:y|ies)\s+(?:included|available)\b"
                r"|\bwith\s+charger\b|\bcharger\s+included\b", re.I),
     "accessories", "included"),
    (re.compile(r"\bno\s+(?:charger|accessor)", re.I), "accessories", "not included"),
    (re.compile(r"\bbill\s+(?:available|included|present)\b|\bwith\s+bill\b", re.I),
     "bill", "available"),
    (re.compile(r"\bno\s+bill\b|\bbill\s+(?:not\s+available|lost|missing)\b", re.I),
     "bill", "not available"),
    (re.compile(r"\bsingle\s+hand\s+used\b|\bfirst\s+owner\b|\b1st\s+owner\b", re.I),
     "ownership", "first owner"),
    (re.compile(r"\bsecond\s+owner\b|\b2nd\s+owner\b", re.I), "ownership", "second owner"),
    (re.compile(r"\bbrand\s+new\b|\bunused\b|\bseal(ed)?\s+pack\b|\bnever\s+used\b",
                re.I), "usage", "unused"),
]

_SALE_REASONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:shift|mov)ing\s+(?:to|out|abroad|overseas|city)|"
                r"\brelocat(?:ing|ion)\b", re.I), "relocating"),
    (re.compile(r"\bupgrad(?:ed|ing)\b|\bbought\s+(?:a\s+)?new\b|\bswitch(?:ed|ing)\s+to",
                re.I), "upgraded to another device"),
    (re.compile(r"\bnot\s+(?:in\s+)?use(?:d)?\b|\bno\s+longer\s+need|\bunwanted\s+gift",
                re.I), "no longer needed"),
    (re.compile(r"\burgent(?:ly)?\s+(?:sale|sell|need)|\bneed\s+(?:money|cash)\b",
                re.I), "needs funds"),
    (re.compile(r"\bclosing\s+(?:down|shop)|\bshutting\s+down\b", re.I),
     "business closing"),
]

_URGENCY = [
    (re.compile(r"\burgent(?:ly)?\b|\bimmediate(?:ly)?\s+sale\b|\bquick\s+sale\b"
                r"|\btoday\s+only\b|\bmust\s+(?:go|sell)\b", re.I), "urgent"),
    (re.compile(r"\bprice\s+(?:slightly\s+)?negotiab|\bnego\b|\bbargain\b", re.I),
     "negotiable"),
    (re.compile(r"\bfix(?:ed)?\s+price\b|\bnon[\s-]?negotiab|\bno\s+bargain", re.I),
     "fixed price"),
]


def _first_group(match: re.Match) -> str | None:
    for g in match.groups():
        if g:
            return g
    return None


def extract(title: str | None = None, description: str | None = None) -> DescriptionFacts:
    """Both fields are searched: sellers put specs in the title just as often."""
    facts = DescriptionFacts()
    desc = (description or "").strip()
    full = f"{title or ''}\n{desc}".strip()
    if not full:
        return facts

    # --- specs ------------------------------------------------------------
    if (m := _RAM.search(full)) and (val := _first_group(m)):
        facts.specs["ram"] = f"{val} GB"
    if m := _STORAGE.search(full):
        size, unit = m.group(1), m.group(2).upper()
        # Do not report RAM a second time as storage.
        if facts.specs.get("ram") != f"{size} GB":
            facts.specs["storage"] = f"{size} {unit}"
    if m := _SCREEN.search(full):
        facts.specs["screen"] = f'{m.group(1)}"'
    if m := _YEAR.search(full):
        facts.specs["year"] = m.group(1)
    if m := _KM_DRIVEN.search(full):
        facts.specs["km_driven"] = m.group(1).replace(",", "")
    if m := _DIMENSIONS.search(full):
        parts = [p for p in m.groups()[:3] if p]
        if len(parts) >= 2:
            unit = m.group(4) or ""
            facts.specs["dimensions"] = (" x ".join(parts) + f" {unit}".rstrip())
    for colour in COLOURS:
        if re.search(rf"\b{re.escape(colour)}\b", full, re.I):
            facts.specs["colour"] = colour
            break

    # --- condition nuances -------------------------------------------------
    for pattern, bucket, label in _CONDITION_PATTERNS:
        if bucket in facts.condition_notes:
            continue  # first (most specific) match wins
        if m := pattern.search(full):
            facts.condition_notes[bucket] = (
                m.expand(label) if "\\1" in label else label
            )

    # --- why they are selling, and how urgently ---------------------------
    for pattern, label in _SALE_REASONS:
        if pattern.search(full):
            facts.sale_reason = label
            break
    for pattern, label in _URGENCY:
        if pattern.search(full):
            facts.urgency = label
            break

    return facts


def extract_all(listings) -> dict:
    """Attach `.description_facts` to each listing; return a coverage summary.

    The summary exists because the honest answer to "how much structure is in
    these descriptions" is a measurement, not a promise.
    """
    with_any = 0
    for item in listings:
        facts = extract(
            getattr(item, "title", None), getattr(item, "description", None)
        )
        item.description_facts = facts
        with_any += facts.chip_count > 0
    summary = {
        "listings": len(listings),
        "with_structured_facts": with_any,
        "pct": round(100 * with_any / len(listings), 1) if listings else 0.0,
    }
    log.info("description.extracted", **summary)
    return summary
