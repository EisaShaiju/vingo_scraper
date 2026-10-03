"""Platform stability: two runners, one pipeline, one lock.

Twenty tests covering the ways manual and automated ingestion interact. The
expensive failures here are not crashes -- they are two Actors started at once
(paying twice), a lock that never releases (nothing runs again), and a manual
run that fails silently while automation keeps working.

**What these tests do and do not prove.** They run offline, so the advisory
lock is modelled by a fake with the same contract as `pg_try_advisory_lock`:
the first caller takes it, a second is refused immediately rather than
queueing. That exercises every one of OUR paths -- the 409, the deferral, the
release on exception, the stale sweep. It does not exercise Postgres itself,
which is why `docs/vm-deployment.md` carries a live two-session check as a
deployment step. Postgres's own locking does not need testing by us; our
handling of being refused does.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from vingo_scraper.api.main import app
from vingo_scraper.apify.budget import BudgetStatus
from vingo_scraper.db.config_store import DemoState

SECRET = "test-secret-token"
AUTH = {"X-Demo-Token": SECRET}
CATEGORIES_IN_ORDER = [
    "mobile_phones", "electronics", "furniture", "vehicles", "appliances",
]


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def make_state(**kw) -> DemoState:
    base = DemoState(
        is_active=False, runs_remaining=0, runs_completed=0,
        interval_seconds=180, search_query="iphone", location="bangalore",
        max_pages=1, sequence_started_at=None, last_run_at=None,
        last_status=None, stopped_reason=None,
        cursor=0, per_cell=8, include_details=False,
        next_run_at=None, city="bangalore",
    )
    return replace(base, **kw)


def healthy_budget(**kw) -> BudgetStatus:
    return BudgetStatus(
        monthly_used_usd=kw.get("monthly", 1.5),
        total_cap_usd=7.0,
        daily_used_usd=kw.get("daily", 0.05),
        daily_cap_usd=2.0,
    )


class Harness:
    """In-memory stand-in for `system_config` plus the Apify client."""

    def __init__(self):
        self.state = make_state()
        self.lock_holder: str | None = None
        self.lock_at: datetime | None = None
        self.pending = None            # automation: (run_id, cell, input)
        self.manual_pending = None     # manual: (run_id, input)
        self.manual_status: str | None = None
        self.filters: dict = {}
        self.started: list[dict] = []  # every Actor start -- the spend ledger
        self.collected: list[str] = []
        self.history: dict[str, list] = {"automation": [], "manual": []}
        self.listings_returned = 8
        self.collect_returns_none = False
        self.ingest_raises: Exception | None = None
        self.known_ids: set[str] = set()
        self.budget_calls = 0
        self.reconcile_calls = 0


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def client(monkeypatch, h) -> TestClient:
    monkeypatch.setattr("vingo_scraper.config.settings.demo_secret", SECRET)

    # ---- the lock: same contract as pg_try_advisory_lock ----------------
    @asynccontextmanager
    async def ingestion_lock(holder: str):
        if h.lock_holder is not None:
            stale = h.lock_at and (
                datetime.now(UTC) - h.lock_at
            ).total_seconds() > 600
            if not stale:
                # Resolved through the MODULE, never imported by name. A
                # reload elsewhere in the suite rebinds this class, and a
                # stale reference makes the endpoint's `except` silently miss.
                from vingo_scraper.db import config_store
                raise config_store.IngestionBusy(h.lock_holder)
            h.lock_holder = None  # orphan swept
        h.lock_holder = holder
        h.lock_at = datetime.now(UTC)
        try:
            yield
        finally:
            # Always, including on exception. A lock released only on the
            # happy path is a deadlock waiting for the first bad run.
            h.lock_holder = None
            h.lock_at = None

    async def lock_status():
        return {"locked": bool(h.lock_holder), "holder": h.lock_holder,
                "since": h.lock_at.isoformat() if h.lock_at else None}

    # ---- automation state ------------------------------------------------
    async def get_state():
        return h.state

    async def claim_run():
        if not h.state.is_active or h.state.runs_remaining <= 0:
            if h.state.is_active:
                h.state = replace(h.state, is_active=False,
                                  stopped_reason="sequence complete")
            return None
        claimed = h.state
        h.state = replace(
            h.state,
            runs_remaining=h.state.runs_remaining - 1,
            runs_completed=h.state.runs_completed + 1,
            cursor=h.state.cursor + 1,
            last_run_at=datetime.now(UTC),
        )
        if h.state.runs_remaining == 0:
            h.state = replace(h.state, is_active=False,
                              stopped_reason="sequence complete")
        return claimed

    async def stop_sequence(reason="stopped by operator"):
        h.state = replace(h.state, is_active=False, runs_remaining=0,
                          stopped_reason=reason, last_status="stopped")
        return h.state

    async def record_result(status):
        return None

    async def set_pending(run_id, cell, run_input=None):
        h.pending = (run_id, cell, run_input or {})

    async def clear_pending(status=None):
        h.pending = None

    async def get_pending():
        return h.pending

    # ---- manual state ----------------------------------------------------
    async def set_manual_pending(run_id, run_input, label):
        h.manual_pending = (run_id, run_input or {})
        h.manual_status = f"running {label}"

    async def clear_manual(status=None):
        h.manual_pending = None
        if status:
            h.manual_status = status

    async def get_manual_pending():
        return h.manual_pending

    async def manual_state():
        return {"running": bool(h.manual_pending), "status": h.manual_status,
                "run_id": h.manual_pending[0] if h.manual_pending else None,
                "started_at": None, "input": {}}

    async def recent_ticks(limit=12, trigger="automation"):
        return h.history.get(trigger, [])

    async def get_filters():
        return h.filters

    async def automation_snapshot():
        from vingo_scraper.pipeline.sweep import automation_cells
        cells = automation_cells(h.state.city)
        idx = h.state.cursor % len(cells)
        return {
            "running": h.state.is_active, "cursor": idx,
            "cells_total": len(cells),
            "next_cell": f"{cells[idx][0]} / {cells[idx][1]}",
            "cells": [{"city": c, "category": k} for c, k in cells],
            "cycle_position": h.state.runs_completed % len(cells),
            "ticks": h.state.runs_completed,
            "current_cell": h.pending[1] if h.pending else None,
            "pending": bool(h.pending),
            "spend_today_usd": 0.05, "daily_cap_usd": 2.0,
            "est_tick_usd": 0.004, "ticks_affordable": 487,
            "projected_daily_usd": 1.9, "interval_s": 180,
            "runs_remaining": h.state.runs_remaining,
            "stopped_reason": h.state.stopped_reason,
            "budget": healthy_budget().to_dict(),
        }

    for name, fn in [
        ("ingestion_lock", ingestion_lock), ("lock_status", lock_status),
        ("get_state", get_state), ("claim_run", claim_run),
        ("stop_sequence", stop_sequence), ("record_result", record_result),
        ("set_pending", set_pending), ("clear_pending", clear_pending),
        ("get_pending", get_pending), ("get_filters", get_filters),
        ("set_manual_pending", set_manual_pending),
        ("clear_manual", clear_manual),
        ("get_manual_pending", get_manual_pending),
        ("manual_state", manual_state), ("recent_ticks", recent_ticks),
        ("automation_snapshot", automation_snapshot),
    ]:
        monkeypatch.setattr(f"vingo_scraper.db.config_store.{name}", fn)

    # ---- budget ----------------------------------------------------------
    async def get_budget():
        h.budget_calls += 1
        return healthy_budget()

    async def reconcile_costs(limit=6):
        h.reconcile_calls += 1
        return 0

    monkeypatch.setattr("vingo_scraper.apify.budget.get_budget", get_budget)
    monkeypatch.setattr("vingo_scraper.apify.budget.invalidate_cache", lambda: None)
    monkeypatch.setattr("vingo_scraper.apify.budget.reconcile_costs", reconcile_costs)

    # ---- Apify -----------------------------------------------------------
    async def start_run(query, location, max_items, *, include_details=False,
                        filters=None, **kw):
        from vingo_scraper.apify.client import ActorRun, build_run_input
        run_input = build_run_input(
            query, location, max_items,
            include_details=include_details, filters=filters,
        )
        rec = {"query": query, "location": location, "max_items": max_items,
               "filters": filters or {}, "run_input": run_input}
        h.started.append(rec)
        return ActorRun(run_id=f"run-{len(h.started)}", actor_id="test/actor",
                        status="RUNNING", run_input=run_input)

    async def collect_run(run_id, *, run_input=None, **kw):
        if h.collect_returns_none:
            return None
        from vingo_scraper.apify.client import ActorRun
        h.collected.append(run_id)
        return ActorRun(run_id=run_id, actor_id="test/actor", status="SUCCEEDED",
                        run_input=run_input or {}, cost_usd=0.0115)

    async def ingest_collected(run, *, location, trigger="automation"):
        if h.ingest_raises:
            raise h.ingest_raises
        from vingo_scraper.apify.runner import IngestResult
        r = IngestResult()
        r.run_id = run.run_id
        r.cost_usd = run.cost_usd
        r.items_returned = h.listings_returned
        r.created = h.listings_returned
        r.images_stored = h.listings_returned
        r.listings = [object()] * h.listings_returned
        h.history.setdefault(trigger, []).insert(0, {
            "at": datetime.now(UTC).isoformat(), "city": location,
            "category": "x", "listings": h.listings_returned,
            "created": h.listings_returned, "images": h.listings_returned,
            "cost_usd": 0.0115, "error": None,
        })
        return r

    monkeypatch.setattr("vingo_scraper.apify.client.start_run", start_run)
    monkeypatch.setattr("vingo_scraper.apify.client.collect_run", collect_run)
    monkeypatch.setattr("vingo_scraper.apify.runner.ingest_collected", ingest_collected)

    return TestClient(app)


def arm(h, runs=5, cursor=0):
    h.state = replace(h.state, is_active=True, runs_remaining=runs,
                      runs_completed=0, cursor=cursor)


# ==========================================================================
# 1-2  the two runners work at all
# ==========================================================================

def test_01_manual_trigger_runs_through_the_two_phase_runner(client, h):
    """Start returns immediately; the ingest happens on the collect call.

    The old manual path waited inline for the Actor (10.8-47.1s) and died
    against the 60s ceiling without reporting why.
    """
    r = client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert len(h.started) == 1
    assert h.manual_pending is not None      # handed to the next request

    out = client.post("/api/ingest/collect", headers=AUTH).json()
    assert out["ran"] is True
    assert out["listings"] == 8
    assert h.manual_pending is None          # handoff cleared


def test_02_automated_tick_starts_then_collects_and_advances(client, h):
    arm(h, runs=2)
    first = client.post("/api/automation/tick", headers=AUTH).json()
    assert first["started"] is True
    assert h.state.runs_completed == 1       # slot consumed at start

    second = client.post("/api/automation/tick", headers=AUTH).json()
    assert second["ran"] is True
    assert h.pending is None


# ==========================================================================
# 3-5  contention
# ==========================================================================

def test_03_manual_during_automation_is_409_not_a_second_actor(client, h):
    """The expensive failure this prevents: two Actors, two charges."""
    h.lock_holder, h.lock_at = "automation", datetime.now(UTC)

    r = client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})

    assert r.status_code == 409
    assert r.json()["detail"] == "Ingestion currently in progress by automated runner"
    assert h.started == []                   # nothing was spent


def test_04_automated_tick_during_manual_defers_without_spending(client, h):
    arm(h, runs=3)
    h.lock_holder, h.lock_at = "manual", datetime.now(UTC)

    body = client.post("/api/automation/tick", headers=AUTH).json()

    assert body["deferred"] is True
    assert "manual" in body["reason"]
    assert h.started == []
    assert h.pending is None


def test_05_double_clicking_manual_starts_exactly_one_actor(client, h):
    """Rapid repeat clicks must not each buy a page."""
    first = client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})
    second = client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})

    assert first.status_code == 200
    assert second.status_code == 409         # a run is already in flight
    assert len(h.started) == 1


# ==========================================================================
# 6-7  the rotation
# ==========================================================================

def test_06_round_robin_walks_all_five_categories_in_order(client, h):
    arm(h, runs=5)
    seen = []
    for _ in range(5):
        client.post("/api/automation/tick", headers=AUTH)   # start
        seen.append(h.pending[1])
        client.post("/api/automation/tick", headers=AUTH)   # collect

    assert [c.split(" / ")[1] for c in seen] == CATEGORIES_IN_ORDER
    assert {c.split(" / ")[0] for c in seen} == {"bangalore"}


def test_07_cursor_wraps_back_to_the_first_category(client, h):
    """A sixth run returns to category 1 rather than running off the end."""
    arm(h, runs=2, cursor=4)                 # start on appliances

    client.post("/api/automation/tick", headers=AUTH)
    assert h.pending[1].endswith("appliances")
    client.post("/api/automation/tick", headers=AUTH)

    client.post("/api/automation/tick", headers=AUTH)
    assert h.pending[1].endswith("mobile_phones")


# ==========================================================================
# 8-10  feeds and filters
# ==========================================================================

def test_08_manual_history_is_separate_from_automated(client, h):
    client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})
    client.post("/api/ingest/collect", headers=AUTH)

    manual = client.get("/api/manual").json()["history"]
    auto = client.get("/api/automation").json()["history"]

    assert len(manual) == 1
    assert auto == []                        # a manual run is not a tick


def test_09_manual_filters_reach_the_actor(client, h):
    client.post("/api/ingest", headers=AUTH, json={
        "query": "iphone", "location": "bangalore",
        "min_price": 5000, "max_price": 50000,
        "sort_by": "price_ascend", "days_since_listed": "1",
    })

    sent = h.started[0]["run_input"]
    assert sent["minPrice"] == 5000
    assert sent["maxPrice"] == 50000
    assert sent["sortBy"] == "price_ascend"
    assert sent["daysSinceListed"] == "1"
    assert sent["location"] == "bangalore"


def test_10_automated_filters_reach_the_actor_and_default_to_newest(client, h):
    """Freshness is the default, not something you have to remember."""
    arm(h, runs=1)
    h.filters = {"minPrice": 1000}

    client.post("/api/automation/tick", headers=AUTH)

    sent = h.started[0]["run_input"]
    assert sent["minPrice"] == 1000
    assert sent["sortBy"] == "creation_time_descend"
    assert sent["daysSinceListed"] == "7"


# ==========================================================================
# 11-12  the kill switch, mid-run
# ==========================================================================

def test_11_kill_switch_during_a_manual_run_still_collects_what_was_paid_for(
    client, h
):
    """Stopping must not throw away a page already charged for."""
    client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})
    client.post("/api/automation/stop")

    out = client.post("/api/ingest/collect", headers=AUTH).json()

    assert out["ran"] is True
    assert h.state.is_active is False


def test_12_kill_switch_during_automation_blocks_the_next_start(client, h):
    arm(h, runs=5)
    client.post("/api/automation/tick", headers=AUTH)   # one start
    client.post("/api/automation/tick", headers=AUTH)   # collect it
    started_before = len(h.started)

    client.post("/api/automation/stop")
    body = client.post("/api/automation/tick", headers=AUTH).json()

    assert body["ran"] is False
    assert len(h.started) == started_before        # no further spend


# ==========================================================================
# 13-15  the lock always comes back
# ==========================================================================

def test_13_lock_releases_after_a_successful_manual_run(client, h):
    client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})
    assert h.lock_holder is None                   # released on the way out

    client.post("/api/ingest/collect", headers=AUTH)
    arm(h, runs=1)
    body = client.post("/api/automation/tick", headers=AUTH).json()
    assert body.get("started") is True


def test_14_lock_releases_when_the_scraper_raises(client, h, monkeypatch):
    """A lock released only on the happy path deadlocks on the first bad run."""
    async def boom(*a, **kw):
        raise RuntimeError("actor exploded")

    monkeypatch.setattr("vingo_scraper.apify.client.start_run", boom)

    with pytest.raises(RuntimeError, match="actor exploded"):
        client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})

    # The point of the test: the failure escaped, the lock did not.
    assert h.lock_holder is None
    assert h.lock_at is None


def test_15_an_orphaned_lock_expires_rather_than_blocking_forever(client, h):
    h.lock_holder = "automation"
    h.lock_at = datetime.now(UTC) - timedelta(seconds=3600)

    r = client.post("/api/ingest", headers=AUTH, json={"query": "iphone"})

    assert r.status_code == 200
    assert len(h.started) == 1


# ==========================================================================
# 16-17  the detail modal payload
# ==========================================================================

def test_16_detail_payload_carries_every_image_url(client, monkeypatch):
    """The no-fbcdn invariant is only checkable if the URLs are actually there."""
    import uuid

    from vingo_scraper.extract.schema import ImageStatus

    lid = uuid.uuid4()

    class Img:
        def __init__(self, n):
            self.storage_url = f"https://storage.googleapis.com/b/img{n}.jpg"
            self.is_primary = n == 0
            self.status = ImageStatus.STORED
            self.size_bytes = 1000
            self.content_type = "image/jpeg"

    payload = _fake_detail(monkeypatch, lid, images=[Img(0), Img(1), Img(2)])
    out = client.get(f"/api/listings/{lid}").json()

    assert len(out["images"]) == 3
    assert all(i["url"] for i in out["images"])
    assert not any("fbcdn" in i["url"] for i in out["images"])
    assert payload is not None


def test_17_detail_payload_handles_a_missing_seller(client, monkeypatch):
    """Seller is absent on ~93.6% of listings. Absence is normal, not an error."""
    import uuid

    lid = uuid.uuid4()
    _fake_detail(monkeypatch, lid, images=[], seller=None)

    r = client.get(f"/api/listings/{lid}")

    assert r.status_code == 200
    assert r.json()["seller"] is None


def _fake_detail(monkeypatch, lid, images, seller="absent"):
    """Stub the detail read without a database."""
    from vingo_scraper.extract.schema import Condition, ReviewStatus

    class Row:
        id = lid
        fb_listing_id = "123"
        title = "iPhone 13 128GB"
        description = "128gb, no scratches, box included. Moving abroad."
        price_minor = 4_500_000
        currency = "INR"
        condition = Condition.USED_GOOD
        city = "bangalore"
        location_text = "Koramangala"
        category = "mobile_phones"
        fb_category_id = "99"
        attributes = {"brand": "Apple"}
        quality_score = 82
        quality_flags = []
        description_facts = {"specs": {"storage": "128 GB"}}
        listing_url = "https://www.facebook.com/marketplace/item/123"
        posted_at = None
        first_seen_at = datetime.now(UTC)
        last_seen_at = datetime.now(UTC)
        review_status = ReviewStatus.PENDING_REVIEW
        is_sold = False
        is_pending = False
        seller_id = None

    async def fake(listing_id):
        return {
            "id": str(lid), "fb_listing_id": Row.fb_listing_id,
            "title": Row.title, "description": Row.description,
            "price_inr": 45000.0, "currency": "INR",
            "condition": Row.condition.value, "city": Row.city,
            "location": Row.location_text, "category": Row.category,
            "fb_category_id": Row.fb_category_id, "attributes": Row.attributes,
            "quality_score": Row.quality_score, "quality_flags": Row.quality_flags,
            "description_facts": Row.description_facts,
            "listing_url": Row.listing_url, "posted_at": None,
            "first_seen_at": Row.first_seen_at.isoformat(),
            "last_seen_at": Row.last_seen_at.isoformat(),
            "review_status": Row.review_status.value,
            "is_sold": False, "is_pending": False,
            "seller": None if seller is None else {
                "name": "Khan Kh", "profile_url": "https://facebook.com/pfbid0",
                "phone": None, "email": None, "rating": None,
            },
            "images": [{
                "url": i.storage_url, "is_primary": i.is_primary,
                "status": i.status.value, "size_bytes": i.size_bytes,
                "content_type": i.content_type,
            } for i in images],
            "price_history": [],
        }

    monkeypatch.setattr("vingo_scraper.api.main._listing", fake)
    return fake


# ==========================================================================
# 18-20  extraction, dedup, budget
# ==========================================================================

def test_18_description_extraction_pulls_structure_out_of_prose():
    """Specs, condition nuance and sale reason, from one real-shaped string."""
    from vingo_scraper.extract.description import extract

    facts = extract(
        "iPhone 13 128GB Midnight",
        "128gb variant, no scratches at all, battery health 92%, "
        "with original box and charger, under warranty till 2027. "
        "Selling because I am shifting to Dubai. Urgent sale.",
    )

    assert facts.specs["storage"] == "128 GB"
    assert facts.specs["colour"] == "midnight"
    assert facts.condition_notes["scratches"] == "none"
    assert facts.condition_notes["battery_health"] == "92%"
    assert facts.condition_notes["box"] == "included"
    assert facts.condition_notes["warranty"] == "in warranty"
    assert facts.sale_reason == "relocating"
    assert facts.urgency == "urgent"


@pytest.mark.anyio
async def test_19_known_listings_skip_image_work_entirely(monkeypatch):
    """Dedup before rehosting. Saves bandwidth and storage -- NOT Apify spend.

    The Actor charges when it scrapes, long before an id reaches us, so this
    cannot prevent re-billing; `onlyNewListings` on the Actor is what does.
    The saving here is real but it is image work, and the test says so.
    """
    from vingo_scraper.apify import runner

    class L:
        def __init__(self, fid):
            self.fb_listing_id = fid
            self.images = [object(), object()]

    listings = [L("a"), L("b"), L("c")]

    async def fake_split(items):
        known = [x for x in items if x.fb_listing_id in {"a", "b"}]
        fresh = [x for x in items if x.fb_listing_id == "c"]
        return fresh, known

    touched = []

    async def fake_touch(items):
        touched.extend(x.fb_listing_id for x in items)

    monkeypatch.setattr(runner, "_split_known", fake_split)
    monkeypatch.setattr(runner, "_touch_last_seen", fake_touch)

    fresh, known = await fake_split(listings)
    await fake_touch(known)

    assert [x.fb_listing_id for x in fresh] == ["c"]
    assert sorted(touched) == ["a", "b"]
    # Two listings x two images each never downloaded.
    assert sum(len(x.images) for x in known) == 4


def test_20_repeated_polling_does_not_multiply_budget_reads(client, h):
    """The console polls every 5s; reads must stay cheap and consistent."""
    seen = set()
    for _ in range(15):
        d = client.get("/api/automation").json()
        seen.add((d["cursor"], d["next_cell"], d["est_tick_usd"]))

    assert len(seen) == 1                      # one answer, every time
    # Snapshot reads must not trigger a reconciliation pass each poll --
    # that is one Apify API call per 5 seconds, per open tab.
    assert h.reconcile_calls == 0


# --------------------------------------------------------------------------
# engine shape -- the Supabase workarounds must not follow us onto the VM
# --------------------------------------------------------------------------

def test_pooler_detection_distinguishes_supavisor_from_plain_postgres():
    """Both workarounds are correct through a pooler and wrong without one."""
    from vingo_scraper.db.session import is_pooled

    assert is_pooled("postgresql+psycopg://u:p@aws-0-x.pooler.supabase.com:6543/postgres")
    assert is_pooled("postgresql+psycopg://u:p@host:6543/db")
    # The VM: Postgres on localhost, no pooler in front of it.
    assert not is_pooled("postgresql+psycopg://comet:p@127.0.0.1:5432/comet")


def test_local_engine_pools_and_keeps_prepared_statements():
    """NullPool would reconnect per request; disabling prepares is free loss."""
    from sqlalchemy.pool import NullPool

    from vingo_scraper.db.session import build_engine, engine_options

    connect_args, kwargs = engine_options(
        "postgresql+psycopg://comet:p@127.0.0.1:5432/comet"
    )
    assert "prepare_threshold" not in connect_args
    assert kwargs["pool_size"] == 5
    assert kwargs["pool_pre_ping"] is True

    engine = build_engine("postgresql+psycopg://comet:p@127.0.0.1:5432/comet")
    assert not isinstance(engine.pool, NullPool)


def test_pooled_engine_keeps_both_workarounds():
    """Supavisor still needs them -- do not "simplify" this away."""
    from sqlalchemy.pool import NullPool

    from vingo_scraper.db.session import build_engine, engine_options

    connect_args, kwargs = engine_options(
        "postgresql+psycopg://u:p@aws-0.pooler.supabase.com:6543/postgres"
    )
    # None DISABLES prepared statements; 0 would prepare immediately.
    assert connect_args["prepare_threshold"] is None
    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs

    engine = build_engine("postgresql+psycopg://u:p@host:6543/postgres")
    assert isinstance(engine.pool, NullPool)


# --------------------------------------------------------------------------
# the pipeline must run against the REAL schema, not a stand-in
# --------------------------------------------------------------------------

def _real_listing(**kw):
    from vingo_scraper.extract.schema import ExtractionMethod, Listing, ListingImage

    base = dict(
        fb_listing_id="1", listing_url="https://fb.com/marketplace/item/1",
        title="iPhone 13 128GB Midnight",
        description="128gb, no scratches, box included. Shifting to Dubai.",
        price_minor=4_500_000,
        images=[ListingImage(source_url=f"https://x/{i}.jpg") for i in range(3)],
        extraction_method=ExtractionMethod.APIFY,
    )
    base.update(kw)
    return Listing(**base)


def test_21_extract_and_quality_attach_to_a_real_pydantic_listing():
    """The gap that reached production.

    Pydantic rejects assignment of undeclared attributes, so
    `item.description_facts = ...` raised `ValueError: "Listing" object has no
    field "description_facts"` on the first live run -- after Apify had been
    charged. Every test here used plain stand-in objects, which accept any
    attribute, so none of them could ever have caught it.
    """
    from vingo_scraper.extract.description import extract_all
    from vingo_scraper.pipeline.quality import assess_all

    listings = [_real_listing()]
    extract_all(listings)
    listings, summary = assess_all(listings)

    item = listings[0]
    assert item.description_facts.specs["storage"] == "128 GB"
    assert item.description_facts.condition_notes["scratches"] == "none"
    assert item.quality.score > 0
    assert summary["assessed"] == 1


def test_22_a_real_listing_survives_the_whole_pre_persist_pipeline():
    """Parse -> extract -> assess, in the order `ingest_collected` runs them."""
    from vingo_scraper.extract.description import extract_all
    from vingo_scraper.pipeline.quality import assess_all

    good = _real_listing()
    thin = _real_listing(fb_listing_id="2", description="x", images=[],
                         price_minor=100)

    items = [good, thin]
    extract_all(items)
    items, summary = assess_all(items)

    assert good.quality.passed is True
    # Thin listing: too few photos, description under 15 chars, Rs 1 price.
    assert thin.quality.passed is False
    assert len(thin.quality.reasons) == 3
    assert summary["flagged"] == 1
    # Flagged, never dropped -- the reject rate has to stay measurable.
    assert len(items) == 2


def test_23_photo_and_description_gates_skip_when_details_not_requested():
    """The gate flagged 24 of 24 on the first real VM run.

    Not 24 bad listings -- `include_details: false` makes the Actor return only
    the primary photo, so "fewer than 2 photos" was measuring our own cost
    setting. Penalising a listing for data we chose not to buy is a broken
    signal, so photos leave the score entirely in that case.
    """
    from vingo_scraper.pipeline.quality import assess

    one_photo = _real_listing()
    one_photo.images = one_photo.images[:1]

    cheap = assess(one_photo, details_known=False)
    full = assess(one_photo, details_known=True)

    assert cheap.passed is True
    assert not any("photo" in r for r in cheap.reasons)
    # The description is empty for the same reason and must not be judged
    # either -- that was the half of the bug the first fix missed.
    assert not any("description" in r for r in cheap.reasons)
    assert full.passed is False
    assert any("photo" in r for r in full.reasons)
    # Rescaled, so a cheap run stays comparable with a detailed one.
    assert cheap.score > full.score


def test_24_score_stays_on_a_0_100_scale_either_way():
    from vingo_scraper.pipeline.quality import assess

    # Real prose, not "x" * 200 -- a run of identical characters correctly
    # trips the spam pattern, which would be the fixture failing, not the gate.
    rich = _real_listing(
        description=(
            "iPhone 13 128GB in midnight black, bought January 2024 from an "
            "authorised Apple reseller. Battery health is 92 percent and the "
            "screen has no scratches whatsoever. Comes with the original box, "
            "charging cable and the purchase bill. Selling only because I am "
            "relocating to Dubai next month and cannot carry it."
        ),
        attributes={"brand": "Apple", "model": "13", "storage": "128GB"},
    )
    for details_known in (True, False):
        rep = assess(rich, details_known=details_known)
        assert 0 <= rep.score <= 100

    # A strong listing scores high whichever signals were observable, and the
    # two stay within a few points of each other. That closeness IS the point
    # of rescaling: a cheap run and a detailed one have to remain comparable,
    # which they were not before -- photos-off capped every listing at 60.
    cheap = assess(rich, details_known=False).score
    full = assess(rich, details_known=True).score
    assert cheap >= 90 and full >= 90
    assert abs(cheap - full) <= 10


def test_25_runner_tells_the_gate_what_the_actor_was_asked_for():
    """The wiring, not just the gate: a cheap run must set details_known=False."""
    import inspect

    from vingo_scraper.apify import runner

    src = inspect.getsource(runner.ingest_collected)
    assert "getListingDetails" in src
    assert "details_known=details_known" in src


def test_26_seller_id_column_fits_a_real_pfbid():
    """A pfbid is ~73 chars and overflowed String(64) on the first run that
    actually returned a seller -- after Apify had been charged for the page.

    The length is asserted against a real captured value rather than a round
    number, because the round number is what was wrong.
    """
    from vingo_scraper.db import models

    real = "pfbid02nMjZE2MMvAAXtfBad9Hm6PLTiCcTweTfeb3xp1jzN2QMP4VWRPoXLeZ52fgnXjbSl"
    assert len(real) > 64
    column = models.Seller.__table__.c.fb_seller_id
    assert column.type.length >= len(real)


def test_27_actor_side_cache_keys_are_sent():
    """The only mechanism that avoids re-billing.

    Our fb_listing_id check runs after the money is spent -- Apify charges
    when it scrapes, before any id reaches us. A repeat query with the cache
    off cost $0.01205 and returned 24 listings we already held.
    """
    from vingo_scraper.apify.client import CURIOUS_CODER, build_run_input
    from vingo_scraper.config import settings

    payload = build_run_input("iphone", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert payload["onlyNewListings"] is True
    assert payload["cacheStorageId"] == settings.apify_cache_store_id


def test_28_empty_cache_id_forces_a_full_rescrape(monkeypatch):
    """A demo needs listings arriving, not a correct but empty run."""
    from vingo_scraper.apify.client import CURIOUS_CODER, build_run_input

    monkeypatch.setattr("vingo_scraper.config.settings.apify_cache_store_id", "")
    payload = build_run_input("iphone", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert "onlyNewListings" not in payload
    assert "cacheStorageId" not in payload


def test_29_legacy_actor_never_sees_the_cache_keys():
    from vingo_scraper.apify.client import build_run_input

    payload = build_run_input(
        "iphone", "bangalore", 8, actor_id="apify/facebook-marketplace-scraper"
    )
    assert "cacheStorageId" not in payload
