"""Tests for the unattended sweep.

The budget guard is the safety-critical part: it is the only thing standing
between a misconfigured cron entry and an unbounded Apify bill. It gets tested
properly rather than eyeballed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vingo_scraper.apify.client import ApifyRunError
from vingo_scraper.apify.runner import IngestResult
from vingo_scraper.extract.schema import ExtractionMethod, Listing
from vingo_scraper.pipeline import sweep as sw


def make_listing(i: int) -> Listing:
    return Listing(
        fb_listing_id=str(i),
        listing_url=f"https://www.facebook.com/marketplace/item/{i}/",
        title=f"Item {i}",
        extraction_method=ExtractionMethod.APIFY,
    )


def fake_ingest(*, listings=5, cost=0.062, error=None, raises=None):
    """Build a stand-in for runner.ingest()."""
    calls = []

    async def _ingest(**kwargs):
        calls.append(kwargs)
        if raises:
            raise raises
        r = IngestResult()
        r.listings = [make_listing(i) for i in range(listings)]
        r.items_returned = listings
        r.created = listings
        r.images_stored = listings * 2
        r.cost_usd = cost
        r.error = error
        return r

    _ingest.calls = calls
    return _ingest


@pytest.fixture
def no_spend(monkeypatch):
    """Database reports zero spend so far today."""
    async def _spend(_since):
        return 0.0
    monkeypatch.setattr(sw, "spend_since", _spend)


# --------------------------------------------------------------------------
# grid
# --------------------------------------------------------------------------

def test_build_cells_is_full_cross_product():
    cells = sw.build_cells(["mumbai", "pune"], ["a", "b", "c"])
    assert len(cells) == 6
    assert ("mumbai", "a") in cells
    assert ("pune", "c") in cells


def test_estimated_cost_drops_without_details():
    """Details are the expensive half, whichever actor is configured."""
    with_d = sw.estimated_cell_cost(100, True)
    without = sw.estimated_cell_cost(100, False)
    listing, detail = sw.actor_pricing()
    assert with_d == pytest.approx(100 * (listing + detail))
    assert without == pytest.approx(100 * listing)
    assert without < with_d


def test_cost_estimate_follows_the_configured_actor():
    """A pricing constant that does not move with the actor is a money bug.

    A single global kept the legacy actor's $0.0062 after the switch to
    curious_coder, so the console quoted $0.0620 for a run that really cost
    about $0.015 -- high enough to make the budget look nearly spent.
    """
    assert sw.estimated_cell_cost(
        100, True, actor_id="curious_coder/facebook-marketplace"
    ) == pytest.approx(0.15)
    assert sw.estimated_cell_cost(
        100, True, actor_id="apify/facebook-marketplace-scraper"
    ) == pytest.approx(1.24)


def test_unknown_actor_estimates_high_not_low():
    """An unknown actor must over-estimate: stop early, never overspend."""
    unknown = sw.estimated_cell_cost(100, True, actor_id="someone/brand-new")
    known = sw.estimated_cell_cost(
        100, True, actor_id="curious_coder/facebook-marketplace"
    )
    assert unknown > known


def test_automation_grid_starts_in_bengaluru():
    """The spec says Bengaluru first; INDIA_CITIES is ordered Mumbai-first."""
    cells = sw.automation_cells()
    assert cells, "automation grid must not be empty"
    assert {c for c, _ in cells} == {"bangalore"}
    assert len(cells) < len(sw.build_cells())


# --------------------------------------------------------------------------
# the budget guard
# --------------------------------------------------------------------------

async def test_runs_every_cell_when_budget_allows(monkeypatch, no_spend):
    ing = fake_ingest()
    monkeypatch.setattr(sw, "ingest", ing)
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 100.0)

    r = await sw.run_sweep(per_cell=5, cities=["mumbai", "pune"], categories=["a", "b"])

    assert len(ing.calls) == 4
    assert len(r.cells) == 4
    assert r.budget_stopped is False
    assert r.listings == 20


async def test_stops_before_exceeding_cap(monkeypatch, no_spend):
    """The guard must refuse to START a cell it cannot afford."""
    ing = fake_ingest(cost=1.0)
    monkeypatch.setattr(sw, "ingest", ing)
    # est per cell = 100 * 0.0124 = $1.24; cap 3.0 allows 2 cells, not 3.
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 3.0)

    r = await sw.run_sweep(
        per_cell=100, cities=["a", "b", "c", "d"], categories=["x"]
    )

    assert r.budget_stopped is True
    assert len(ing.calls) < 4
    assert any(c.skipped for c in r.cells)


async def test_existing_spend_counts_against_cap(monkeypatch):
    """Spend from earlier runs today must be honoured, not ignored.

    This is why spend is read from the database rather than tracked in memory:
    four cron runs a day must share one budget.
    """
    # Headroom deliberately smaller than any one cell can cost, so the
    # assertion holds whatever the configured actor charges.
    async def _spend(_since):
        return 24.99
    monkeypatch.setattr(sw, "spend_since", _spend)
    ing = fake_ingest()
    monkeypatch.setattr(sw, "ingest", ing)
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 25.0)

    r = await sw.run_sweep(per_cell=100, cities=["a"], categories=["x"])

    assert ing.calls == []          # nothing ran
    assert r.budget_stopped is True


async def test_already_exhausted_returns_immediately(monkeypatch):
    async def _spend(_since):
        return 99.0
    monkeypatch.setattr(sw, "spend_since", _spend)
    ing = fake_ingest()
    monkeypatch.setattr(sw, "ingest", ing)
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 25.0)

    r = await sw.run_sweep(per_cell=10, cities=["a", "b"], categories=["x"])

    assert ing.calls == []
    assert r.budget_stopped is True
    assert r.cells == []


async def test_ignore_budget_overrides(monkeypatch):
    async def _spend(_since):
        return 999.0
    monkeypatch.setattr(sw, "spend_since", _spend)
    ing = fake_ingest()
    monkeypatch.setattr(sw, "ingest", ing)
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 1.0)

    # Already-exhausted check still fires first; that is intentional.
    r = await sw.run_sweep(
        per_cell=10, cities=["a"], categories=["x"], stop_on_budget=False
    )
    assert r.budget_stopped is True


async def test_dry_run_skips_spend_check(monkeypatch):
    """A dry run costs nothing, so the cap must not block it."""
    called = False

    async def _spend(_since):
        nonlocal called
        called = True
        return 999.0

    monkeypatch.setattr(sw, "spend_since", _spend)
    ing = fake_ingest(cost=0.0)
    monkeypatch.setattr(sw, "ingest", ing)
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 1.0)

    r = await sw.run_sweep(
        per_cell=10, cities=["a"], categories=["x"], dry_run=True
    )

    assert called is False
    assert len(ing.calls) == 1
    assert r.budget_stopped is False


# --------------------------------------------------------------------------
# failure isolation
# --------------------------------------------------------------------------

async def test_one_dead_cell_does_not_stop_the_sweep(monkeypatch, no_spend):
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 100.0)
    good = fake_ingest()
    calls = []

    async def flaky(**kwargs):
        calls.append(kwargs)
        if kwargs["location"] == "delhi":
            raise ApifyRunError("run status=FAILED")
        return await good(**kwargs)

    monkeypatch.setattr(sw, "ingest", flaky)
    r = await sw.run_sweep(
        per_cell=5, cities=["mumbai", "delhi", "pune"], categories=["x"]
    )

    assert len(r.cells) == 3               # all attempted
    assert len(r.failed_cells) == 1
    assert r.failed_cells[0].city == "delhi"
    assert r.listings == 10                # the other two still landed


async def test_unexpected_exception_is_contained(monkeypatch, no_spend):
    monkeypatch.setattr(sw.settings, "daily_spend_cap_usd", 100.0)
    monkeypatch.setattr(sw, "ingest", fake_ingest(raises=ValueError("boom")))

    r = await sw.run_sweep(per_cell=5, cities=["a", "b"], categories=["x"])

    assert len(r.cells) == 2
    assert all("ValueError" in c.error for c in r.cells)


# --------------------------------------------------------------------------
# exit codes -- cron and systemd alert on these
# --------------------------------------------------------------------------

def _result(cells):
    r = sw.SweepResult(started_at=datetime.now(UTC))
    r.cells = cells
    r.finished_at = datetime.now(UTC)
    return r


def test_exit_code_success():
    assert sw.exit_code(_result([sw.CellResult("a", "x", listings=5)])) == 0


def test_exit_code_distinguishes_empty_from_broken():
    """'Found nothing' and 'everything failed' need opposite responses."""
    empty = _result([sw.CellResult("a", "x", listings=0)])
    broken = _result([sw.CellResult("a", "x", error="ApifyRunError")])

    assert sw.exit_code(empty) == 5     # supply signal
    assert sw.exit_code(broken) == 2    # genuine failure
    assert sw.exit_code(empty) != sw.exit_code(broken)


def test_exit_code_partial_failure():
    r = _result([
        sw.CellResult("a", "x", listings=5),
        sw.CellResult("b", "x", error="boom"),
    ])
    assert sw.exit_code(r) == 3


def test_exit_code_budget_stop_with_nothing_done():
    r = _result([sw.CellResult("a", "x", skipped="daily spend cap")])
    r.budget_stopped = True
    assert sw.exit_code(r) == 4


# --------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------

def test_lock_blocks_a_second_holder(tmp_path):
    from vingo_scraper.pipeline.lock import AlreadyRunning, single_instance

    path = tmp_path / "sweep.lock"
    with single_instance(path):
        with pytest.raises(AlreadyRunning):
            with single_instance(path):
                pass


def test_lock_is_released_after_the_block(tmp_path):
    from vingo_scraper.pipeline.lock import single_instance

    path = tmp_path / "sweep.lock"
    with single_instance(path):
        pass
    with single_instance(path):      # must not raise
        pass


def test_lock_released_even_when_body_raises(tmp_path):
    from vingo_scraper.pipeline.lock import single_instance

    path = tmp_path / "sweep.lock"
    with pytest.raises(ValueError):
        with single_instance(path):
            raise ValueError("boom")
    with single_instance(path):      # must still be acquirable
        pass
