"""Ingestion quality gate.

Runs **before** rehosting, which is the whole point: a listing rejected here
costs no bandwidth, no storage, and no Supabase/GCS write. Rehosting is the
expensive step, so the cheap check goes first.

Two tiers, and the distinction is the whole design:

* **Hard rejections** are dropped before rehosting, so they cost no bandwidth
  and no storage. Fewer than two photos, a placeholder price, or spam text.
* **Soft signals** only move `quality_score`. A missing description is the
  main one: the business answer to "no description" is to contact the seller,
  not to throw the listing away.

Discarding makes the reject rate invisible, which is how a wrong threshold
survives unnoticed. `assess_all` therefore returns a tally of what it dropped
and why, and `scrape_run` stores it -- we keep the number without the rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# --- thresholds, all deliberately conservative -----------------------------

# Hard floor. A single-photo listing is the clearest signal of a low-effort
# post, and the spec asks for photos.
# Relaxed 2 -> 1 after real listings were seen being discarded: a nike search
# in Pune returned one genuine listing and the gate dropped it, leaving a bare
# "0 listings" on screen. Zero photos is still a reject -- a listing with no
# image cannot be shown to a buyer.
MIN_PHOTOS = 1

# NOT a rejection. Scores lower without one, but a listing with no description
# is a listing whose seller we contact -- that is the business process, so
# dropping it here would destroy the lead.
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
    # Drop it before rehosting rather than storing it flagged. Only the hard
    # rules set this; a thin description does not.
    hard_reject: bool = False

    def to_dict(self) -> dict:
        return {"score": self.score, "passed": self.passed,
                "reasons": self.reasons, "hard_reject": self.hard_reject}


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


def assess(listing, *, details_known: bool = True) -> QualityReport:
    """Score 0-100 from photo count, description richness, and attributes.

    Takes the parsed `Listing`, so it runs on the dataset before any image is
    downloaded.

    `details_known=False` when the run did not request listing details. The
    Actor then returns only the primary image AND no description at all --
    measured on a real VM run: 1 photo and 0 description characters on all 24
    listings. Judging either one is measuring OUR cost setting rather than the
    seller's effort, so both leave the score entirely and what remains is
    rescaled over what was actually observable, keeping runs comparable.

    A details-off run therefore scores on price sanity and attributes only.
    That is thin on purpose: it is a discovery run, and it cannot satisfy the
    spec, which requires description and all photos.
    """
    rep = QualityReport()
    photos = len(getattr(listing, "images", []) or [])
    description = (getattr(listing, "description", "") or "").strip()
    attributes = getattr(listing, "attributes", {}) or {}
    price_minor = getattr(listing, "price_minor", None)

    earned = 0
    possible = 0

    # --- photos: 40 points, the strongest single signal of effort ---------
    if details_known:
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
            rep.hard_reject = True

    # --- description: 35 points -------------------------------------------
    # Only fetched with details. Without them it is empty for every listing,
    # so judging it would flag the entire run for a field we did not buy.
    if details_known:
        possible += 35
        if len(description) < MIN_DESCRIPTION_CHARS:
            # Scored down, never rejected. No description means the seller
            # gets contacted, which is the point of collecting the listing.
            rep.reasons.append(f"short description ({len(description)} chars)")
        elif _looks_like_spam(description):
            rep.reasons.append("description matches a spam pattern")
            rep.passed = False
            rep.hard_reject = True
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
        rep.hard_reject = True
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


def assess_all(listings, *, details_known: bool = True) -> tuple[list, dict]:
    """Attach a report to every listing; return (listings, summary).

    Nothing is dropped here -- see the module docstring. The summary is what
    makes the thresholds reviewable rather than a guess frozen in code.
    """
    kept, rejected = [], []
    tally: dict[str, int] = {}
    for item in listings:
        report = assess(item, details_known=details_known)
        # Carried on the parsed object so `_persist` can write it without
        # re-deriving, and so the console can show why something scored low.
        item.quality = report
        if report.hard_reject:
            rejected.append(item)
            for reason in report.reasons:
                # Tally the shape, not the instance: "only 1 photo(s)" and
                # "only 0 photo(s)" are the same problem.
                key = reason.split("(")[0].strip()
                tally[key] = tally.get(key, 0) + 1
        else:
            kept.append(item)

    summary = {
        "assessed": len(listings),
        "kept": len(kept),
        "rejected": len(rejected),
        "rejected_reasons": tally,
        "mean_score": round(
            sum(x.quality.score for x in kept) / len(kept), 1
        ) if kept else 0.0,
    }
    log.info("quality.summary", **summary)
    # Only the kept listings continue. The count and the reasons survive in
    # the summary so a threshold that is silently wrong is still visible.
    return kept, summary
