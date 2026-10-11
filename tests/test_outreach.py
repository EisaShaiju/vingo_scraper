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
    "seller_name": "Prashanth Maruru",
    "fb_seller_id": "100000494167942",
    "seller_phone": "+91 98450 11223",
    "seller_email": None,
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


# ==========================================================================
# messaging the seller directly
# --------------------------------------------------------------------------
# For weeks this project stated seller identity was present on 6.4% of
# listings and that a Messenger link therefore could not be built. Measured
# 2026-10-11 over 96 live items with `include_details` on: 88/96 = 91% carry a
# numeric id. The old figure came from fixtures captured before that flag and
# was never re-measured, so the data was in the database and only the UI was
# behind.
# ==========================================================================

def test_23_a_numeric_seller_id_addresses_a_messenger_thread() -> None:
    """/messages/t/<id> opens the conversation with that PERSON. The listing
    URL only opens Marketplace's composer under one item."""
    url = api.messenger_url("100000494167942")
    assert url == "https://www.facebook.com/messages/t/100000494167942"


def test_24_a_missing_id_builds_no_link_rather_than_a_broken_one() -> None:
    assert api.messenger_url(None) is None
    assert api.messenger_url("") is None


def test_25_a_pfbid_token_is_refused() -> None:
    """A `pfbid` is a rotating profile token, not a user id -- /messages/t/
    does not resolve it. Returning None lets the caller fall back to the
    listing and SAY so; a button that silently lands elsewhere is a lie the
    operator pays for on every row.
    """
    assert api.messenger_url("pfbid02aBcDeFgHiJkLmNoPqRsTuVwXyZ") is None


# --------------------------------------------------------------------------
# the draft
# --------------------------------------------------------------------------

def test_26_the_draft_names_the_item_and_its_price() -> None:
    """A generic opener reads as a mass message and gets ignored."""
    draft = api.draft_message("Harley Davidson Street 750", 235000.0)
    assert "Harley Davidson Street 750" in draft
    assert "2,35,000" in draft or "235,000" in draft


def test_27_the_draft_asks_for_a_phone_number() -> None:
    """The one thing we cannot scrape -- Facebook publishes phone on 0% of
    listings, so the seller volunteering it is the only route to one.

    Asserts the ask, not one spelling of it. This test pinned the literal
    "phone number" and failed the moment WhatsApp was named alongside it --
    a wording change breaking a test that was never about wording.
    """
    draft = api.draft_message("Sofa", 8000.0).lower()
    assert "phone" in draft
    assert "reply" in draft


def test_28_the_draft_makes_the_actual_ask() -> None:
    assert "vingo" in api.draft_message("Sofa", 8000.0).lower()


def test_29_a_known_name_is_used_once_and_only_the_first() -> None:
    draft = api.draft_message("Sofa", 8000.0, "Prashanth Maruru")
    assert draft.startswith("Hi Prashanth!")
    assert "Maruru" not in draft


def test_30_a_missing_price_does_not_print_a_blank_or_a_zero() -> None:
    draft = api.draft_message("Sofa", None)
    assert "₹" not in draft and "listed at" not in draft
    assert "Sofa" in draft


# --------------------------------------------------------------------------
# capturing what came back -- manual, on purpose
# --------------------------------------------------------------------------

def test_31_a_capture_with_neither_phone_nor_email_is_refused() -> None:
    """Saving an empty form would set `replied` on a listing nobody replied
    to, which is exactly the kind of number this console must not invent."""
    c = TestClient(api.app)
    r = c.post("/api/listings/11111111-1111-1111-1111-111111111111/contact",
               json={"note": "they seemed keen"})
    assert r.status_code == 400
    assert "phone" in r.text and "email" in r.text


def test_32_the_contact_endpoint_takes_phone_email_and_note() -> None:
    fields = api.ContactRequest.model_fields
    assert set(fields) == {"phone", "email", "note"}


def test_33_contact_is_stored_on_the_seller_not_the_listing() -> None:
    """One person has several listings. Capture a number once and every other
    listing by them shows it, so nobody asks the same seller twice."""
    from vingo_scraper.db.models import Listing, Seller

    assert hasattr(Seller, "phone") and hasattr(Seller, "email")
    assert not hasattr(Listing, "phone")


def test_34_no_migration_is_needed_for_the_contact_columns() -> None:
    """`Seller.phone` and `Seller.email` were already in the schema and
    unused. If that stops being true this test says so before a deploy does.
    """
    from vingo_scraper.db.models import Seller
    assert "phone" in Seller.__table__.columns
    assert "email" in Seller.__table__.columns


# --------------------------------------------------------------------------
# making the captured contacts usable
# --------------------------------------------------------------------------

def test_35_export_carries_the_contact_details(client) -> None:
    """The point of capturing a number is that it can leave the console and
    reach whoever builds the Vingo listing."""
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["seller_phone"] == ROW["seller_phone"]
    assert row["seller_name"] == ROW["seller_name"]
    assert row["seller_email"] == ""


