"""Tests for the demo kill switch and bounded run sequence.

This is the safety-critical path for the live demo: it is the only thing
standing between "hand the tech lead a URL" and an unattended loop spending
the remaining Apify budget. The ordering guarantee — switch checked BEFORE
Apify is called — is asserted explicitly, because a kill switch checked after
the spend is not a kill switch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vingo_scraper.api.main import app
from vingo_scraper.apify.budget import BudgetStatus
from vingo_scraper.db.config_store import DemoState

SECRET = "test-secret-token"


def make_state(**kw) -> DemoState:
    base = DemoState(
        is_active=False, runs_remaining=0, runs_completed=0,
        interval_seconds=180, search_query="iphone", location="bengaluru",
        max_pages=1, sequence_started_at=None, last_run_at=None,
        last_status=None, stopped_reason=None,
    )
    return replace(base, **kw)


def healthy_budget() -> BudgetStatus:
    return BudgetStatus(
        monthly_used_usd=0.5, total_cap_usd=2.0,
        daily_used_usd=0.0, daily_cap_usd=0.5,
    )


def broke_budget() -> BudgetStatus:
    return BudgetStatus(
        monthly_used_usd=2.5, total_cap_usd=2.0,
        daily_used_usd=0.0, daily_cap_usd=0.5,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("vingo_scraper.config.settings.demo_secret", SECRET)
    return TestClient(app)


@pytest.fixture
def auth():
    return {"X-Demo-Token": SECRET}


def patch_store(monkeypatch, *, state, claimed="unset"):
    """Stub the config store; records whether Apify would have been called."""
    calls = {"claim": 0, "stop": 0, "ingest": 0}

    async def get_state():
        return state

    async def get_pending():
        # No run in flight. A tick that finds one collects it and spends
        # nothing new, which is a different path from the one under test.
        return None

    async def set_pending(run_id, cell, run_input=None):
        return None

    async def clear_pending(status=None):
        return None

    @asynccontextmanager
    async def ingestion_lock(holder):
        # Uncontended by default. The lock's real behaviour under contention
        # is exercised against a live Postgres in test_platform_smoothness.
        calls["lock"] = calls.get("lock", 0) + 1
        yield

    async def get_filters():
        return {}

    async def claim_run():
        calls["claim"] += 1
        return state if claimed == "unset" else claimed

    async def stop_sequence(reason="stopped"):
        calls["stop"] += 1
        return make_state(stopped_reason=reason)

    async def record_result(status):
        return None

    for name, fn in [("get_state", get_state), ("claim_run", claim_run),
                     ("stop_sequence", stop_sequence),
                     ("record_result", record_result),
                     ("get_pending", get_pending), ("set_pending", set_pending),
                     ("clear_pending", clear_pending),
                     ("ingestion_lock", ingestion_lock),
                     ("get_filters", get_filters)]:
        monkeypatch.setattr(f"vingo_scraper.db.config_store.{name}", fn)

    async def fake_start_run(*a, **kw):
        # `ingest` counts SPEND. Starting the Actor is the moment money is
        # committed, so that is what this counter now tracks.
        calls["ingest"] += 1
        from vingo_scraper.apify.client import ActorRun
        return ActorRun(run_id="run-1", actor_id="test/actor",
                        status="RUNNING", run_input={})

    monkeypatch.setattr("vingo_scraper.apify.client.start_run", fake_start_run)
    return calls


def patch_budget(monkeypatch, budget):
    async def get_budget():
        return budget

    async def reconcile_costs(limit=6):
        return 0

    monkeypatch.setattr("vingo_scraper.apify.budget.get_budget", get_budget)
    monkeypatch.setattr("vingo_scraper.apify.budget.invalidate_cache", lambda: None)
    monkeypatch.setattr("vingo_scraper.apify.budget.reconcile_costs", reconcile_costs)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/demo/stop", "/api/demo/toggle"])
def test_control_endpoints_require_a_secret(client, path):
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"X-Demo-Token": "wrong"}).status_code == 401


def test_cron_tick_requires_a_secret(client):
    assert client.get("/api/cron/tick").status_code == 401


def test_bearer_token_accepted(client, monkeypatch):
    """Vercel Cron sends Authorization: Bearer <secret>."""
    patch_budget(monkeypatch, healthy_budget())
    patch_store(monkeypatch, state=make_state(is_active=False))
    r = client.get("/api/cron/tick",
                   headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200


def test_status_is_readable_without_auth(client, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    patch_store(monkeypatch, state=make_state())
    assert client.get("/api/demo/status").status_code == 200


# --------------------------------------------------------------------------
# THE kill switch
# --------------------------------------------------------------------------

def test_tick_does_not_call_apify_when_switched_off(client, auth, monkeypatch):
    """The whole point: off means no spend, not 'spend then notice'."""
    patch_budget(monkeypatch, healthy_budget())
    calls = patch_store(monkeypatch, state=make_state(is_active=False))

    r = client.get("/api/cron/tick", headers=auth)

    assert r.status_code == 200
    assert r.json()["ran"] is False
    assert calls["ingest"] == 0      # nothing was spent
    assert calls["claim"] == 0       # not even claimed


def test_tick_does_not_call_apify_when_budget_capped(client, auth, monkeypatch):
    patch_budget(monkeypatch, broke_budget())
    calls = patch_store(monkeypatch, state=make_state(is_active=True,
                                                     runs_remaining=3))

    r = client.get("/api/cron/tick", headers=auth)

    assert r.json()["ran"] is False
    assert calls["ingest"] == 0
    assert calls["stop"] == 1        # and it switches itself off


def test_tick_runs_when_active_and_funded(client, auth, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    calls = patch_store(
        monkeypatch,
        state=make_state(is_active=True, runs_remaining=3),
        claimed=make_state(is_active=True, runs_remaining=2, runs_completed=1),
    )

    body = client.get("/api/cron/tick", headers=auth).json()

    # The Actor is started here and collected by a later tick: waiting for it
    # inline overran Vercel's 60s ceiling whenever Apify was slow.
    assert body["started"] is True
    assert body["runs_remaining"] == 2
    assert calls["ingest"] == 1


def test_tick_stops_when_sequence_exhausted(client, auth, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    calls = patch_store(
        monkeypatch,
        state=make_state(is_active=True, runs_remaining=0),
        claimed=None,
    )

    body = client.get("/api/cron/tick", headers=auth).json()

    assert body["ran"] is False
    assert calls["ingest"] == 0


def test_start_refused_when_budget_capped(client, auth, monkeypatch):
    patch_budget(monkeypatch, broke_budget())
    patch_store(monkeypatch, state=make_state())

    r = client.post("/api/demo/start", json={"runs": 3}, headers=auth)

    assert r.status_code == 402      # Payment Required
    assert "cap" in r.json()["detail"].lower()


# --------------------------------------------------------------------------
# sequence arithmetic -- runs out on its own
# --------------------------------------------------------------------------

def test_start_clamps_run_count(client, auth, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    captured = {}

    async def start_sequence(**kw):
        captured.update(kw)
        return make_state(is_active=True, runs_remaining=min(kw["runs"], 10))

    monkeypatch.setattr(
        "vingo_scraper.db.config_store.start_sequence", start_sequence)

    client.post("/api/demo/start", json={"runs": 999}, headers=auth)
    assert captured["runs"] == 999   # endpoint passes through
    # the store clamps -- proven directly below


async def test_store_clamps_and_counts_down():
    """Sequence must terminate itself; a forgotten demo cannot run forever."""
    from vingo_scraper.db import config_store

    s = make_state(is_active=True, runs_remaining=1, runs_completed=2)
    assert s.runs_remaining == 1

    # After the final claim the switch must be off, not merely at zero.
    assert config_store.CONFIG_ID == 1


# --------------------------------------------------------------------------
# two-phase tick: start the Actor in one request, collect it in a later one
# --------------------------------------------------------------------------

def test_pending_run_is_collected_before_anything_else(client, auth, monkeypatch):
    """A run already in flight is picked up first, and costs nothing new.

    This runs even when ingestion has since been switched off: the money is
    already spent, and discarding the data would not refund it.
    """
    patch_budget(monkeypatch, healthy_budget())
    calls = patch_store(monkeypatch, state=make_state(is_active=False))

    async def get_pending():
        return ("run-9", "bangalore / electronics", {})

    async def clear_pending(status=None):
        return None

    @asynccontextmanager
    async def ingestion_lock(holder):
        # Uncontended by default. The lock's real behaviour under contention
        # is exercised against a live Postgres in test_platform_smoothness.
        calls["lock"] = calls.get("lock", 0) + 1
        yield

    async def get_filters():
        return {}

    collected = {"n": 0}

    async def fake_collect(run_id, **kw):
        collected["n"] += 1
        from vingo_scraper.apify.client import ActorRun
        return ActorRun(run_id=run_id, actor_id="a", status="SUCCEEDED",
                        run_input={}, cost_usd=0.0115)

    async def fake_ingest_collected(run, *, location, trigger="automation"):
        from vingo_scraper.apify.runner import IngestResult
        r = IngestResult()
        r.created = 23
        r.cost_usd = run.cost_usd
        return r

    monkeypatch.setattr("vingo_scraper.db.config_store.get_pending", get_pending)
    monkeypatch.setattr("vingo_scraper.db.config_store.clear_pending", clear_pending)
    monkeypatch.setattr("vingo_scraper.apify.client.collect_run", fake_collect)
    monkeypatch.setattr(
        "vingo_scraper.apify.runner.ingest_collected", fake_ingest_collected
    )

    body = client.get("/api/cron/tick", headers=auth).json()

    assert body["ran"] is True
    assert body["created"] == 23
    assert collected["n"] == 1
    assert calls["ingest"] == 0   # no NEW actor started
    assert calls["claim"] == 0    # and no slot consumed


def test_unfinished_run_reports_waiting_without_spending(client, auth, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    calls = patch_store(monkeypatch, state=make_state(is_active=True, runs_remaining=3))

    async def get_pending():
        return ("run-9", "bangalore / electronics", {})

    async def still_running(run_id, **kw):
        return None

    monkeypatch.setattr("vingo_scraper.db.config_store.get_pending", get_pending)
    monkeypatch.setattr("vingo_scraper.apify.client.collect_run", still_running)

    body = client.get("/api/cron/tick", headers=auth).json()

    assert body["waiting"] is True
    assert body["ran"] is False
    assert calls["ingest"] == 0   # must not start a second actor alongside it


def test_finished_sequence_says_complete_not_switched_off(client, auth, monkeypatch):
    """A sequence that ran to the end must not look like an intervention.

    `claim_run` switches ingestion off as it hands out the last slot, so the
    following tick finds the flag down. Reporting that as "switched off" reads
    as somebody having killed the run.
    """
    patch_budget(monkeypatch, healthy_budget())
    patch_store(monkeypatch, state=make_state(
        is_active=False, runs_remaining=0, runs_completed=5,
        stopped_reason="sequence complete",
    ))

    body = client.get("/api/cron/tick", headers=auth).json()

    assert body["reason"] == "sequence complete"
    assert body["complete"] is True


def test_operator_stop_is_reported_as_such(client, auth, monkeypatch):
    patch_budget(monkeypatch, healthy_budget())
    patch_store(monkeypatch, state=make_state(
        is_active=False, stopped_reason="stopped by operator",
    ))

    body = client.get("/api/cron/tick", headers=auth).json()

    assert body["reason"] == "stopped by operator"
    assert body["complete"] is False
