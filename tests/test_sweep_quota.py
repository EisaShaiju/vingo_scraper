"""Bulk sweeps: filling a target, rebalancing, stopping, and resuming.

The budget allows **one** 1,000-listing run. It takes ~40 minutes and ~$1.40,
so a crash partway through must not re-buy the cells already paid for — dedup
stops a listing being STORED twice, not BILLED twice, because the Actor charges
when it scrapes.

Every test here drives `run_quota_sweep` with an injected `run_cell`, so the
resume path is watched working without spending anything. A resume nobody has
seen succeed is not known to work.
"""

from __future__ import annotations

import pytest

from vingo_scraper.pipeline.sweep import (
    CellOutcome,
    SweepState,
    run_quota_sweep,
)

YIELDS = {"vehicles": 26.0, "electronics": 22.0, "home_goods": 7.0}
BUDGET = 5.0


def cells_that_deliver(per_page: dict[str, float] | None = None, cost=0.05):
    """A `run_cell` that returns what the yields predict, and records calls."""
    per_page = per_page or YIELDS
    calls: list[tuple[str, int]] = []

    async def run_cell(category: str, pages: int, location: str) -> CellOutcome:
        calls.append((category, pages))
        return CellOutcome(category=category, pages=pages,
                           new_rows=int(per_page.get(category, 0) * pages),
                           cost_usd=cost * pages)

    run_cell.calls = calls          # type: ignore[attr-defined]
    return run_cell


# --------------------------------------------------------------------------
# filling a target
# --------------------------------------------------------------------------

async def test_01_a_sweep_reaches_its_target() -> None:
    run_cell = cells_that_deliver()
    state = await run_quota_sweep(target=60, location="bangalore",
                                  yields=YIELDS, budget_usd=BUDGET,
                                  run_cell=run_cell)
    assert state.obtained >= 60
    assert "target met" in (state.stopped_reason or "")


async def test_02_every_cell_is_recorded_with_what_it_returned() -> None:
    run_cell = cells_that_deliver()
    state = await run_quota_sweep(target=60, location="bangalore",
                                  yields=YIELDS, budget_usd=BUDGET,
                                  run_cell=run_cell)
    assert state.completed
    assert state.obtained == sum(c.new_rows for c in state.completed)
    assert state.spent_usd == pytest.approx(sum(c.cost_usd for c in state.completed))


async def test_03_no_category_is_swept_twice() -> None:
    """Each cell is a real charge. Buying one twice in a single sweep is
    paying twice for rows dedup will then collapse."""
    run_cell = cells_that_deliver()
    await run_quota_sweep(target=200, location="bangalore", yields=YIELDS,
                          budget_usd=BUDGET, run_cell=run_cell)
    seen = [c for c, _ in run_cell.calls]        # type: ignore[attr-defined]
    assert len(seen) == len(set(seen))


# --------------------------------------------------------------------------
# stopping, and saying why
# --------------------------------------------------------------------------

async def test_04_the_budget_stops_the_sweep() -> None:
    expensive = cells_that_deliver(cost=10.0)
    state = await run_quota_sweep(target=1000, location="bangalore",
                                  yields=YIELDS, budget_usd=1.0,
                                  run_cell=expensive)
    assert "spend ceiling" in (state.stopped_reason or "")


async def test_05_exhausting_the_categories_stops_the_sweep() -> None:
    """Marketplace ran out, not us. A bigger quota would not help, and the
    message has to say so or someone will just raise the target."""
    run_cell = cells_that_deliver()
    state = await run_quota_sweep(target=10_000, location="bangalore",
                                  yields=YIELDS, budget_usd=BUDGET,
                                  run_cell=run_cell)
    reason = state.stopped_reason or ""
    assert "exhausted" in reason or "no categories left" in reason


async def test_06_a_sweep_always_ends_with_a_reason() -> None:
    """Silence is indistinguishable from a crash."""
    for target in (10, 60, 5_000):
        state = await run_quota_sweep(target=target, location="bangalore",
                                      yields=YIELDS, budget_usd=BUDGET,
                                      run_cell=cells_that_deliver())
        assert state.stopped_reason


async def test_07_a_dead_cell_does_not_wedge_the_sweep() -> None:
    """A category returning nothing must not be retried forever."""
    async def nothing(category, pages, location):
        return CellOutcome(category=category, pages=pages, new_rows=0,
                           cost_usd=0.01)

    state = await run_quota_sweep(target=500, location="bangalore",
                                  yields=YIELDS, budget_usd=BUDGET,
                                  run_cell=nothing)
    assert state.stopped_reason
    assert state.obtained == 0


# --------------------------------------------------------------------------
# resume -- what protects the single 1,000 attempt
# --------------------------------------------------------------------------

async def test_08_a_resumed_sweep_skips_what_was_already_paid_for() -> None:
    """The whole reason sweep state is persisted."""
    already = SweepState(
        target=200, location="bangalore",
        completed=[CellOutcome("vehicles", 2, new_rows=52, cost_usd=0.10)],
    )
    run_cell = cells_that_deliver()
    state = await run_quota_sweep(target=200, location="bangalore",
                                  yields=YIELDS, budget_usd=BUDGET,
                                  run_cell=run_cell, resume=already)

    assert "vehicles" not in [c for c, _ in run_cell.calls]  # type: ignore[attr-defined]
    assert state.obtained >= 52          # the earlier work is still counted


async def test_09_resuming_counts_the_earlier_spend() -> None:
    """Otherwise a resumed sweep could blow a budget it had already half
    spent, which is the expensive version of this bug."""
    already = SweepState(
        target=1000, location="bangalore",
        completed=[CellOutcome("vehicles", 3, new_rows=78, cost_usd=0.95)],
    )
    state = await run_quota_sweep(target=1000, location="bangalore",
                                  yields=YIELDS, budget_usd=1.0,
                                  run_cell=cells_that_deliver(),
                                  resume=already)
    assert "spend ceiling" in (state.stopped_reason or "")


async def test_10_state_round_trips_through_json() -> None:
    """It is stored as JSONB and read back after a restart. A field lost in
    that trip is a cell bought twice."""
    original = SweepState(
        target=250, location="bangalore",
        completed=[CellOutcome("vehicles", 2, new_rows=52, cost_usd=0.10),
                   CellOutcome("home_goods", 1, new_rows=7, cost_usd=0.05,
                               error="thin")],
        stopped_reason="target met: 59 of 250",
    )
    back = SweepState.from_dict(original.to_dict())
    assert back is not None
    assert back.target == 250
    assert back.obtained == original.obtained
    assert back.spent_usd == pytest.approx(original.spent_usd)
    assert back.done_cells == original.done_cells
    assert back.stopped_reason == original.stopped_reason
    assert back.completed[1].error == "thin"


async def test_11_an_absent_state_is_not_a_resume() -> None:
    assert SweepState.from_dict(None) is None
    assert SweepState.from_dict({}) is None


async def test_12_progress_is_reported_after_every_cell() -> None:
    """A 40-minute sweep that shows nothing until the end is indistinguishable
    from one that hung."""
    seen: list[int] = []

    async def on_progress(state: SweepState) -> None:
        seen.append(state.obtained)

    await run_quota_sweep(target=60, location="bangalore", yields=YIELDS,
                          budget_usd=BUDGET, run_cell=cells_that_deliver(),
                          on_progress=on_progress)
    assert len(seen) >= 2
    assert seen == sorted(seen)          # monotonic: never reports going backwards
