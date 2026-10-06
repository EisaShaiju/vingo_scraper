"""Outreach state and the CSV the team actually works from.

Offline like everything else: the database read is substituted, so these pin
the contract and the shaping, not Postgres.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from vingo_scraper.api import main as api

ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "title": 'Royal Enfield "Classic" 350, 2019',   # quotes: CSV escaping
    "price_inr": 145000.0,
    "location": "Indiranagar",
    "category": "vehicles",
    "posted_at": "2026-09-20T10:00:00+00:00",
    "image_count": 7,
    "quality_score": 88,
    "outreach_status": "contacted",
    "contacted_at": "2026-10-05T09:00:00+00:00",
    "outreach_note": "asked for pickup Sat",
    "listing_url": "https://www.facebook.com/marketplace/item/123",
    "lead": {
        "score": 72, "band": "hot",
        "signals": [
            {"label": "Price cut 12% (₹20,000)", "points": 30, "kind": "good"},
            {"label": "Seller is relocating", "points": 22, "kind": "good"},
        ],
    },
}


@pytest.fixture
def client(monkeypatch):
    async def fake_listings(limit, since_run, **kw):
        fake_listings.seen = {"limit": limit, "since_run": since_run, **kw}
        return {"listings": [ROW], "count": 1}

    monkeypatch.setattr(api, "_listings", fake_listings)
    return TestClient(api.app), fake_listings


# --------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------

def test_01_the_five_states_cover_a_real_conversation() -> None:
    assert api.OUTREACH_STATUSES == (
        "new", "contacted", "replied", "converted", "dead"
    )


def test_02_an_invented_status_is_refused() -> None:
    c = TestClient(api.app)
    r = c.post("/api/listings/11111111-1111-1111-1111-111111111111/outreach",
               json={"status": "maybe"})
    assert r.status_code == 400
    assert "status must be one of" in r.text


def test_03_the_refusal_names_the_allowed_values() -> None:
    """A 400 that does not say what IS allowed costs a round trip."""
    c = TestClient(api.app)
    r = c.post("/api/listings/11111111-1111-1111-1111-111111111111/outreach",
               json={"status": "nope"})
    for state in api.OUTREACH_STATUSES:
        assert state in r.text


def test_04_a_note_is_optional() -> None:
    from vingo_scraper.api.main import OutreachRequest
    assert OutreachRequest(status="contacted").note is None


def test_05_status_is_required() -> None:
    """Defaulting it would let a mis-shaped request silently mark a lead."""
    from pydantic import ValidationError

    from vingo_scraper.api.main import OutreachRequest
    with pytest.raises(ValidationError):
        OutreachRequest()


# --------------------------------------------------------------------------
# the export
# --------------------------------------------------------------------------

def test_06_export_is_a_csv_attachment(client) -> None:
    c, _ = client
    r = c.get("/api/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "vingo-leads.csv" in r.headers["content-disposition"]


def test_07_export_carries_the_score_and_the_reasons(client) -> None:
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["lead_score"] == "72"
    assert row["band"] == "hot"
    assert "Seller is relocating" in row["signals"]


def test_08_export_carries_the_contact_route(client) -> None:
    """With no phone or email anywhere, the listing URL IS the contact route.
    An export without it is a spreadsheet nobody can act on."""
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["listing_url"] == ROW["listing_url"]


def test_09_export_carries_outreach_state(client) -> None:
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["outreach_status"] == "contacted"
    assert row["note"] == "asked for pickup Sat"


def test_10_a_title_with_quotes_and_commas_survives(client) -> None:
    """Marketplace titles are seller-written. One stray quote must not shift
    every later column by one."""
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["title"] == ROW["title"]
    assert row["listing_url"].startswith("https://")


def test_11_export_defaults_to_the_lead_ordering(client) -> None:
    c, fake = client
    c.get("/api/export.csv")
    assert fake.seen["order"] == "lead"


def test_12_export_passes_its_filters_through(client) -> None:
    """The file must be what you were looking at, not a different question."""
    c, fake = client
    c.get("/api/export.csv?search=iphone&outreach=new")
    assert fake.seen["search"] == "iphone"
    assert fake.seen["outreach"] == "new"


def test_13_export_has_a_header_row(client) -> None:
    c, _ = client
    first = c.get("/api/export.csv").text.splitlines()[0]
    assert first.startswith("lead_score,band,signals,title")


def test_14_a_lead_with_no_signals_still_exports(monkeypatch) -> None:
    """A listing with nothing stated is still a lead -- the business contacts
    that seller regardless."""
    bare = {**ROW, "lead": {"score": 0, "band": "cold", "signals": []}}

    async def one_bare(limit, since_run, **kw):
        return {"listings": [bare], "count": 1}

    monkeypatch.setattr(api, "_listings", one_bare)
    text = TestClient(api.app).get("/api/export.csv").text
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["lead_score"] == "0" and row["signals"] == ""


# --------------------------------------------------------------------------
# the listings query the lead tab depends on
# --------------------------------------------------------------------------

def test_15_the_lead_order_is_an_explicit_request(client) -> None:
    """The catalog must stay newest-first; only the Leads tab asks to be
    re-sorted."""
    c, fake = client
    c.get("/api/listings")
    assert fake.seen["order"] == "recent"
    c.get("/api/listings?order=lead")
    assert fake.seen["order"] == "lead"


def test_16_filters_reach_the_query(client) -> None:
    c, fake = client
    c.get("/api/listings?search=sofa&outreach=new&offset=20")
    assert fake.seen["search"] == "sofa"
    assert fake.seen["outreach"] == "new"
    assert fake.seen["offset"] == 20


def test_17_a_database_failure_is_an_empty_list_not_a_500(monkeypatch) -> None:
    """The console must still render when Postgres is down -- a blank page
    tells the operator nothing about which part broke."""
    async def boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(api, "_listings", boom)
    r = TestClient(api.app).get("/api/listings")
    assert r.status_code == 200
    body = r.json()
    assert body["listings"] == [] and body["db_available"] is False


def test_21_a_failed_read_says_WHY_rather_than_looking_empty(monkeypatch) -> None:
    """The failure mode that cost two debugging rounds.

    A crashed query and an empty catalog both rendered as "No listings yet",
    so a MissingGreenlet on a lazy relationship looked exactly like a scraper
    that had stored nothing -- while 237 listings sat in the database.
    """
    async def boom(*a, **kw):
        raise RuntimeError("greenlet_spawn has not been called")

    monkeypatch.setattr(api, "_listings", boom)
    body = TestClient(api.app).get("/api/listings").json()
    assert "error" in body, "a failed read must report why"
    assert "greenlet_spawn" in body["error"]
    assert "RuntimeError" in body["error"]


def test_22_a_genuinely_empty_catalog_reports_no_error(monkeypatch) -> None:
    """The other half: empty must not look like a failure either."""
    async def empty(*a, **kw):
        return {"listings": [], "count": 0}

    monkeypatch.setattr(api, "_listings", empty)
    body = TestClient(api.app).get("/api/listings").json()
    assert body.get("error") is None
    assert body.get("db_available") is not False


# --------------------------------------------------------------------------
# the model, against the migration
# --------------------------------------------------------------------------

def test_18_outreach_columns_are_on_listing_not_seller() -> None:
    """These landed on Seller once and nothing caught it.

    `Seller` and `Listing` both declare a `first_seen_at` line, so an edit
    anchored on that text hit the wrong class. Every test passed -- they all
    exercised the scorer and the CSV, neither of which touches the ORM -- and
    the console returned an empty list for every tab, because the swallowed
    error was `'Listing' object has no attribute 'outreach_status'`.
    """
    from vingo_scraper.db.models import Listing, Seller

    for col in ("outreach_status", "contacted_at", "outreach_note"):
        assert col in Listing.__table__.columns, f"listing is missing {col}"
        assert col not in Seller.__table__.columns, f"seller should not have {col}"


def test_19_the_model_matches_what_migration_0009_creates() -> None:
    """The DDL and the mapped class must agree on the table, or queries fail
    at runtime against a database that is perfectly correct."""
    import pathlib
    import re

    src = pathlib.Path("alembic/versions/0009_outreach.py").read_text(encoding="utf-8")
    added = set(re.findall(r'add_column\(\s*"(\w+)",\s*sa\.Column\(\s*"(\w+)"', src))
    assert added, "could not read the migration's add_column calls"

    from vingo_scraper.db.models import Listing
    for table, column in added:
        assert table == "listing", f"0009 touches {table}, model expects listing"
        assert column in Listing.__table__.columns, f"model missing {column}"


def test_20_the_listings_payload_reads_only_columns_that_exist() -> None:
    """The endpoint reads these off the row; a typo here is a silent empty
    catalog, because /api/listings swallows its own errors by design."""
    from vingo_scraper.db.models import Listing

    for attr in ("category", "quality_score", "description_facts",
                 "outreach_status", "contacted_at", "outreach_note",
                 "price_history"):
        assert hasattr(Listing, attr), f"Listing has no {attr}"
