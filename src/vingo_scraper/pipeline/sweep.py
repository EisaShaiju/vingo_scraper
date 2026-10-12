"""Unattended sweep across every city x category cell.

This is what cron runs. `vingo-scrape` handles one query; this walks the whole
configured grid, and is built for the failure modes that only appear when
nobody is watching:

  * Apify bills per result, so an unattended loop needs a HARD spend ceiling
    that is checked against the database, not just trusted from config.
  * One dead cell must not abort the sweep.
  * A run that overruns its schedule must be skipped, not stacked.

Budget enforcement reads actual spend from `scrape_run`, so it survives
restarts, crashes, and manual runs in between.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select

from vingo_scraper.apify.client import ApifyRunError
from vingo_scraper.apify.runner import ingest
from vingo_scraper.config import (
    AUTOMATION_CATEGORIES,
    CATEGORIES,
    INDIA_CITIES,
    settings,
)
from vingo_scraper.db import models
from vingo_scraper.db.session import session_scope

log = structlog.get_logger(__name__)

# What one listing costs us, measured from Apify's billing API. Used only to
# predict whether the *next* cell would breach the cap; actual spend always
# comes from the database.
#
# Keyed by actor id for the same reason `apify/client.py` keys its input
# vocabulary: we have already swapped actors twice. A single global constant
# kept the LEGACY actor's $0.0062 after the switch to curious_coder, so the
# console quoted $0.0620 for a run that really costs ~$0.015 -- 8x high, and
# high enough to make the budget look nearly spent when it was not.
ACTOR_PRICING: dict[str, tuple[float, float]] = {
    # actor id -> (per listing, per listing-details)
    "curious_coder/facebook-marketplace": (0.0005, 0.0010),
    "apify/facebook-marketplace-scraper": (0.0062, 0.0062),
}
# Used when the configured actor is not in the table. The dearest known price,
# on purpose: an unknown actor should over-estimate and stop early rather than
# under-estimate and overspend.
FALLBACK_PRICING = (0.0062, 0.0062)


def actor_pricing(actor_id: str | None = None) -> tuple[float, float]:
    """(listing, listing-details) price for an actor, defaulting to config."""
    return ACTOR_PRICING.get(actor_id or settings.apify_actor_id, FALLBACK_PRICING)


# Kept as module attributes because older call sites and docs refer to them.
COST_PER_LISTING_USD, COST_PER_DETAIL_USD = actor_pricing()


@dataclass
class CellResult:
    city: str
    category: str
    listings: int = 0
    created: int = 0
    updated: int = 0
    images_stored: int = 0
    images_failed: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    skipped: str | None = None


@dataclass
class SweepResult:
    started_at: datetime
    finished_at: datetime | None = None
    cells: list[CellResult] = field(default_factory=list)
    spend_before_usd: float = 0.0
    budget_stopped: bool = False

    @property
    def listings(self) -> int:
        return sum(c.listings for c in self.cells)

    @property
    def created(self) -> int:
        return sum(c.created for c in self.cells)

    @property
    def images_stored(self) -> int:
        return sum(c.images_stored for c in self.cells)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.cells)

    @property
    def failed_cells(self) -> list[CellResult]:
        return [c for c in self.cells if c.error]

    def summary(self) -> str:
        done = [c for c in self.cells if not c.error and not c.skipped]
        dur = (
            (self.finished_at - self.started_at).total_seconds()
            if self.finished_at
            else 0
        )
        lines = [
            f"  cells run      : {len(done)}/{len(self.cells)}",
            f"  listings       : {self.listings}  ({self.created} new)",
            f"  images stored  : {self.images_stored}",
            f"  sweep cost     : ${self.cost_usd:.4f}",
            f"  spend today    : ${self.spend_before_usd + self.cost_usd:.4f}"
            f" / ${settings.daily_spend_cap_usd:.2f} cap",
            f"  duration       : {dur:.0f}s",
        ]
        if self.budget_stopped:
            lines.append("  STOPPED EARLY  : daily spend cap reached")
        for c in self.failed_cells:
            lines.append(f"  FAILED {c.city}/{c.category}: {c.error}")
        return "\n".join(lines)


async def spend_since(since: datetime) -> float:
    """Actual Apify spend recorded since `since`.

    Read from the database rather than tracked in memory so the cap holds
    across restarts, crashes, and any manual runs in between.
    """
    async with session_scope() as s:
        total = await s.scalar(
            select(func.coalesce(func.sum(models.ScrapeRun.cost_usd), 0.0)).where(
                models.ScrapeRun.started_at >= since
            )
        )
    return float(total or 0.0)


def _utc_midnight() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def build_cells(
    cities: list[str] | None = None, categories: list[str] | None = None
) -> list[tuple[str, str]]:
    cities = cities or list(INDIA_CITIES.values())
    categories = categories or list(CATEGORIES.keys())
    return [(c, k) for c in cities for k in categories]


def automation_cells(city: str | None = None) -> list[tuple[str, str]]:
    """The grid the console's Automation panel walks: one city, every category.

    Deliberately narrower than `build_cells()`. The client spec says start with
    Bengaluru, and `INDIA_CITIES` is ordered Mumbai-first, so the full grid both
    opened in the wrong city and spread a 3-run demo across three different
    ones. Five cells means a short sequence visibly covers a city.

    The full 25-cell grid is still what `run_sweep` uses for supply measurement.
    """
    return build_cells(
        cities=[city or INDIA_CITIES["bengaluru"]],
        categories=list(AUTOMATION_CATEGORIES),
    )


def estimated_cell_cost(
    per_cell: int, include_details: bool, actor_id: str | None = None
) -> float:
    listing, detail = actor_pricing(actor_id)
    return per_cell * (listing + (detail if include_details else 0.0))


async def run_sweep(
    *,
    per_cell: int = 20,
    cities: list[str] | None = None,
    categories: list[str] | None = None,
    include_details: bool = True,
    dry_run: bool = False,
    stop_on_budget: bool = True,
) -> SweepResult:
    """Walk every cell once, within the daily spend cap."""
    cells = build_cells(cities, categories)
    result = SweepResult(started_at=datetime.now(UTC))

    midnight = _utc_midnight()
    result.spend_before_usd = 0.0 if dry_run else await spend_since(midnight)
    cap = settings.daily_spend_cap_usd
    est = estimated_cell_cost(per_cell, include_details)

    log.info(
        "sweep.start",
        cells=len(cells),
        per_cell=per_cell,
        spend_today=round(result.spend_before_usd, 4),
        cap=cap,
        est_total=round(est * len(cells), 4),
    )

    if not dry_run and result.spend_before_usd >= cap:
        log.error("sweep.budget_exhausted", spend=result.spend_before_usd, cap=cap)
        result.budget_stopped = True
        result.finished_at = datetime.now(UTC)
        return result

    for city, category in cells:
        spent = result.spend_before_usd + result.cost_usd

        # Predict before spending: refusing to start a cell we cannot afford
        # is the whole point of the guard.
        if stop_on_budget and not dry_run and spent + est > cap:
            log.warning(
                "sweep.budget_stop", city=city, category=category,
                spent=round(spent, 4), would_add=round(est, 4), cap=cap,
            )
            result.budget_stopped = True
            result.cells.append(
                CellResult(city=city, category=category, skipped="daily spend cap")
            )
            break

        cell = CellResult(city=city, category=category)
        spec = CATEGORIES.get(category, {})
        query = spec.get("query", category)

        try:
            out = await ingest(
                search_query=query,
                location=city,
                max_items=per_cell,
                include_details=include_details,
                dry_run=dry_run,
            )
            cell.listings = len(out.listings)
            cell.created = out.created
            cell.updated = out.updated
            cell.images_stored = out.images_stored
            cell.images_failed = out.images_failed
            cell.cost_usd = out.cost_usd or 0.0
            cell.error = out.error
        except ApifyRunError as exc:
            # One bad cell must not end the sweep -- the other 24 are still
            # worth having.
            cell.error = f"ApifyRunError: {exc}"
            log.error("sweep.cell_failed", city=city, category=category, error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            cell.error = f"{type(exc).__name__}: {exc}"
            log.exception("sweep.cell_crashed", city=city, category=category)

        result.cells.append(cell)
        log.info(
            "sweep.cell_done", city=city, category=category,
            listings=cell.listings, created=cell.created,
            images=cell.images_stored, cost=cell.cost_usd,
        )

    result.finished_at = datetime.now(UTC)
    log.info(
        "sweep.done",
        listings=result.listings, created=result.created,
        cost=round(result.cost_usd, 4), failed=len(result.failed_cells),
        budget_stopped=result.budget_stopped,
    )
    return result


def exit_code(result: SweepResult) -> int:
    """Exit code for cron / systemd to key alerting off.

    Distinguishes "nothing came back" from "it broke" -- those need opposite
    responses, and collapsing them is how a dead pipeline looks healthy.
    """
    if result.budget_stopped and not result.listings:
        return 4  # capped before doing anything
    if result.failed_cells and len(result.failed_cells) == len(result.cells):
        return 2  # everything failed
    if result.failed_cells:
        return 3  # partial failure
    if not result.listings:
        return 5  # ran clean, found nothing -- a supply signal
    return 0


def main() -> int:
    """Sync entry point for `vingo-sweep`."""
    import argparse
    import logging
    import sys
    from pathlib import Path

    p = argparse.ArgumentParser(
        description="Unattended sweep across all city x category cells."
    )
    p.add_argument("--per-cell", type=int, default=20,
                   help="listings per city/category (default 20)")
    p.add_argument("--cities", nargs="*", default=None)
    p.add_argument("--categories", nargs="*", default=None)
    p.add_argument("--no-details", action="store_true",
                   help="skip detail fetch -- halves cost, loses descriptions")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and map only; no storage, no database, no spend check")
    p.add_argument("--ignore-budget", action="store_true",
                   help="do not stop at the daily cap (use deliberately)")
    p.add_argument("--lock", type=Path, default=None,
                   help="lock file path (default: <state>/sweep.lock)")
    args = p.parse_args()

    logging.basicConfig(format="%(message)s", stream=sys.stdout,
                        level=settings.log_level)
    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%dT%H:%M:%S", utc=True),
        structlog.processors.JSONRenderer(),
    ])

    from vingo_scraper.db.session import ensure_compatible_event_loop
    from vingo_scraper.pipeline.lock import AlreadyRunning, single_instance

    ensure_compatible_event_loop()

    lock_path = args.lock or (settings.state_dir / "sweep.lock")
    try:
        with single_instance(lock_path):
            result = asyncio.run(run_sweep(
                per_cell=args.per_cell,
                cities=args.cities,
                categories=args.categories,
                include_details=not args.no_details,
                dry_run=args.dry_run,
                stop_on_budget=not args.ignore_budget,
            ))
    except AlreadyRunning as exc:
        log.warning("sweep.skipped", reason=str(exc))
        return 75  # EX_TEMPFAIL -- previous run still going, not an error

    print()
    print(result.summary())
    print()
    return exit_code(result)


# ==========================================================================
# Quota-driven bulk sweeps
# ==========================================================================
#
# A target (250 / 500 / 1000) is filled by running cells until `quota` says
# stop. Three things make this different from a manual run:
#
#   1. It is long. ~42 sequential pages for 1,000 listings, 15-40 minutes,
#      holding the ingestion lock throughout -- because two Actors at once is
#      paying twice.
#   2. It is RESUMABLE. Dedup stops a listing being stored twice; it does not
#      stop it being billed twice, since the Actor charges when it scrapes.
#      A crash at cell 9 of 14 must not re-buy cells 1-8.
#   3. It stops for a REASON, and says which. Target met, budget reached and
#      Marketplace exhausted need different responses and look identical
#      otherwise.
#
# `run_cell` is injected so the loop can be exercised offline, without network
# and without spending. The resume path in particular is only trustworthy if
# it has been watched working.

from collections.abc import Awaitable, Callable  # noqa: E402

from vingo_scraper.pipeline import quota  # noqa: E402


@dataclass
class CellOutcome:
    """What one cell actually returned. Measured, never predicted."""

    category: str
    pages: int
    new_rows: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class SweepState:
    """Everything needed to resume. Serialises straight to JSONB."""

    target: int
    location: str
    completed: list[CellOutcome] = field(default_factory=list)
    stopped_reason: str | None = None
    # Which of the three stop conditions fired. The sentence is for the
    # operator; this is for the UI, which must not branch by matching prose.
    stopped_kind: str | None = None
    # When this sweep began, ISO-8601. The Bulk tab filters listings on it so
    # successive sweeps do not pile into one view -- a sweep spans one
    # `ScrapeRun` per cell, so the existing single-run `since_run` cannot
    # scope it. Survives resume: a resumed sweep is the same sweep.
    started_at: str | None = None

    @property
    def obtained(self) -> int:
        return sum(c.new_rows for c in self.completed)

    @property
    def spent_usd(self) -> float:
        return sum(c.cost_usd for c in self.completed)

    @property
    def done_cells(self) -> set[str]:
        """Categories already paid for. Resume must not buy them again."""
        return {c.category for c in self.completed}

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "location": self.location,
            "stopped_reason": self.stopped_reason,
            "stopped_kind": self.stopped_kind,
            "started_at": self.started_at,
            "completed": [
                {"category": c.category, "pages": c.pages, "new_rows": c.new_rows,
                 "cost_usd": c.cost_usd, "error": c.error}
                for c in self.completed
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> SweepState | None:
        if not raw:
            return None
        return cls(
            target=raw.get("target", 0),
            location=raw.get("location", ""),
            stopped_reason=raw.get("stopped_reason"),
            stopped_kind=raw.get("stopped_kind"),
            started_at=raw.get("started_at"),
            completed=[CellOutcome(**c) for c in raw.get("completed", [])],
        )


async def run_quota_sweep(
    *,
    target: int,
    location: str,
    yields: dict[str, float],
    budget_usd: float,
    run_cell: Callable[[str, int, str], Awaitable[CellOutcome]],
    resume: SweepState | None = None,
    on_progress: Callable[[SweepState], Awaitable[None]] | None = None,
) -> SweepState:
    """Fill `target` net-new listings, rebalancing as cells report back.

    `yields` are measured net-new rows per page from the supply baseline.
    `run_cell(category, pages, location)` performs one cell and returns what it
    actually got -- the loop never assumes a cell delivered what was planned.

    Passing `resume` continues a sweep that died: its completed cells are
    skipped, so nothing already paid for is bought again.
    """
    state = resume or SweepState(target=target, location=location)

    while True:
        why = quota.should_stop(
            obtained=state.obtained,
            target=state.target,
            spent_usd=state.spent_usd,
            budget_usd=budget_usd,
            # A cell already paid for is not a cell still available.
            cells_left=len([k for k in yields if k not in state.done_cells]),
        )
        if why:
            state.stopped_kind, state.stopped_reason = why
            break

        # Plan only from what is left to buy. Feeding the allocator the full
        # yield map made it keep proposing a category the sweep had already
        # swept -- highest yield, already paid for -- and the loop dead-ended
        # on its own first choice.
        #
        # THIS is the rebalancing: the remaining target is recomputed from what
        # cells actually returned, so a category that under-delivered leaves a
        # larger remainder for the ones still producing.
        available = {k: y for k, y in yields.items() if k not in state.done_cells}
        remaining = state.target - state.obtained
        plan = quota.plan_cells(remaining, available)

        nxt = plan.allocations[0] if plan.allocations else None
        if nxt is None:
            # The allocator has nothing left to suggest that we have not
            # already bought. Say so rather than looping.
            state.stopped_reason = (
                f"no categories left to try at {state.obtained} of "
                f"{state.target} -- every usable one has been swept"
            )
            break

        outcome = await run_cell(nxt.category, nxt.pages, location)
        state.completed.append(outcome)
        if on_progress:
            await on_progress(state)

    if on_progress:
        await on_progress(state)
    return state
