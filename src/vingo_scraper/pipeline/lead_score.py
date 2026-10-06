"""Which sellers are worth messaging first.

The scraper answers "what is on Marketplace". This answers the only question
the business actually has: **of these, who is most likely to say yes?**

Two constraints shape the whole design.

**The unit is the LISTING, not the person.** Seller identity is populated on
~6.4% of listings and phone/email on none at all -- a Facebook platform limit,
not a scraping gap (`docs/seller-data-problem.md`). So a lead is a listing, and
the contact route is the listing URL, which opens Marketplace's own composer.
Nothing here is keyed on a human.

**Every point is traceable.** The score is useless on its own -- an operator
will not work a queue of bare numbers they cannot argue with. So each rule
returns a `Signal` naming itself in plain words, and the UI shows those rather
than the total. A score nobody can question is a score nobody trusts.

Computed on read, never stored. It costs nothing to recompute, and the weights
*will* change once someone works the queue for a week and tells us what
actually converted. Storing it would mean a migration every time we learn
something.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# What we are scoring: willingness to engage, not the value of the item.
#
# A cheap phone from someone who is moving out next week is a better lead than
# a luxury sofa from someone who priced it high and will wait six months.
#
# Weights are a starting point, deliberately legible rather than tuned -- there
# is no conversion data yet to tune against. They are here, in one block, so
# the first person who gets that data can change them without reading the code.
W_PRICE_DROP_LARGE = 30   # cut more than 10%: the seller has already blinked
W_PRICE_DROP_SMALL = 18   # any cut at all
W_REASON_FORCED = 22      # moving, needs money, closing down -- a deadline
W_REASON_SOFT = 12        # upgraded, no longer needed -- willing, not pressed
W_URGENT = 20             # "urgent", "must go", "quick sale"
W_NEGOTIABLE = 10         # invites a conversation
W_FIXED_PRICE = -8        # says the opposite, in the seller's own words
W_AGING = 14              # listed a while and still unsold
W_AGING_LONG = 20
W_EFFORT = 10             # many photos: an invested seller answers messages
W_QUALITY = 10            # our own gate agreed it is a real listing

# A seller whose deadline is in their own words is the strongest thing we can
# read. These are the labels `extract.description` emits, not free text.
FORCED_REASONS = frozenset({"relocating", "needs funds", "business closing"})
SOFT_REASONS = frozenset({"upgraded to another device", "no longer needed"})

AGING_DAYS = 7
AGING_LONG_DAYS = 21


@dataclass(frozen=True)
class Signal:
    """One reason this listing scored what it did, in the operator's words."""

    label: str
    points: int
    kind: str = "neutral"  # good | bad | neutral -- drives the chip colour


@dataclass
class LeadScore:
    score: int = 0
    signals: list[Signal] = field(default_factory=list)

    @property
    def band(self) -> str:
        """Three buckets, because a queue is worked top-down, not sorted by 73
        versus 71."""
        if self.score >= 65:
            return "hot"
        if self.score >= 35:
            return "warm"
        return "cold"

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "band": self.band,
            "signals": [
                {"label": s.label, "points": s.points, "kind": s.kind}
                for s in self.signals
            ],
        }


def _age_days(posted_at: datetime | None, now: datetime) -> float | None:
    if posted_at is None:
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return (now - posted_at).total_seconds() / 86400.0


def _price_drop(history: list | None) -> tuple[int, float] | None:
    """Biggest fall from any earlier observation to the latest one.

    `ListingPriceHistory` already records a row per observed change, so this is
    reading something we collect rather than asking for new scraping. Returns
    (minor units dropped, fraction of the original) or None.
    """
    rows = [h for h in (history or [])
            if getattr(h, "price_minor", None) not in (None, 0)]
    # Sort explicitly. `Listing.price_history` is declared order_by seen_at
    # DESC, so trusting the incoming order would read every price CUT as a
    # rise and score the most motivated sellers lowest -- exactly backwards,
    # and entirely plausible-looking in the UI.
    rows.sort(key=lambda h: getattr(h, "seen_at", None) or datetime.min.replace(tzinfo=UTC))
    prices = [h.price_minor for h in rows]
    if len(prices) < 2:
        return None
    first, last = prices[0], prices[-1]
    if last >= first:
        return None
    return first - last, (first - last) / first


def score_listing(listing, *, price_history: list | None = None,
                  photo_count: int | None = None,
                  now: datetime | None = None) -> LeadScore:
    """Score one listing. Pure: no database, no clock unless you pass one.

    `listing` may be the pydantic `Listing` or the SQLAlchemy row -- this only
    ever reads attributes both of them have, via getattr, because the two
    diverge on exactly the optional fields we care about here.

    It reads only COLUMNS off the row. Relationships (`images`,
    `price_history`) must be handed in by the caller: lazy-loading one inside
    an async request raises MissingGreenlet.
    """
    now = now or datetime.now(UTC)
    out = LeadScore()

    def add(label: str, points: int, kind: str = "good") -> None:
        out.signals.append(Signal(label, points, kind))
        out.score += points

    # --- the seller has already moved on price ----------------------------
    drop = _price_drop(price_history)
    if drop:
        minor, fraction = drop
        rupees = int(minor / 100)
        if fraction > 0.10:
            add(f"Price cut {int(fraction * 100)}% (₹{rupees:,})", W_PRICE_DROP_LARGE)
        else:
            add(f"Price cut ₹{rupees:,}", W_PRICE_DROP_SMALL)

    # --- the seller said why, in their own words --------------------------
    facts = getattr(listing, "description_facts", None) or {}
    if not isinstance(facts, dict):
        facts = {}
    reason = facts.get("sale_reason")
    if reason in FORCED_REASONS:
        add(f"Seller is {reason}", W_REASON_FORCED)
    elif reason in SOFT_REASONS:
        add(f"Seller {reason}", W_REASON_SOFT)

    urgency = facts.get("urgency")
    if urgency == "urgent":
        add("Urgent sale", W_URGENT)
    elif urgency == "negotiable":
        add("Price negotiable", W_NEGOTIABLE)
    elif urgency == "fixed price":
        add("Fixed price", W_FIXED_PRICE, "bad")

    # --- it has not sold --------------------------------------------------
    age = _age_days(getattr(listing, "posted_at", None), now)
    if age is not None:
        if age >= AGING_LONG_DAYS:
            add(f"Unsold {int(age)} days", W_AGING_LONG)
        elif age >= AGING_DAYS:
            add(f"Unsold {int(age)} days", W_AGING)

    # --- the seller put work in -------------------------------------------
    # Pass `photo_count` from a caller that already has the images. Reading
    # `listing.images` off a SQLAlchemy row is a LAZY load, and under the async
    # engine that raises MissingGreenlet mid-request rather than quietly doing
    # a second query -- which emptied every tab in the console once already.
    if photo_count is None:
        photo_count = len(getattr(listing, "images", None) or [])
    if photo_count >= 5:
        add(f"{photo_count} photos", W_EFFORT)

    quality = getattr(listing, "quality_score", None)
    if isinstance(quality, int) and quality >= 70:
        add("Complete listing", W_QUALITY)

    # A listing with none of these is still a lead -- the business contacts the
    # seller either way, and a missing description is not a defect. It just
    # sorts below the ones that told us something.
    out.score = max(0, min(100, out.score))
    return out


def sort_key(scored: LeadScore) -> tuple:
    """Highest score first, then more evidence first at equal score."""
    return (-scored.score, -len(scored.signals))