def test_36_export_carries_both_contact_routes(client) -> None:
    c, _ = client
    row = next(csv.DictReader(io.StringIO(c.get("/api/export.csv").text)))
    assert row["messenger_url"].endswith(ROW["fb_seller_id"])
    assert row["listing_url"] == ROW["listing_url"]


def test_37_a_seller_with_no_id_exports_an_empty_messenger_column(monkeypatch) -> None:
    """Blank, not a URL that goes nowhere."""
    async def fake(limit, since_run, **kw):
        return {"listings": [{**ROW, "fb_seller_id": None}], "count": 1}

    monkeypatch.setattr(api, "_listings", fake)
    text = TestClient(api.app).get("/api/export.csv").text
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["messenger_url"] == ""
    assert row["listing_url"].startswith("https://")


def test_38_the_has_contact_filter_reaches_the_query(client) -> None:
    """"Sellers who gave us a number" is the only segment that matters once
    outreach is running."""
    c, fake = client
    c.get("/api/listings?has_contact=true")
    assert fake.seen["has_contact"] is True


def test_39_has_contact_is_off_unless_asked_for(client) -> None:
    c, fake = client
    c.get("/api/listings")
    assert fake.seen["has_contact"] is False


def test_40_export_passes_has_contact_through(client) -> None:
    c, fake = client
    c.get("/api/export.csv?has_contact=true")
    assert fake.seen["has_contact"] is True


def test_41_the_listings_payload_carries_the_route_and_the_draft() -> None:
    """The UI must not build either itself -- it did, and drifted from what we
    actually hold."""
    import inspect
    src = inspect.getsource(api._listings)
    for key in ("fb_seller_id", "messenger_url", "draft_message",
                "seller_email"):
        assert f'"{key}"' in src, f"/api/listings does not expose {key}"


def test_42_the_console_never_claims_to_read_a_messenger_inbox() -> None:
    """Reply capture is manual and the UI has to say so. An outreach status
    that updated itself without anyone checking would be a number nobody
    could trust."""
    html = TestClient(api.app).get("/").text
    assert "cannot read your Messenger inbox" in html


# --------------------------------------------------------------------------
# The fallback route had the link but no message -- the regression that sent
# an operator to a listing page with nothing to paste.
# --------------------------------------------------------------------------

def test_43_the_opener_says_who_is_writing_before_anything_else() -> None:
    """The first version led with "Saw your <item>", which reads like a
    haggling buyer. A stranger needs to know who this is to answer at all."""
    draft = api.draft_message("Sofa", 8000.0, "Prashanth Maruru")
    intro, _, rest = draft.partition("\n\n")
    assert "team vingo" in intro.lower()
    assert "Sofa" not in intro and "Sofa" in rest


def test_44_the_opener_says_what_vingo_does() -> None:
    """"Would you like to sell on Vingo?" from an unknown party is a question
    nobody answers. The offer has to be in the message."""
    intro = api.draft_message("Sofa", 8000.0).split("\n\n")[0].lower()
    assert "buyers" in intro
    assert "free" in intro


def test_45_the_draft_survives_being_pasted_as_one_block() -> None:
    """Messenger and the Marketplace composer both SEND on Enter. Paragraphs
    separated by blank lines paste whole; a single newline would send a
    half-written message to a stranger."""
    draft = api.draft_message("Sofa", 8000.0, "Prashanth Maruru")
    assert draft.count("\n\n") == 2
    assert "\n" not in draft.replace("\n\n", "")
    assert not draft.endswith("\n")


def test_46_the_fallback_route_also_offers_the_draft() -> None:
    """THE BUG. `Copy message` lived only in the messenger_url arm, so the
    ~9% without a user id got the listing link and nothing to paste -- they
    landed on Facebook's own "Hi, is this available?" with our opener
    unreachable. Both arms must reach window.__draft."""
    html = TestClient(api.app).get("/").text
    assert "copyAndOpen" in html
    assert "Copy message + open listing" in html


def test_47_the_fallback_opens_the_tab_inside_the_click() -> None:
    """`await`ing the clipboard before window.open drops the user-gesture
    context and the popup blocker eats the tab -- the copy works, nothing
    opens, and it reads as a dead button. copyDraft must not be awaited."""
    html = TestClient(api.app).get("/").text
    body = html.split("function copyAndOpen")[1][:400]
    assert "await copyDraft" not in body
    assert "copyDraft(btn);" in body


def test_48_the_leads_tab_does_not_still_claim_the_old_route() -> None:
    """The help text said Message seller "opens the listing on Marketplace",
    written when that was the only route. It is now wrong for 91% of rows."""
    html = TestClient(api.app).get("/").text
    assert "opens the listing\n        on Marketplace" not in html
    assert "opens the Messenger thread" in html
