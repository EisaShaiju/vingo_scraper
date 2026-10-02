"""Ingestion quality gate.

Runs **before** rehosting, which is the whole point: a listing rejected here
costs no bandwidth, no storage, and no Supabase/GCS write. Rehosting is the
expensive step, so the cheap check goes first.

Design decision: this **flags, it does not discard**. Rows land with a
`quality_score` and stay `pending_review`, matching the invariant that nothing
auto-publishes. Discarding would make the rejects unmeasurable -- and the
rejection rate is exactly the number that tells us whether the thresholds are
right. `is_servable` already gates what a buyer could ever see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# --- thresholds, all deliberately conservative -----------------------------

MIN_PHOTOS = 2
MIN_DESCRIPTION_CHARS = 15

# Placeholder prices. These are not cheap listings, they are listings where the
# seller did not enter a price: Marketplace requires one, so people type
# whatever dismisses the field.
PLACEHOLDER_PRICES_MINOR = {
    0,            # free, or unpriced
    100,          # Rs 1
    12_345_600,   # Rs 123,456 -- the keyboard-walk placeholder
    99_999_900,   # Rs 999,999
    123_456_789_00,
}

# Repeated-digit prices (1111, 99999, 7777777) are the other common shape.
_REPDIGIT = re.compile(r"^(\d)\1{3,}$")

SPAM_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\bdm\s+for\s+(price|details)\b",
        r"\bprice\s+negotiab",          # alone, with nothing else, is noise
        r"\bresell(er)?s?\s+only\b",
        r"\b(whats?app|call)\s+only\b",
        r"\bno\s+time\s+waster",
        r"(.)\1{9,}",                   # "aaaaaaaaaa", "!!!!!!!!!!"
        r"^(.{1,12})\1{3,}$",           # short phrase repeated to pad length
    )
]


@dataclass
class QualityReport:
    score: int = 0
    passed: bool = True
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"score": self.score, "passed": self.passed, "reasons": self.reasons}


def _price_is_placeholder(price_minor: int | None) -> bool:
    """`None` is unknown, not placeholder -- they mean different things.

    A listing we could not read a price from is a parser gap worth seeing; a
    listing priced Rs 1 is a seller dodging a required field.
    """
    if price_minor is None:
        return False
    if price_minor in PLACEHOLDER_PRICES_MINOR:
        return True
    rupees = price_minor // 100
    return bool(_REPDIGIT.match(str(rupees))) and rupees >= 1111


def _looks_like_spam(text: str) -> bool:
    return any(p.search(text) for p in SPAM_PATTERNS)


def assess(listing, *, photos_known: bool = True) -> QualityReport:
    """Score 0-100 from photo count, description richness, and attributes.

    Takes the parsed `Listing`, so it runs on the dataset before any image is
    downloaded.

    `photos_known=False` when the run did not request listing details: the
    Actor then returns only the primary image, so every listing looks like a
    one-photo listing. Scoring that as low effort measures OUR cost setting
    rather than the seller's -- it flagged 24 of 24 on the first real VM run.
    Photos leave the score entirely in that case, and the result is rescaled
    over what was actually observable so runs stay comparable.
    """
    rep = QualityReport()
    photos = len(getattr(listing, "images", []) or [])
    description = (getattr(listing, "description", "") or "").strip()
    attributes = getattr(listing, "attributes", {}) or {}
    price_minor = getattr(listing, "price_minor", None)

    earned = 0
    possible = 0

    # --- photos: 40 points, the strongest single signal of effort ---------
    if photos_known:
        possible += 40
        if photos >= 5:
            earned += 40
        elif photos >= 3:
            earned += 32
        elif photos >= MIN_PHOTOS:
            earned += 22
        else:
            rep.reasons.append(f"only {photos} photo(s), want >= {MIN_PHOTOS}")
            rep.passed = False

    # --- description: 35 points -------------------------------------------
    possible += 35
    if len(description) < MIN_DESCRIPTION_CHARS:
        rep.reasons.append(
            f"description {len(description)} chars, want >= {MIN_DESCRIPTION_CHARS}"
        )
        rep.passed = False
    elif _looks_like_spam(description):
        rep.reasons.append("description matches a spam pattern")
        rep.passed = False
        earned += 5
    else:
        earned += 15
        if len(description) >= 60:
            earned += 10
        if len(description) >= 160:
            earned += 10

    # --- price sanity: 15 points ------------------------------------------
    possible += 15
    if _price_is_placeholder(price_minor):
        rep.reasons.append(f"placeholder price ({(price_minor or 0) // 100})")
        rep.passed = False
    elif price_minor is None:
        rep.reasons.append("no price could be read")
    else:
        earned += 15

    # --- structured attributes: 10 points ---------------------------------
    possible += 10
    if attributes:
        earned += min(10, 3 * len(attributes))

    # Rescale over what was observable. Without this a photo-less run caps
    # every listing at 60 and scores stop comparing across run settings.
    rep.score = max(0, min(100, round(100 * earned / possible))) if possible else 0
    if not rep.passed:
        log.info("quality.flagged", fb_id=getattr(listing, "fb_listing_id", None),
                 score=rep.score, reasons=rep.reasons)
    return rep


def assess_all(listings, *, photos_known: bool = True) -> tuple[list, dict]:
    """Attach a report to every listing; return (listings, summary).

    Nothing is dropped here -- see the module docstring. The summary is what
    makes the thresholds reviewable rather than a guess frozen in code.
    """
    flagged = 0
    for item in listings:
        report = assess(item, photos_known=photos_known)
        # Carried on the parsed object so `_persist` can write it without
        # re-deriving, and so the console can show why something was flagged.
        item.quality = report
        flagged += not report.passed
    summary = {
        "assessed": len(listings),
        "flagged": flagged,
        "passed": len(listings) - flagged,
        "mean_score": round(
            sum(x.quality.score for x in listings) / len(listings), 1
        ) if listings else 0.0,
    }
    log.info("quality.summary", **summary)
    return listings, summary
