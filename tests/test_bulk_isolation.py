"""The Bulk tab shows THIS sweep, and says when Marketplace is empty.

Two failures these pin, both of which read as something else:

  * every bulk row ever ingested rendered as the current sweep's haul, so a
    46-row sweep looked like it had found hundreds
  * a sweep stopping at 46 of 1000 because Marketplace had no more looked
    identical to a crash, and the Run button stayed live
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from vingo_scraper.api import main as api
from vingo_scraper.pipeline.quota import should_stop
from vingo_scraper.pipeline.sweep import SweepState


def console() -> str:
    return TestClient(api.app).get("/").text


# --------------------------------------------------------------------------
# scoping one sweep
# --------------------------------------------------------------------------

@pytest.fixture
def seen(monkeypatch):
    async def fake(limit, since_run, **kw):
        fake.kw = {"limit": limit, "since_run": since_run, **kw}
        return {"listings": [], "count": 0}

    monkeypatch.setattr(api, "_listings", fake)
    return fake


def test_01_since_ts_reaches_the_query(seen) -> None:
    TestClient(api.app).get("/api/listings?since_ts=2026-10-12T09:00:00%2B00:00")
    assert seen.kw["since_ts"] == "2026-10-12T09:00:00+00:00"


def test_02_absent_since_ts_still_shows_everything(seen) -> None:
    """The Catalog must keep behaving exactly as it did -- the filter is what
    changed, not the default."""
    TestClient(api.app).get("/api/listings")
    assert seen.kw["since_ts"] is None


def test_03_the_export_carries_the_same_scope(seen) -> None:
    """An export that silently covers a different set than the screen is a
    spreadsheet nobody can reconcile."""
    TestClient(api.app).get("/api/export.csv?since_ts=2026-10-12T09:00:00%2B00:00")
    assert seen.kw["since_ts"] == "2026-10-12T09:00:00+00:00"


def test_04_a_sweep_stamps_its_window_before_the_first_cell() -> None:
    """Stamped after the first cell started, a run could fall outside its own
    sweep's window and vanish from the panel."""
    src = inspect.getsource(api.sweep_start)
    assert "started_at=datetime.now(UTC).isoformat()" in src
    assert src.index("started_at") < src.index("_drive_sweep")


def test_05_resume_keeps_the_original_window() -> None:
    """A resumed sweep is the same sweep. Re-stamping would hide every cell
    already paid for."""
    src = inspect.getsource(api.sweep_resume)
    assert "if not state.started_at:" in src


def test_06_an_unparseable_timestamp_narrows_rather_than_widens() -> None:
    """A filter that fails open shows everything while looking filtered --
    which is how the last sweep's rows got read as this one's."""
    src = inspect.getsource(api._listings)
    assert "sa_false()" in src


def test_07_the_bulk_panel_sends_the_window(seen) -> None:
    html = console()
    assert "since_ts=" in html
    assert "__sweepSince" in html


def test_08_the_state_endpoint_publishes_the_window() -> None:
    src = inspect.getsource(api.sweep_state)
    assert '"started_at": state.started_at' in src


# --------------------------------------------------------------------------
# exhaustion
# --------------------------------------------------------------------------

def test_09_exhausted_is_its_own_kind() -> None:
    kind, _ = should_stop(obtained=46, target=1000, spent_usd=0.5,
                          budget_usd=5, cells_left=0)
    assert kind == "exhausted"


def test_10_a_full_target_is_not_exhaustion() -> None:
    """Success and "Marketplace is empty" must never render the same."""
    kind, _ = should_stop(obtained=1000, target=1000, spent_usd=0.5,
                          budget_usd=5, cells_left=0)
    assert kind == "target"


def test_11_running_out_of_money_is_not_exhaustion() -> None:
    """Budget is OUR limit and is fixable by raising the cap. Supply is not."""
    kind, _ = should_stop(obtained=46, target=1000, spent_usd=9.0,
                          budget_usd=5, cells_left=0)
    assert kind == "budget"


def test_12_the_kind_survives_a_round_trip_through_jsonb() -> None:
    """State lives in system_config, never in memory -- an in-process copy
    alternates between lambdas and the UI flickers between two self-consistent
    states."""
    s = SweepState(target=1000, location="bangalore",
                   stopped_kind="exhausted", stopped_reason="...",
                   started_at="2026-10-12T09:00:00+00:00")
    back = SweepState.from_dict(s.to_dict())
    assert back.stopped_kind == "exhausted"
    assert back.started_at == "2026-10-12T09:00:00+00:00"


def test_13_the_console_branches_on_the_kind_not_the_sentence() -> None:
    """Matching prose breaks the first time a message is reworded, and the
    breakage is silent."""
    html = console()
    assert "stopped_kind === 'exhausted'" in html


def test_14_exhaustion_disables_run(seen) -> None:
    """The whole point: stop the operator mashing Run and paying again for
    pages Marketplace has already given us."""
    html = console()
    assert "disabled = running || exhausted" in html


def test_15_exhaustion_says_a_bigger_target_will_not_help(seen) -> None:
    html = console()
    assert "A bigger target will not help" in html
