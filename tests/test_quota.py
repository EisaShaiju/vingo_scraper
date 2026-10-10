"""Spreading a listing target across categories that yield differently.

These encode a spending judgement, not a calculation. The allocator decides
where real money goes, so the assertions worth arguing with are here rather
than buried in the sweep loop.

Yields throughout are **net-new listings per page** — what survives the quality
gate and the duplicate check. A category returning 24 of which 20 are already
held yields 4, and allocating against the 24 would keep buying the same rows.
"""

from __future__ import annotations

from vingo_scraper.pipeline.quota import (
    MAX_PAGES_PER_CELL,
    Plan,
    plan_cells,
    rebalance,
    should_stop,
)

# Shaped like a real supply baseline: two strong, one thin, one all but dead.
YIELDS = {
    "vehicles": 26.0,
    "electronics": 22.0,
    "home_goods": 7.0,
    "toys_games": 1.0,
}


def pages(plan: Plan) -> dict[str, int]:
    return {a.category: a.pages for a in plan.allocations}


# --------------------------------------------------------------------------
# proportional allocation
# --------------------------------------------------------------------------

def test_01_the_strongest_category_gets_the_most_pages() -> None:
    got = pages(plan_cells(100, YIELDS))
    assert got["vehicles"] >= got.get("home_goods", 0)


def test_02_a_dead_category_is_never_allocated() -> None:
    """`toys_games` yields 1 new row a page. The page costs the same as a
    productive one."""
    assert "toys_games" not in pages(plan_cells(100, YIELDS))


def test_03_an_unmeasured_category_gets_nothing() -> None:
    """We do not guess at a category nobody has probed."""
    assert "vintage_cameras" not in pages(plan_cells(100, YIELDS))


def test_04_no_cell_exceeds_the_page_ceiling() -> None:
    """One category monopolising a sweep makes the catalog lopsided, and a
    yield measured yesterday is a guess about today."""
    for n in pages(plan_cells(10_000, YIELDS)).values():
        assert n <= MAX_PAGES_PER_CELL


def test_05_a_bigger_target_buys_more_pages() -> None:
    assert plan_cells(150, YIELDS).pages >= plan_cells(50, YIELDS).pages


def test_06_a_zero_target_allocates_nothing() -> None:
    assert plan_cells(0, YIELDS).allocations == []


def test_07_no_usable_categories_allocates_nothing() -> None:
    plan = plan_cells(100, {"toys_games": 1.0})
    assert plan.allocations == []
    assert plan.shortfall == 100


# --------------------------------------------------------------------------
# honesty about what cannot be reached
# --------------------------------------------------------------------------

def test_08_an_unreachable_target_reports_a_shortfall() -> None:
    """The rule this project keeps relearning: a sweep that cannot hit its
    target says so rather than over-allocating to look successful."""
    plan = plan_cells(5_000, YIELDS)
    assert plan.shortfall > 0
    assert plan.reachable is False


def test_09_a_reachable_target_reports_none() -> None:
    plan = plan_cells(60, YIELDS)
    assert plan.shortfall == 0 and plan.reachable is True


def test_10_the_shortfall_is_the_real_gap() -> None:
    plan = plan_cells(5_000, YIELDS)
    assert plan.shortfall == plan.target - plan.expected_new


# --------------------------------------------------------------------------
# rebalancing on what actually happened
# --------------------------------------------------------------------------

def test_11_a_category_that_returned_nothing_is_dropped() -> None:
    """Proven dead, not merely thin. Paying it again buys the same empty page."""
    plan = rebalance(100, {"electronics": 0}, YIELDS)
    assert "electronics" not in pages(plan)


def test_12_the_shortfall_moves_to_cells_still_producing() -> None:
    plan = rebalance(100, {"electronics": 0}, YIELDS)
    assert "vehicles" in pages(plan)


def test_13_a_measured_result_overrides_the_prediction() -> None:
    """Trust what happened over what was forecast.

    A category predicted at 26 new rows a page that returned 3 must not then
    outrank one still delivering 22. It may still receive pages -- when the
    strong cells are capped out there is nowhere else to go -- but never MORE
    than a cell doing better. The first version of this test asserted it got
    fewer pages than before, which is not the same claim and was wrong: a
    degraded category needs more pages to yield the same rows.
    """
    after = pages(rebalance(100, {"vehicles": 3}, YIELDS))
    assert after.get("vehicles", 0) <= after.get("electronics", 0)


def test_14_meeting_the_target_stops_allocating() -> None:
    assert rebalance(100, {"vehicles": 60, "electronics": 50}, YIELDS).allocations == []


def test_15_rebalance_only_plans_the_remainder() -> None:
    plan = rebalance(100, {"vehicles": 40}, YIELDS)
    assert plan.expected_new <= 60 + max(YIELDS.values())


# --------------------------------------------------------------------------
# stop conditions -- each must say WHY
# --------------------------------------------------------------------------

def test_16_target_met_stops() -> None:
    why = should_stop(obtained=250, target=250, spent_usd=0.3,
                      budget_usd=5, cells_left=4)
    assert why and "target met" in why


def test_17_the_spend_ceiling_stops_it() -> None:
    why = should_stop(obtained=90, target=250, spent_usd=5.0,
                      budget_usd=5, cells_left=4)
    assert why and "spend ceiling" in why
    assert "90 of 250" in why          # names what it got, not just that it stopped


def test_18_exhausting_every_category_stops_it() -> None:
    """And says a bigger quota will not help, because the limit is
    Marketplace's supply rather than our ambition."""
    why = should_stop(obtained=90, target=250, spent_usd=0.3,
                      budget_usd=5, cells_left=0)
    assert why and "exhausted" in why
    assert "will not help" in why


def test_19_a_healthy_sweep_continues() -> None:
    assert should_stop(obtained=90, target=250, spent_usd=0.3,
                       budget_usd=5, cells_left=4) is None


def test_20_the_budget_check_beats_the_cells_check() -> None:
    """Out of money and out of cells at once: report the money, since that is
    the one that costs something to get wrong."""
    why = should_stop(obtained=10, target=250, spent_usd=9.0,
                      budget_usd=5, cells_left=0)
    assert why and "spend ceiling" in why


def test_21_every_stop_reason_is_a_readable_sentence() -> None:
    """A sweep that halts without saying why is indistinguishable from one
    that crashed."""
    for kwargs in (
        {"obtained": 250, "target": 250, "spent_usd": 0.1, "budget_usd": 5, "cells_left": 2},
        {"obtained": 10, "target": 250, "spent_usd": 5.0, "budget_usd": 5, "cells_left": 2},
        {"obtained": 10, "target": 250, "spent_usd": 0.1, "budget_usd": 5, "cells_left": 0},
    ):
        why = should_stop(**kwargs)
        assert why and len(why.split()) >= 4
