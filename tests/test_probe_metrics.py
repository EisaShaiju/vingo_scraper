"""Tests for Phase 0 probe metrics and the go/no-go thresholds.

The recommendation gates real spend, so its boundaries are tested explicitly
rather than eyeballed on the day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vingo_scraper.extract.schema import ExtractionMethod, Listing, ListingImage
from vingo_scraper.pipeline.probe import CellMetrics, ProbeReport, summarize


def make_listing(
    idx: int,
    *,
    age_days: int | None = 5,
    price_minor: int | None = 100_000,
    photo: bool = True,
    description: bool = True,
) -> Listing:
    posted = (
        None if age_days is None else datetime.now(UTC) - timedelta(days=age_days)
    )
    return Listing(
        fb_listing_id=str(idx),
        listing_url=f"https://www.facebook.com/marketplace/item/{idx}/",
        title=f"Item {idx}",
        description="A description" if description else None,
        price_minor=price_minor,
        posted_at=posted,
        images=[ListingImage(source_url="https://x/i.jpg")] if photo else [],
        extraction_method=ExtractionMethod.GRAPHQL,
    )


def test_summarize_empty():
    m = summarize([], city="mumbai", category="electronics")
    assert m.total == 0
    assert m.median_price_inr is None
    assert m.pct_with_photo == 0.0


def test_freshness_split():
    listings = [make_listing(i, age_days=5) for i in range(3)]
    listings += [make_listing(i + 10, age_days=90) for i in range(2)]
    listings += [make_listing(i + 20, age_days=None) for i in range(2)]

    m = summarize(listings, city="mumbai", category="electronics")
    assert m.total == 7
    assert m.fresh_30d == 3
    assert m.unknown_age == 2


def test_median_price_excludes_free_and_missing():
    """Free items and unreadable prices must not drag the median to zero."""
    listings = [
        make_listing(1, price_minor=100_000),   # Rs 1,000
        make_listing(2, price_minor=300_000),   # Rs 3,000
        make_listing(3, price_minor=500_000),   # Rs 5,000
        make_listing(4, price_minor=0),         # free
        make_listing(5, price_minor=None),      # unreadable
    ]
    m = summarize(listings, city="mumbai", category="furniture")
    assert m.median_price_inr == 3_000.0
    # pct_with_price counts free as a known price; only None is missing.
    assert m.pct_with_price == 80.0


def test_completeness_percentages():
    listings = [make_listing(i, photo=(i < 3), description=(i < 1)) for i in range(4)]
    m = summarize(listings, city="pune", category="appliances")
    assert m.pct_with_photo == 75.0
    assert m.pct_with_description == 25.0


def _report(cells: list[CellMetrics], total: int, fresh: int) -> ProbeReport:
    return ProbeReport(
        started_at=datetime.now(UTC).isoformat(),
        cities=["mumbai"],
        categories=["electronics"],
        cells=cells,
        total_listings=total,
        total_fresh=fresh,
    )


def test_recommendation_no_go_on_zero():
    assert _report([], 0, 0).recommendation().startswith("NO-GO")


def test_recommendation_no_go_when_too_thin():
    cells = [CellMetrics(city="mumbai", category=f"c{i}") for i in range(5)]
    rec = _report(cells, total=25, fresh=20).recommendation()  # 5 per cell
    assert rec.startswith("NO-GO")
    assert "price-intelligence" in rec


def test_recommendation_marginal_on_low_freshness():
    """Plenty of listings but mostly stale is still not a green light."""
    cells = [CellMetrics(city="mumbai", category=f"c{i}") for i in range(5)]
    rec = _report(cells, total=500, fresh=50).recommendation()  # 100/cell, 10% fresh
    assert rec.startswith("MARGINAL")


def test_recommendation_go_on_healthy_density():
    cells = [CellMetrics(city="mumbai", category=f"c{i}") for i in range(5)]
    rec = _report(cells, total=500, fresh=300).recommendation()  # 100/cell, 60% fresh
    assert rec.startswith("GO")


def test_recommendation_blocked_when_all_errored():
    """No data because of login failure must not read as 'no inventory'."""
    report = _report([], 0, 0)
    report.errors = ["mumbai/electronics: LoginRequiredError"]
    rec = report.recommendation()
    assert rec.startswith("BLOCKED")
    assert "NO-GO" not in rec


def test_table_renders_and_flags_fallback():
    cells = [
        CellMetrics(
            city="mumbai",
            category="electronics",
            total=42,
            fresh_30d=30,
            median_price_inr=12500.0,
            pct_with_photo=95.0,
            used_fallback=True,
        )
    ]
    report = _report(cells, 42, 30)
    report.fallback_cells = 1
    table = report.to_table()
    assert "mumbai" in table
    assert "12,500" in table
    assert "*" in table
    assert "DOM extraction" in table
