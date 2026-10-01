"""Runtime switchboard backed by `system_config`.

Vercel functions are stateless: an in-process flag does not survive between
invocations, so the on/off state for scheduled ingestion has to live where
both the cron endpoint and the toggle endpoint can see it. That is this table.

Design rule: **the cron tick reads this BEFORE it calls Apify.** The switch is
only a kill switch if it is checked before the money is spent, not after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from vingo_scraper.db import models
from vingo_scraper.db.session import session_scope

log = structlog.get_logger(__name__)

CONFIG_ID = 1


@dataclass
class DemoState:
    is_active: bool
    runs_remaining: int
    runs_completed: int
    interval_seconds: int
    search_query: str
    location: str
    max_pages: int
    sequence_started_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    stopped_reason: str | None
    cursor: int = 0
    per_cell: int = 10
    include_details: bool = False
    next_run_at: datetime | None = None
    city: str = "bangalore"

    def to_dict(self) -> dict:
        return {
            "is_ingestion_active": self.is_active,
            "runs_remaining": self.runs_remaining,
            "runs_completed": self.runs_completed,
            "interval_seconds": self.interval_seconds,
            "search_query": self.search_query,
            "location": self.location,
            "max_pages": self.max_pages,
            "sequence_started_at": _iso(self.sequence_started_at),
            "last_run_at": _iso(self.last_run_at),
            "last_status": self.last_status,
            "stopped_reason": self.stopped_reason,
            "cursor": self.cursor,
            "per_cell": self.per_cell,
            "include_details": self.include_details,
            "next_run_at": _iso(self.next_run_at),
            "city": self.city,
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _to_state(row: models.SystemConfig) -> DemoState:
    return DemoState(
        is_active=row.is_ingestion_active,
        runs_remaining=row.runs_remaining,
        runs_completed=row.runs_completed,
        interval_seconds=row.interval_seconds,
        search_query=row.search_query,
        location=row.location,
        max_pages=row.max_pages,
        sequence_started_at=row.sequence_started_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        stopped_reason=row.stopped_reason,
        cursor=row.cursor,
        per_cell=row.per_cell,
        include_details=row.include_details,
        next_run_at=row.next_run_at,
        city=row.city,
    )


async def _row(session, *, lock: bool = False) -> models.SystemConfig:
    """Fetch the single config row, creating it if a fresh database lacks it.

    `lock=True` takes `SELECT ... FOR UPDATE`. Anything that decrements
    `runs_remaining` must use it: Postgres defaults to READ COMMITTED, so two
    concurrent claims would both read the same value, both decrement from it,
    and both spend. That is one claimed slot billed twice.
    """
    stmt = select(models.SystemConfig).where(models.SystemConfig.id == CONFIG_ID)
    if lock:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    if row is None:
        # Default OFF. A new database must never start ingesting on its own.
        row = models.SystemConfig(id=CONFIG_ID, is_ingestion_active=False)
        session.add(row)
        await session.flush()
    return row


async def get_state() -> DemoState:
    async with session_scope() as s:
        return _to_state(await _row(s))


async def start_sequence(
    *,
    runs: int = 3,
    interval_seconds: int = 180,
    search_query: str | None = None,
    location: str | None = None,
    max_pages: int = 1,
) -> DemoState:
    """Arm a bounded sequence of runs.

    Bounded on purpose: `runs_remaining` counts down and ingestion switches
    itself off at zero, so a demo left running cannot quietly drain the Apify
    budget overnight.
    """
    runs = max(1, min(runs, 10))
    async with session_scope() as s:
        row = await _row(s)
        row.is_ingestion_active = True
        row.runs_remaining = runs
        row.runs_completed = 0
        row.interval_seconds = max(60, interval_seconds)
        row.max_pages = max(1, min(max_pages, 3))
        if search_query:
            row.search_query = search_query
        if location:
            row.location = location
        row.sequence_started_at = datetime.now(UTC)
        row.stopped_reason = None
        row.last_status = "armed"
        log.info("demo.started", runs=runs, query=row.search_query,
                 location=row.location, interval=row.interval_seconds)
        return _to_state(row)


async def stop_sequence(reason: str = "stopped by operator") -> DemoState:
    """The kill switch. Takes effect before the next tick spends anything."""
    async with session_scope() as s:
        row = await _row(s)
        row.is_ingestion_active = False
        row.runs_remaining = 0
        row.stopped_reason = reason
        row.last_status = "stopped"
        log.warning("demo.stopped", reason=reason,
                    completed=row.runs_completed)
        return _to_state(row)


async def toggle() -> DemoState:
    """Flip the switch. Turning on with no runs left arms a fresh sequence."""
    async with session_scope() as s:
        row = await _row(s)
        if row.is_ingestion_active:
            row.is_ingestion_active = False
            row.runs_remaining = 0
            row.stopped_reason = "toggled off"
            row.last_status = "stopped"
        else:
            row.is_ingestion_active = True
            if row.runs_remaining <= 0:
                row.runs_remaining = 3
                row.runs_completed = 0
                row.sequence_started_at = datetime.now(UTC)
            row.stopped_reason = None
            row.last_status = "armed"
        log.info("demo.toggled", active=row.is_ingestion_active,
                 remaining=row.runs_remaining)
        return _to_state(row)


async def claim_run() -> DemoState | None:
    """Atomically take one run from the sequence.

    Returns the state to run with, or None if ingestion is off or exhausted.
    Decrementing here -- before the Actor is called -- means a crashed run
    costs one slot rather than looping forever on the same one.
    """
    async with session_scope() as s:
        row = await _row(s, lock=True)
        if not row.is_ingestion_active:
            return None
        if row.runs_remaining <= 0:
            row.is_ingestion_active = False
            row.stopped_reason = "sequence complete"
            row.last_status = "complete"
            return None

        now = datetime.now(UTC)
        row.runs_remaining -= 1
        row.runs_completed += 1
        row.last_run_at = now
        row.last_status = "running"

        # The caller runs the cell this state names; the cursor moves on now so
        # a crash mid-run does not repeat the same cell forever.
        claimed = _to_state(row)
        row.cursor = row.cursor + 1
        row.next_run_at = now + timedelta(seconds=row.interval_seconds)

        # Last one: switch off now so nothing can slip through after it.
        if row.runs_remaining == 0:
            row.is_ingestion_active = False
            row.stopped_reason = "sequence complete"
            row.next_run_at = None

        return claimed


async def record_result(status: str) -> None:
    async with session_scope() as s:
        row = await _row(s)
        row.last_status = status


# --------------------------------------------------------------------------
# In-flight Apify run, handed from one tick to the next
# --------------------------------------------------------------------------

# A run that has not finished within this long is treated as lost rather than
# polled forever. Apify's own run timeout is longer, so this only trips when
# something has genuinely gone wrong.
PENDING_TIMEOUT_S = 600


async def set_pending(run_id: str, cell: str, run_input: dict | None = None) -> None:
    async with session_scope() as s:
        row = await _row(s, lock=True)
        row.pending_run_id = run_id
        row.pending_started_at = datetime.now(UTC)
        row.pending_cell = cell
        row.pending_input = run_input or {}
        row.last_status = f"running {cell}"


async def clear_pending(status: str | None = None) -> None:
    async with session_scope() as s:
        row = await _row(s, lock=True)
        row.pending_run_id = None
        row.pending_started_at = None
        row.pending_cell = None
        row.pending_input = None
        if status:
            row.last_status = status


async def get_pending() -> tuple[str, str, dict] | None:
    """(run_id, cell, run_input) for an in-flight run, or None.

    Returns None once the run is older than `PENDING_TIMEOUT_S`, and clears it,
    so a run that never reports back cannot wedge the sequence permanently.
    """
    async with session_scope() as s:
        row = await _row(s)
        if not row.pending_run_id:
            return None
        started = row.pending_started_at
        if started and (datetime.now(UTC) - started).total_seconds() > PENDING_TIMEOUT_S:
            log.warning("automation.pending_abandoned", run_id=row.pending_run_id,
                        cell=row.pending_cell)
            row.pending_run_id = None
            row.pending_started_at = None
            row.pending_cell = None
            row.pending_input = None
            row.last_status = "abandoned a run that never reported back"
            return None
        return row.pending_run_id, row.pending_cell or "-", row.pending_input or {}


# --------------------------------------------------------------------------
# Automation -- the console's grid walk, same table, same kill switch
# --------------------------------------------------------------------------

# One tick must finish inside Vercel's 60s function ceiling. A measured ingest
# is Apify ~21s + ~60 images ~33s + persist ~8s, so 10 listings sits right on
# the limit and 8 leaves margin. Exceeding it does not fail loudly -- the
# function is simply cut off mid-rehost.
MAX_PER_CELL_SERVERLESS = 8


async def start_automation(
    *,
    runs: int = 3,
    interval_seconds: int = 180,
    per_cell: int = 10,
    include_details: bool = False,
    city: str | None = None,
    restart: bool = False,
) -> DemoState:
    """Arm the grid walk. Bounded exactly like `start_sequence`.

    The cursor is NOT reset. It used to be, which meant every Start began at
    mobile phones and a 3-run sequence covered phones/electronics/furniture
    while vehicles and appliances were never scraped at all. Successive
    sequences now continue the rotation and wrap, so the whole catalogue gets
    covered. Pass `restart=True` to deliberately open a demo on category 1.
    """
    runs = max(1, min(runs, 10))
    now = datetime.now(UTC)
    async with session_scope() as s:
        row = await _row(s, lock=True)
        row.is_ingestion_active = True
        row.runs_remaining = runs
        row.runs_completed = 0
        row.interval_seconds = max(60, interval_seconds)
        row.per_cell = max(1, min(per_cell, MAX_PER_CELL_SERVERLESS))
        row.include_details = include_details
        if restart:
            row.cursor = 0
        if city:
            row.city = city
        row.sequence_started_at = now
        # Due immediately: the first tick should not wait out a full interval.
        row.next_run_at = now
        row.stopped_reason = None
        row.last_status = "armed"
        log.info("automation.started", runs=runs, per_cell=row.per_cell,
                 details=row.include_details, city=row.city,
                 interval=row.interval_seconds)
        return _to_state(row)


async def automation_snapshot() -> dict:
    """Everything the console's Automation panel renders, read from Postgres.

    Replaces the old `api/scheduler.snapshot()`, which read a module-level
    object and therefore returned a different answer per lambda instance.
    """
    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.pipeline.sweep import automation_cells, estimated_cell_cost

    state = await get_state()
    budget = await get_budget()
    pending = await get_pending()

    cells = automation_cells(state.city)
    idx = state.cursor % len(cells) if cells else 0
    nxt = cells[idx] if cells else ("-", "-")
    est = estimated_cell_cost(state.per_cell, state.include_details)

    # The cell being scraped RIGHT NOW is the pending one; `cursor` has already
    # moved on to what comes next. Conflating the two is what made the panel
    # impossible to read during a demo.
    current = pending[1] if pending else None
    done = state.runs_completed % len(cells) if cells else 0

    # Two caps, two numbers, never crossed. The panel previously showed the
    # MONTHLY Apify total against the DAILY cap, which read $1.56 of $2.00 on a
    # day that had actually spent $0.0001.
    spend_today = budget.daily_used_usd
    cap = budget.daily_cap_usd

    return {
        "running": state.is_active,
        # An in-flight Actor run. The console polls back quickly while this is
        # set rather than waiting out the full interval, since the run is
        # typically done within 10-50s.
        "pending": bool(pending),
        "pending_cell": pending[1] if pending else None,
        "interval_s": state.interval_seconds,
        "per_cell": state.per_cell,
        "include_details": state.include_details,
        "ticks": state.runs_completed,
        "runs_remaining": state.runs_remaining,
        "cells_total": len(cells),
        "cursor": idx,
        "next_cell": f"{nxt[0]} / {nxt[1]}",
        "current_cell": current,
        "city": state.city,
        # The whole rotation, in order, so the console renders the strip
        # instead of re-deriving the category list in JavaScript.
        "cells": [{"city": c, "category": k} for c, k in cells],
        "cycle_position": done,
        "started_at": _iso(state.sequence_started_at),
        "next_run_at": _iso(state.next_run_at),
        "last_run_at": _iso(state.last_run_at),
        "last_status": state.last_status,
        "stopped_reason": state.stopped_reason,
        "spend_today_usd": round(spend_today, 4),
        "daily_cap_usd": cap,
        "budget_left_usd": round(max(cap - spend_today, 0), 4),
        "est_tick_usd": round(est, 4),
        "ticks_affordable": int(max(cap - spend_today, 0) / est) if est else 0,
        "projected_daily_usd": round(est * (86400 / max(state.interval_seconds, 1)), 2),
        "budget": budget.to_dict(),
    }


async def recent_ticks(limit: int = 12, trigger: str = "automation") -> list[dict]:
    """Tick history, from `scrape_run` rather than an in-memory deque.

    Filtered to automation runs. Unfiltered, a manual "Today's update" appeared
    among the category cells -- a `bangalore / iphone` row between
    `bangalore / electronics` and `bangalore / furniture`, which reads as
    something the automation scraped.
    """
    from sqlalchemy import desc

    async with session_scope() as s:
        stmt = select(models.ScrapeRun).order_by(desc(models.ScrapeRun.started_at))
        if trigger:
            stmt = stmt.where(models.ScrapeRun.trigger == trigger)
        rows = (await s.scalars(stmt.limit(limit))).all()
    return [
        {
            "at": _iso(r.started_at),
            # `run_input` is the actor payload, whose key names differ per
            # actor -- hence the spelling tuples rather than direct access.
            "city": _from_input(r.run_input, "location", "city"),
            "category": _from_input(r.run_input, "searchKeyword", "query", "search"),
            "listings": r.items_returned or 0,
            "created": r.items_ingested or 0,
            "images": r.images_stored or 0,
            "cost_usd": float(r.cost_usd or 0.0),
            "error": r.error,
        }
        for r in rows
    ]


def _from_input(run_input: dict | None, *names: str) -> str:
    for name in names:
        value = (run_input or {}).get(name)
        if value:
            return str(value)
    return "-"
