"""In-memory job registry for the demo console.

Deliberately not durable: this exists so one operator can watch one ingest run
live in a browser. Anything that needs to survive a restart belongs in the
`scrape_run` table, which the pipeline already writes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_EVENTS = 500


@dataclass
class Job:
    id: str
    query: str
    location: str
    max_items: int
    dry_run: bool = False
    status: str = "queued"  # queued | running | done | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    result: dict[str, Any] | None = None
    error: str | None = None

    # Subscribers get their own queue so several browser tabs can watch.
    _queues: list[asyncio.Queue] = field(default_factory=list)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        # Replay history first, so a late-joining tab sees the whole run.
        for event in self.events:
            q.put_nowait(event)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    async def emit(self, stage: str, payload: dict | None = None) -> None:
        event = {
            "stage": stage,
            "at": datetime.now(UTC).isoformat(),
            **(payload or {}),
        }
        self.events.append(event)
        for q in list(self._queues):
            q.put_nowait(event)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=50)

    def create(self, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], **kwargs)
        self._jobs[job.id] = job
        self._order.append(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[Job]:
        return [self._jobs[i] for i in list(self._order)[::-1][:limit]]

    @property
    def running(self) -> Job | None:
        for job in self.recent(50):
            if job.status == "running":
                return job
        return None


registry = JobRegistry()
