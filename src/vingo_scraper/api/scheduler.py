"""In-process scheduler so the console can show automation running live.

Production scheduling on Linux is systemd/cron (`deploy/`). This exists so the
same behaviour is visible in a browser without leaving the app.

Two design choices worth knowing:

* **One cell per tick, round-robin.** A 3-minute cadence running all 25 cells
  would be 12,000 listings an hour. Ticking one cell at a time keeps a short
  interval affordable and still covers the whole grid.
* **The daily spend cap is checked before every tick**, against actual spend
  recorded in `scrape_run`. A short interval on a per-result-billed API is
  exactly where an unattended loop turns into an invoice, so the guard is the
  point, not a nicety.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from vingo_scraper.apify.budget import get_budget, invalidate_cache
from vingo_scraper.config import CATEGORIES, settings
from vingo_scraper.pipeline.sweep import build_cells, estimated_cell_cost

log = structlog.get_logger(__name__)


def _utc_midnight() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class TickRecord:
    at: datetime
    city: str
    category: str
    listings: int = 0
    created: int = 0
    images: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    skipped: str | None = None


@dataclass
class SchedulerState:
    running: bool = False
    interval_s: int = 180
    per_cell: int = 10
    include_details: bool = True
    cursor: int = 0
    ticks: int = 0
    started_at: datetime | None = None
    next_run_at: datetime | None = None
    last_tick: TickRecord | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=50))
    stopped_reason: str | None = None

    def snapshot(self, spend_today: float) -> dict:
        cells = build_cells()
        nxt = cells[self.cursor % len(cells)] if cells else ("-", "-")
        cap = settings.daily_spend_cap_usd
        est = estimated_cell_cost(self.per_cell, self.include_details)
        return {
            "running": self.running,
            "interval_s": self.interval_s,
            "per_cell": self.per_cell,
            "include_details": self.include_details,
            "ticks": self.ticks,
            "cells_total": len(cells),
            "cursor": self.cursor % max(len(cells), 1),
            "next_cell": f"{nxt[0]} / {nxt[1]}",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "stopped_reason": self.stopped_reason,
            "spend_today_usd": round(spend_today, 4),
            "daily_cap_usd": cap,
            "budget_left_usd": round(max(cap - spend_today, 0), 4),
            "est_tick_usd": round(est, 4),
            "ticks_affordable": int(max(cap - spend_today, 0) / est) if est else 0,
            "projected_daily_usd": round(est * (86400 / self.interval_s), 2),
            "history": [
                {
                    "at": t.at.isoformat(), "city": t.city, "category": t.category,
                    "listings": t.listings, "created": t.created, "images": t.images,
                    "cost_usd": t.cost_usd, "error": t.error, "skipped": t.skipped,
                }
                for t in list(self.history)[::-1]
            ],
        }


state = SchedulerState()
_task: asyncio.Task | None = None


async def _run_one_cell() -> TickRecord:
    """Ingest a single cell and advance the cursor."""
    from vingo_scraper.apify.runner import ingest

    cells = build_cells()
    city, category = cells[state.cursor % len(cells)]
    state.cursor = (state.cursor + 1) % len(cells)

    rec = TickRecord(at=datetime.now(UTC), city=city, category=category)
    query = CATEGORIES.get(category, {}).get("query", category)

    try:
        out = await ingest(
            search_query=query,
            location=city,
            max_items=state.per_cell,
            include_details=state.include_details,
        )
        rec.listings = len(out.listings)
        rec.created = out.created
        rec.images = out.images_stored
        rec.cost_usd = out.cost_usd or 0.0
        rec.error = out.error
    except Exception as exc:
        # A failing cell must not kill the scheduler -- the next tick moves on
        # to a different cell, which may well work.
        rec.error = f"{type(exc).__name__}: {exc}"
        log.error("scheduler.tick_failed", city=city, category=category,
                  error=rec.error)

    return rec


async def _loop() -> None:
    log.info("scheduler.started", interval_s=state.interval_s,
             per_cell=state.per_cell, details=state.include_details)
    try:
        while state.running:
            budget = await get_budget()
            est = estimated_cell_cost(state.per_cell, state.include_details)

            blocked = budget.blocked_reason(est)
            if blocked:
                # Stop rather than overspend. This is the behaviour the demo is
                # meant to show: the system halts itself, visibly.
                state.stopped_reason = blocked
                state.running = False
                log.warning("scheduler.budget_stop", reason=blocked,
                            used=budget.monthly_used_usd, cap=budget.total_cap_usd)
                break

            rec = await _run_one_cell()
            invalidate_cache()  # that run just spent; re-read before the next
            state.ticks += 1
            state.last_tick = rec
            state.history.append(rec)
            log.info("scheduler.tick", city=rec.city, category=rec.category,
                     listings=rec.listings, cost=rec.cost_usd, tick=state.ticks)

            state.next_run_at = datetime.now(UTC) + timedelta(seconds=state.interval_s)
            try:
                await asyncio.sleep(state.interval_s)
            except asyncio.CancelledError:
                break
    except asyncio.CancelledError:
        pass
    finally:
        state.running = False
        state.next_run_at = None
        log.info("scheduler.stopped", ticks=state.ticks,
                 reason=state.stopped_reason)


async def start(
    *, interval_s: int = 180, per_cell: int = 10, include_details: bool = True
) -> dict:
    global _task
    if state.running:
        return {"ok": False, "error": "already running"}

    # 60s floor: anything faster outruns a single ingest (~30-60s) and would
    # just stack work.
    state.interval_s = max(60, interval_s)
    state.per_cell = max(1, min(per_cell, 50))
    state.include_details = include_details
    state.running = True
    state.stopped_reason = None
    state.started_at = datetime.now(UTC)
    state.next_run_at = datetime.now(UTC)

    _task = asyncio.create_task(_loop())
    return {"ok": True}


async def stop() -> dict:
    global _task
    state.running = False
    state.stopped_reason = "stopped by operator"
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
    return {"ok": True}


async def snapshot() -> dict:
    budget = await get_budget()
    out = state.snapshot(budget.monthly_used_usd)
    out["budget"] = budget.to_dict()
    return out
