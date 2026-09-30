"""Runtime switchboard backed by `system_config`.

Vercel functions are stateless: an in-process flag does not survive between
invocations, so the on/off state for scheduled ingestion has to live where
both the cron endpoint and the toggle endpoint can see it. That is this table.

Design rule: **the cron tick reads this BEFORE it calls Apify.** The switch is
only a kill switch if it is checked before the money is spent, not after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

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
    )


async def _row(session) -> models.SystemConfig:
    """Fetch the single config row, creating it if a fresh database lacks it."""
    row = await session.scalar(
        select(models.SystemConfig).where(models.SystemConfig.id == CONFIG_ID)
    )
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
        row = await _row(s)
        if not row.is_ingestion_active:
            return None
        if row.runs_remaining <= 0:
            row.is_ingestion_active = False
            row.stopped_reason = "sequence complete"
            row.last_status = "complete"
            return None

        row.runs_remaining -= 1
        row.runs_completed += 1
        row.last_run_at = datetime.now(UTC)
        row.last_status = "running"

        # Last one: switch off now so nothing can slip through after it.
        if row.runs_remaining == 0:
            row.is_ingestion_active = False
            row.stopped_reason = "sequence complete"

        return _to_state(row)


async def record_result(status: str) -> None:
    async with session_scope() as s:
        row = await _row(s)
        row.last_status = status
