"""How to spread a listing target across categories that yield differently.

A quota only makes sense against **net-new** listings. A category returning 24
of which 20 are already held gives 4, and an even split would keep paying for
the same rows. So every number here is measured net-new per page, from
`scripts/measure_supply.py`, never an assumption about what a category "should"
return.

Pure functions, no I/O and no clock, so the allocation can be tested offline
the way `lead_score` is. The caller owns the Actor runs, the lock and the
spending.

The hardest rule, and the one this project keeps relearning: **a sweep that
cannot reach its target must say so and stop.** Silently scraping more cells
to hide a shortfall is how "~72 listings" got shipped on a plan returning 26.
"""

from __future__ import annotations

from dataclasses import dataclass

from vingo_scraper.apify.client import LISTINGS_PER_PAGE

# No cell gets everything, however well it yields. One category monopolising a
# sweep makes the catalog lopsided, and a yield measured yesterday is a guess
# about today.
MAX_PAGES_PER_CELL = 3

# Below this, a category cannot carry a quota: the page costs the same as a
# productive one and returns almost nothing new.
MIN_YIELD_PER_PAGE = 2.0


@dataclass(frozen=True)
class Allocation:
    category: str
    pages: int

    @property
    def expected(self) -> int:
        return self.pages * LISTINGS_PER_PAGE


@dataclass
class Plan:
    allocations: list[Allocation]
    target: int
    expected_new: int
    shortfall: int = 0

    @property
    def reachable(self) -> bool:
        return self.shortfall == 0

    @property
    def pages(self) -> int:
        return sum(a.pages for a in self.allocations)


def plan_cells(
    target: int,
    yields: dict[str, float],
    *,
    max_pages_per_cell: int = MAX_PAGES_PER_CELL,
    min_yield: float = MIN_YIELD_PER_PAGE,
) -> Plan:
    """Pages per category, proportional to **measured net-new yield per page**.

    `yields` is new rows per page, from the supply baseline. A category absent
    from it gets nothing: we do not guess at a category nobody has measured.

    Reports `shortfall` when the categories cannot reach the target between
    them, rather than over-allocating to look successful.
    """
    usable = {k: y for k, y in yields.items() if y >= min_yield}
    if target <= 0 or not usable:
        return Plan([], target, 0, shortfall=max(0, target))

    ceiling = sum(y * max_pages_per_cell for y in usable.values())
    want = min(target, ceiling)

    # Proportional, largest-yield first so rounding favours the productive
    # categories rather than scattering single pages across thin ones.
    order = sorted(usable.items(), key=lambda kv: kv[1], reverse=True)
    total_yield = sum(usable.values())

    pages: dict[str, int] = {}
    expected = 0.0
    for key, yield_per_page in order:
        if expected >= want:
            break
        share = want * (yield_per_page / total_yield)
        n = min(max_pages_per_cell, max(1, round(share / yield_per_page)))
        pages[key] = n
        expected += n * yield_per_page

    # The proportional pass rounds DOWN as often as up, so it can finish short
    # of the target with capacity to spare -- 60 wanted, 55 allocated, and
    # nothing left to notice it. Top up from the strongest cell that still has
    # room, since each page costs the same whichever category buys it.
    while expected < want:
        candidates = [(k, y) for k, y in order
                      if pages.get(k, 0) < max_pages_per_cell]
        if not candidates:
            break
        key, yield_per_page = candidates[0]
        pages[key] = pages.get(key, 0) + 1
        expected += yield_per_page

    # Rounding overshoots. Trim from the WEAKEST cell first, so the categories
    # that actually deliver keep their pages.
    for key, yield_per_page in reversed(order):
        while pages.get(key, 0) > 0 and expected - yield_per_page >= want:
            pages[key] -= 1
            expected -= yield_per_page
            if pages[key] == 0:
                del pages[key]
                break

    allocations = [Allocation(k, n) for k, n in pages.items() if n > 0]
    got = int(expected)
    return Plan(allocations, target, got, shortfall=max(0, target - got))


def rebalance(
    target: int,
    obtained: dict[str, int],
    yields: dict[str, float],
    *,
    max_pages_per_cell: int = MAX_PAGES_PER_CELL,
    min_yield: float = MIN_YIELD_PER_PAGE,
) -> Plan:
    """Re-plan the remainder from what the cells ACTUALLY returned.

    `obtained` is new rows per category so far. A category that under-delivered
    has its measured yield replaced by what it really produced, so the next
    allocation goes to cells still working. A category that produced nothing is
    dropped entirely — paying it again buys the same empty page.
    """
    remaining = target - sum(obtained.values())
    if remaining <= 0:
        return Plan([], target, sum(obtained.values()))

    revised = dict(yields)
    for key, got in obtained.items():
        if got <= 0:
            revised.pop(key, None)          # proven dead, not merely thin
        else:
            # Trust what happened over what was predicted.
            revised[key] = min(revised.get(key, float(got)), float(got))

    return plan_cells(remaining, revised,
                      max_pages_per_cell=max_pages_per_cell, min_yield=min_yield)


def should_stop(
    *,
    obtained: int,
    target: int,
    spent_usd: float,
    budget_usd: float,
    cells_left: int,
) -> tuple[str, str] | None:
    """`(kind, sentence)` for why the sweep should stop, or None to continue.

    Every stop condition returns a sentence the operator can read. A sweep that
    halts without saying why is indistinguishable from one that crashed — the
    confusion that has already cost this project three debugging sessions.

    The `kind` rides alongside so the console can branch on a value instead of
    matching the prose. "Target met" and "Marketplace is empty" deserve
    opposite treatment in the UI — one is success, the other means pressing Run
    again cannot work — and a UI that tells them apart by substring breaks the
    first time a sentence is reworded.
    """
    if obtained >= target:
        return ("target", f"target met: {obtained} of {target}")
    if spent_usd >= budget_usd:
        return ("budget",
                f"spend ceiling reached: ${spent_usd:.4f} of ${budget_usd:.2f} "
                f"after {obtained} of {target} listings")
    if cells_left <= 0:
        return ("exhausted",
                f"every category exhausted at {obtained} of {target} — "
                f"Marketplace had no more, so a bigger quota will not help")
    return None
