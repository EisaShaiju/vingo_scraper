"""Normalization of raw Marketplace values into the Listing contract.

Kept free of Playwright and network concerns so it is unit-testable against
frozen fixtures with zero network calls.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from vingo_scraper.extract.schema import Condition

# "₹12,500", "Rs. 12500", "INR 12,500.50", "12,500", "Free"
_PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_FREE_RE = re.compile(r"\bfree\b", re.IGNORECASE)

# FB sometimes renders large prices in short form.
_MULTIPLIERS = {"k": 1_000, "lakh": 100_000, "lac": 100_000, "l": 100_000, "cr": 10_000_000}
_SHORT_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*([\d.]+)\s*(k|lakh|lac|l|cr)\b", re.IGNORECASE
)

# Keys are in canonical form: lowercased, separators collapsed to single
# spaces. This lets one table serve both input dialects -- GraphQL sends enum
# tokens ("USED_LIKE_NEW") while the DOM renders display text
# ("Used - Like New"), and both must land on the same Condition.
_CONDITION_MAP = {
    "new": Condition.NEW,
    "brand new": Condition.NEW,
    "used like new": Condition.USED_LIKE_NEW,
    "like new": Condition.USED_LIKE_NEW,
    "used good": Condition.USED_GOOD,
    "good": Condition.USED_GOOD,
    "used fair": Condition.USED_FAIR,
    "fair": Condition.USED_FAIR,
    "used": Condition.USED_GOOD,
}

# Underscores, hyphens and runs of whitespace all collapse to one space.
_CONDITION_SEP_RE = re.compile(r"[\s_\-]+")


def parse_price_to_minor(raw: str | int | float | None) -> int | None:
    """Parse a price into paise (INR minor units).

    Integer minor units rather than floats: money in floats accumulates drift,
    and these values feed bid ranges on Vingo.

    Returns None when no price can be read -- which is meaningfully different
    from 0 (a genuinely free listing), so callers must not conflate them.
    """
    if raw is None:
        return None

    # Numeric input from GraphQL is already an amount in major units.
    if isinstance(raw, (int, float)):
        return int(round(float(raw) * 100))

    text = str(raw).strip()
    if not text:
        return None

    if _FREE_RE.search(text):
        return 0

    # Short form first ("1.2 lakh"), else it would parse as 1.2.
    if m := _SHORT_RE.search(text):
        value, unit = float(m.group(1)), m.group(2).lower()
        return int(round(value * _MULTIPLIERS[unit] * 100))

    if m := _PRICE_RE.search(text):
        digits = m.group(1).replace(",", "")
        if not digits or digits == ".":
            return None
        try:
            return int(round(float(digits) * 100))
        except ValueError:
            return None
    return None


def parse_condition(raw: str | None) -> Condition:
    """Map a condition from either dialect onto the Condition enum.

    Accepts GraphQL enum tokens ("USED_LIKE_NEW") and DOM display text
    ("Used - Like New") by canonicalizing separators before lookup.
    """
    if not raw:
        return Condition.UNKNOWN
    canonical = _CONDITION_SEP_RE.sub(" ", str(raw).strip().lower()).strip()
    return _CONDITION_MAP.get(canonical, Condition.UNKNOWN)


def parse_timestamp(raw: int | float | str | None) -> datetime | None:
    """Parse a listing timestamp.

    Two dialects in play: Facebook's own payloads carry unix seconds (sometimes
    milliseconds), while the Apify Actor sends ISO 8601 with a trailing Z
    ("2026-09-19T02:39:57.000Z"). Both must produce a tz-aware datetime, since
    freshness is what the density decision rests on.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        if not raw.isdigit():
            try:
                # fromisoformat rejects a trailing "Z" before 3.11.
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        raw = int(raw)
    value = float(raw)
    # Heuristic: anything past ~233 AD is milliseconds, not seconds.
    if value > 1e11:
        value /= 1000
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def clean_text(raw: str | None, *, limit: int | None = None) -> str | None:
    if raw is None:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip()
    if not text:
        return None
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def listing_url(fb_listing_id: str) -> str:
    return f"https://www.facebook.com/marketplace/item/{fb_listing_id}/"
