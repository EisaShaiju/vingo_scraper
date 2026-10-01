"""Automation state must not live in process memory.

The bug this pins: the console's Automation panel kept its state in a
module-level object. Vercel runs several lambda instances behind one URL, so
polling `/api/scheduler` every 5s returned whichever instance happened to
answer -- the one that received `start` said `running`, cursor 2, $0.0620; a
cold one said `idle`, cursor 1, $0.1240. The UI alternated between two
self-consistent views roughly every 15-20 seconds.

Nothing errored. That is what made it expensive to diagnose, and it is why
these tests assert on *agreement between independent reads* rather than on any
single value being correct.

The same split is why Stop did not work: the button stopped one instance's
loop, and the kill switch in `system_config` governed a different code path
entirely, so `runs_completed` stayed 0 while the panel counted runs.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vingo_scraper.api.main import app
from vingo_scraper.apify.budget import BudgetStatus
from vingo_scraper.db.config_store import DemoState

SECRET = "test-secret-token"


def make_state(**kw) -> DemoState:
    base = DemoState(
        is_active=True, runs_remaining=2, runs_completed=1,
        interval_seconds=180, search_query="iphone", location="bengaluru",
        max_pages=1, sequence_started_at=None, last_run_at=None,
        last_status="running", stopped_reason=None,
        cursor=1, per_cell=8, include_details=False,
        next_run_at=None, city="bangalore",
    )
    return replace(base, **kw)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("vingo_scraper.config.settings.demo_secret", SECRET)
    return TestClient(app)


@pytest.fixture
def stubbed(monkeypatch):
    """Serve automation state from a dict standing in for the table."""
    row = {"state": make_state()}

    async def get_state():
        return row["state"]

    async def stop_sequence(reason="stopped by operator"):
        row["state"] = replace(row["state"], is_active=False,
                               runs_remaining=0, stopped_reason=reason)
        return row["state"]

    async def recent_ticks(limit=12):
        return []

    async def get_budget():
        return BudgetStatus(
            monthly_used_usd=1.5861, total_cap_usd=7.0,
            daily_used_usd=0.0001, daily_cap_usd=2.0,
        )

    monkeypatch.setattr("vingo_scraper.db.config_store.get_state", get_state)
    monkeypatch.setattr("vingo_scraper.db.config_store.stop_sequence", stop_sequence)
    monkeypatch.setattr("vingo_scraper.db.config_store.recent_ticks", recent_ticks)
    monkeypatch.setattr("vingo_scraper.apify.budget.get_budget", get_budget)
    return row


# --------------------------------------------------------------------------
# the reported bug
# --------------------------------------------------------------------------

def test_repeated_polls_agree(client, stubbed):
    """Twenty polls, one answer. This is the test the old build failed."""
    seen = [client.get("/api/automation").json() for _ in range(20)]
    first = seen[0]
    for other in seen[1:]:
        assert other == first


def test_snapshot_survives_a_cold_module(client, stubbed, monkeypatch):
    """A freshly imported module, reading the same table, must agree.

    A fresh import is what a cold lambda does. The old scheduler rebuilt its
    dataclass defaults at this point -- running False, cursor 0,
    include_details True -- which is exactly the second screenshot.

    The reload here discards the stubs, so they are reapplied: the storage is
    held constant and only the module object changes, which is the thing under
    test.
    """
    before = client.get("/api/automation").json()

    import importlib

    import vingo_scraper.db.config_store as cs
    reloaded = importlib.reload(cs)

    async def get_state():
        return stubbed["state"]

    async def recent_ticks(limit=12):
        return []

    monkeypatch.setattr(reloaded, "get_state", get_state)
    monkeypatch.setattr(reloaded, "recent_ticks", recent_ticks)

    after = client.get("/api/automation").json()
    assert after == before


def test_module_holds_no_automation_state():
    """No module-level mutable object may carry automation state again.

    This is the shape of the original bug, not a specific value: anything
    stateful at import time gets one copy per lambda instance.
    """
    import vingo_scraper.db.config_store as cs

    stateful = [
        name for name, value in vars(cs).items()
        if not name.startswith("__")
        and isinstance(value, (list, dict, set))
        and not callable(value)
    ]
    assert stateful == []


def test_no_in_process_scheduler_module():
    """The duplicate mechanism is gone, not merely unused."""
    with pytest.raises(ImportError):
        __import__("vingo_scraper.api.scheduler")


# --------------------------------------------------------------------------
# the money on screen
# --------------------------------------------------------------------------

def test_spend_today_is_daily_not_monthly(client, stubbed):
    """The panel showed $1.56 of a $2.00 DAILY cap on a day that spent $0.0001.

    `snapshot()` was handed `monthly_used_usd` and the UI compared it to the
    daily cap. Two caps, two numbers, never crossed.
    """
    d = client.get("/api/automation").json()
    assert d["spend_today_usd"] == pytest.approx(0.0001)
    assert d["spend_today_usd"] != pytest.approx(1.5861)
    assert d["daily_cap_usd"] == pytest.approx(2.0)
    # The monthly figure is still reachable, against its own cap.
    assert d["budget"]["monthly_used_usd"] == pytest.approx(1.5861)
    assert d["budget"]["total_cap_usd"] == pytest.approx(7.0)


def test_cost_per_run_matches_the_configured_actor(client, stubbed):
    from vingo_scraper.pipeline.sweep import actor_pricing

    d = client.get("/api/automation").json()
    listing, _detail = actor_pricing()
    assert d["est_tick_usd"] == pytest.approx(8 * listing)


def test_grid_is_bengaluru(client, stubbed):
    d = client.get("/api/automation").json()
    assert d["next_cell"].startswith("bangalore")
    assert d["cells_total"] == 5


# --------------------------------------------------------------------------
# stopping
# --------------------------------------------------------------------------

def test_stop_needs_no_token(client, stubbed):
    """A kill switch you can fail to authenticate to is not a kill switch."""
    r = client.post("/api/automation/stop")
    assert r.status_code == 200
    assert r.json()["running"] is False


def test_stop_is_visible_to_the_next_read(client, stubbed):
    """Stop must hold for every reader, not just the instance that got it."""
    assert client.get("/api/automation").json()["running"] is True
    client.post("/api/automation/stop")
    for _ in range(5):
        assert client.get("/api/automation").json()["running"] is False


def test_start_requires_the_secret(client, stubbed):
    """Starting spends money, so it stays gated."""
    assert client.post("/api/automation/start", json={}).status_code == 401
    assert client.post("/api/automation/tick").status_code == 401


def test_read_degrades_instead_of_500(client, monkeypatch):
    """The console polls this every 5s; a database blip must not break it."""
    async def boom():
        raise RuntimeError("pooler is down")

    monkeypatch.setattr("vingo_scraper.db.config_store.get_state", boom)
    r = client.get("/api/automation")
    assert r.status_code == 200
    assert r.json()["running"] is False
