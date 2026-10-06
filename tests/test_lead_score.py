"""Who gets messaged first.

These tests encode a business judgement, not a calculation: a cheap item from
someone who is moving out next week is a better lead than an expensive one from
someone who wrote "fixed price". If the weights change, these are the
assertions that should be argued with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from vingo_scraper.pipeline.lead_score import (
    LeadScore,
    score_listing,
    sort_key,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@dataclass
class FakeListing:
    """Only the attributes the scorer reads -- it uses getattr throughout so
    it works against both the pydantic model and the SQLAlchemy row."""

    description_facts: dict | None = None
    posted_at: datetime | None = None
    images: list | None = None
    quality_score: int = 0


@dataclass
class Price:
    price_minor: int
    seen_at: datetime


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def score(listing, history=None) -> LeadScore:
    return score_listing(listing, price_history=history, now=NOW)


# --------------------------------------------------------------------------
# price cuts -- the strongest signal we hold
# --------------------------------------------------------------------------

def test_01_a_price_cut_scores() -> None:
    hist = [Price(100_000_00, days_ago(9)), Price(80_000_00, days_ago(1))]
    assert score(FakeListing(), hist).score > 0


def test_02_a_big_cut_beats_a_small_one() -> None:
    small = [Price(100_000_00, days_ago(9)), Price(97_000_00, days_ago(1))]
    big = [Price(100_000_00, days_ago(9)), Price(60_000_00, days_ago(1))]
    assert score(FakeListing(), big).score > score(FakeListing(), small).score


def test_03_a_price_RISE_is_not_a_signal() -> None:
    hist = [Price(50_000_00, days_ago(9)), Price(70_000_00, days_ago(1))]
    assert score(FakeListing(), hist).score == 0


def test_04_history_order_does_not_change_the_verdict() -> None:
    """The trap this nearly fell into.

    `Listing.price_history` is declared `order_by seen_at DESC`, so the rows
    arrive newest-first. Reading them in arrival order turns every price CUT
    into a rise -- scoring the most motivated sellers lowest, and looking
    entirely reasonable on screen.
    """
    asc = [Price(100_000_00, days_ago(9)), Price(70_000_00, days_ago(1))]
    desc = list(reversed(asc))
    assert score(FakeListing(), asc).score == score(FakeListing(), desc).score
    assert score(FakeListing(), desc).score > 0


def test_05_one_observation_is_not_a_trend() -> None:
    assert score(FakeListing(), [Price(50_000_00, days_ago(1))]).score == 0


def test_06_the_cut_is_named_in_rupees() -> None:
    hist = [Price(100_000_00, days_ago(9)), Price(60_000_00, days_ago(1))]
    labels = " ".join(s.label for s in score(FakeListing(), hist).signals)
    assert "40,000" in labels


# --------------------------------------------------------------------------
# what the seller said
# --------------------------------------------------------------------------

def test_07_a_deadline_outranks_a_preference() -> None:
    """"Relocating" is a date. "Upgraded" is a mood."""
    forced = FakeListing(description_facts={"sale_reason": "relocating"})
    soft = FakeListing(description_facts={"sale_reason": "no longer needed"})
    assert score(forced).score > score(soft).score


def test_08_needing_money_scores() -> None:
    assert score(FakeListing(description_facts={"sale_reason": "needs funds"})).score > 0


def test_09_urgent_scores_higher_than_negotiable() -> None:
    urgent = FakeListing(description_facts={"urgency": "urgent"})
    nego = FakeListing(description_facts={"urgency": "negotiable"})
    assert score(urgent).score > score(nego).score


def test_10_fixed_price_counts_against() -> None:
    """The seller told us no. Believe them."""
    plain = FakeListing(images=[1] * 6)
    fixed = FakeListing(images=[1] * 6, description_facts={"urgency": "fixed price"})
    assert score(fixed).score < score(plain).score


def test_11_a_negative_signal_is_marked_bad() -> None:
    """Drives the chip colour -- a red chip reads as a warning, not a bonus."""
    s = score(FakeListing(description_facts={"urgency": "fixed price"}))
    assert any(x.kind == "bad" for x in s.signals)


def test_12_an_unrecognised_reason_is_ignored_not_guessed() -> None:
    assert score(FakeListing(description_facts={"sale_reason": "mercury retrograde"})).score == 0


# --------------------------------------------------------------------------
# time on market
# --------------------------------------------------------------------------

def test_13_an_aging_listing_scores() -> None:
    assert score(FakeListing(posted_at=days_ago(10))).score > 0


def test_14_a_long_unsold_listing_scores_higher() -> None:
    assert score(FakeListing(posted_at=days_ago(30))).score > \
           score(FakeListing(posted_at=days_ago(10))).score


def test_15_a_fresh_listing_gets_no_aging_points() -> None:
    assert score(FakeListing(posted_at=days_ago(1))).score == 0


def test_16_a_missing_date_does_not_crash_or_guess() -> None:
    assert score(FakeListing(posted_at=None)).score == 0


def test_17_a_naive_timestamp_is_handled() -> None:
    """Fixtures and some payload paths produce tz-naive datetimes."""
    naive = datetime(2026, 9, 1, 12, 0)
    assert score(FakeListing(posted_at=naive)).score > 0


# --------------------------------------------------------------------------
# effort
# --------------------------------------------------------------------------

def test_18_a_photographed_listing_scores() -> None:
    assert score(FakeListing(images=[1] * 8)).score > score(FakeListing(images=[1, 2])).score


def test_19_quality_contributes() -> None:
    assert score(FakeListing(quality_score=90)).score > score(FakeListing(quality_score=10)).score


# --------------------------------------------------------------------------
# the business rules the score must not break
# --------------------------------------------------------------------------

def test_20_a_listing_with_no_description_is_still_a_lead() -> None:
    """A missing description is not a defect -- the business contacts that
    seller. It must sort low, never be excluded."""
    bare = FakeListing(description_facts={}, posted_at=days_ago(1))
    s = score(bare)
    assert s.score == 0
    assert s.band == "cold"


def test_21_the_score_is_bounded() -> None:
    everything = FakeListing(
        description_facts={"sale_reason": "relocating", "urgency": "urgent"},
        posted_at=days_ago(60), images=[1] * 9, quality_score=95,
    )
    hist = [Price(200_000_00, days_ago(40)), Price(90_000_00, days_ago(1))]
    assert 0 <= score(everything, hist).score <= 100


def test_22_a_negative_total_floors_at_zero() -> None:
    assert score(FakeListing(description_facts={"urgency": "fixed price"})).score == 0


def test_23_every_point_is_explained() -> None:
    """A score with no visible reason is one nobody will act on."""
    s = score(FakeListing(description_facts={"sale_reason": "relocating"}))
    assert s.score > 0 and s.signals
    assert sum(x.points for x in s.signals) == s.score


def test_24_bands_split_the_queue() -> None:
    hot = FakeListing(
        description_facts={"sale_reason": "relocating", "urgency": "urgent"},
        posted_at=days_ago(30), images=[1] * 6,
    )
    assert score(hot).band == "hot"
    assert score(FakeListing()).band == "cold"


def test_25_ties_break_on_evidence() -> None:
    """Equal scores: prefer the listing that told us more about why."""
    a, b = LeadScore(score=50, signals=[1, 2, 3]), LeadScore(score=50, signals=[1])
    assert sorted([b, a], key=sort_key)[0] is a


def test_26_it_reads_a_sqlalchemy_row_without_a_database() -> None:
    """The scorer runs against the ORM row in the API, not the pydantic model.

    A plain stand-in once passed here while the real object raised, so this
    uses the actual mapped class.
    """
    from vingo_scraper.db.models import Listing

    row = Listing(
        fb_listing_id="1", listing_url="https://x", title="t",
        description_facts={"urgency": "urgent"}, quality_score=80,
        posted_at=days_ago(30),
    )
    s = score_listing(row, price_history=[], now=NOW)
    assert s.score > 0
    assert any("Urgent" in x.label for x in s.signals)


def test_27_serialises_for_the_api() -> None:
    d = score(FakeListing(description_facts={"urgency": "urgent"})).to_dict()
    assert set(d) == {"score", "band", "signals"}
    assert set(d["signals"][0]) == {"label", "points", "kind"}


def test_28_the_scorer_never_touches_seller_identity() -> None:
    """Seller fields are null on ~94% of rows. A lead keyed on a person would
    be empty for nearly everything we hold -- see docs/seller-data-problem.md.

    Asserted by giving it a listing whose seller fields would score if they
    were read at all, and checking the score is unmoved -- not by grepping the
    source, which matches the prose explaining the rule as readily as a
    violation of it.
    """
    bare = FakeListing(description_facts={"urgency": "urgent"})
    with_seller = FakeListing(description_facts={"urgency": "urgent"})
    with_seller.seller = object()       # type: ignore[attr-defined]
    with_seller.seller_phone = "+919999999999"  # type: ignore[attr-defined]
    with_seller.fb_seller_id = "pfbid0" + "x" * 60  # type: ignore[attr-defined]

    assert score(with_seller).score == score(bare).score
    labels = " ".join(s.label for s in score(with_seller).signals).lower()
    assert "phone" not in labels and "seller id" not in labels


# --------------------------------------------------------------------------
# it must not touch a lazy relationship
# --------------------------------------------------------------------------

class ExplodingImages:
    """Stands in for a SQLAlchemy row under the async engine.

    Touching an unloaded relationship there raises MissingGreenlet rather than
    quietly issuing a second query. The console showed "No listings yet" for
    237 stored listings because of exactly this.
    """

    description_facts = {"urgency": "urgent"}
    posted_at = None
    quality_score = 0

    @property
    def images(self):
        raise RuntimeError("MissingGreenlet: lazy load attempted in async context")


def test_29_a_given_photo_count_is_used_without_touching_images() -> None:
    s = score_listing(ExplodingImages(), price_history=[], photo_count=7, now=NOW)
    assert any("7 photos" in x.label for x in s.signals)


def test_30_the_api_hands_the_count_in_rather_than_letting_it_lazy_load() -> None:
    """Pins the call site, not just the capability -- the scorer supports both
    and the endpoint must use the safe one."""
    import inspect

    from vingo_scraper.api import main
    src = inspect.getsource(main._listings)
    call = src[src.index("score_listing"):]
    assert "photo_count=" in call[:400]


def test_31_scoring_a_row_whose_relationships_are_unloaded_still_works() -> None:
    """Everything except the photo bonus must survive an unloaded row."""
    s = score_listing(ExplodingImages(), price_history=[], photo_count=0, now=NOW)
    assert s.score > 0
