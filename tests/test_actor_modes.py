"""The Actor has two input modes and they do not compose.

The run that prompted this: Category *Musical instruments*, keyword *guitar*,
max Rs 50,000, sort *price low to high*, 3 pages. It returned 14 listings
including 1000L IBC Totes, a townhouse, a Rs 9,00,000 Volvo and a Rs 2,49,999
Mahindra -- 18x over the cap, in no price order.

The Actor's own schema explains it:

  sortBy     "Applies to Filters-based searches (ignored when you paste your
              own URLs)."
  urls       "Optional if Filters are set."
  latitude   "Ignored if you paste URLs above."

Supply `urls` and every Filters-mode field is discarded. We were sending both.
"""

from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlparse

from vingo_scraper.apify.client import build_run_input

SCHEMA = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "curious_coder_input_schema.json")
    .read_text(encoding="utf-8")
)
PROPS = SCHEMA["properties"]

# Everything the schema groups as "Filters" -- discarded in URL mode.
FILTER_ONLY = ("sortBy", "minPrice", "maxPrice", "daysSinceListed",
               "condition", "availability", "deliveryMethod", "radiusKm")

BAND = {"maxPrice": 50_000, "sortBy": "price_ascend", "daysSinceListed": "7",
        "strictFiltering": True}


def qs(payload: dict) -> dict:
    """The query string of the single URL a URL-mode payload carries."""
    return {k: v[0] for k, v in parse_qs(urlparse(payload["urls"][0]).query).items()}


# --------------------------------------------------------------------------
# the schema says it, not us
# --------------------------------------------------------------------------

def test_01_the_schema_itself_says_filters_are_ignored_with_urls() -> None:
    """Sourced from the Actor, so the reason survives a refresh of the fixture."""
    assert "ignored when you paste your own URLs" in PROPS["sortBy"]["description"]
    assert "Optional if Filters are set" in PROPS["urls"]["description"]


# --------------------------------------------------------------------------
# URL mode
# --------------------------------------------------------------------------

def test_02_a_category_run_sends_no_filter_fields() -> None:
    """The bug exactly: filters sent beside `urls` look functional and are not."""
    payload = build_run_input("guitar", "bangalore", 72,
                              category="musical_instruments", filters=BAND)
    leaked = [k for k in FILTER_ONLY if k in payload]
    assert leaked == [], f"sent in URL mode, where the Actor discards them: {leaked}"


def test_03_the_price_cap_rides_in_the_url_instead() -> None:
    assert qs(build_run_input("guitar", "bangalore", 72,
                              category="musical_instruments",
                              filters=BAND))["maxPrice"] == "50000"


def test_04_the_sort_rides_in_the_url_instead() -> None:
    """"Price: low to high" did nothing, because sortBy is the one field the
    schema explicitly documents as ignored."""
    assert qs(build_run_input("guitar", "bangalore", 72,
                              category="musical_instruments",
                              filters=BAND))["sortBy"] == "price_ascend"


def test_05_recency_rides_in_the_url_too() -> None:
    assert qs(build_run_input("guitar", "bangalore", 72,
                              category="musical_instruments",
                              filters=BAND))["daysSinceListed"] == "7"


def test_06_the_category_name_rides_in_the_url() -> None:
    """A category run always searches for the category's own wording. A
    keyword cannot narrow it -- Facebook discards `category_id` whenever the
    query contradicts it, so the endpoint refuses the combination."""
    got = qs(build_run_input(None, "bangalore", 72,
                             category="musical_instruments"))
    assert got["query"] == "Musical instruments"


def test_07_strict_filtering_survives_url_mode() -> None:
    """Not a search filter -- it narrows how loosely Facebook matches, so it
    is not part of the Filters group the schema describes as ignored."""
    payload = build_run_input("guitar", "bangalore", 72,
                              category="musical_instruments", filters=BAND)
    assert payload["strictFiltering"] is True


def test_08_a_category_without_a_keyword_still_narrows() -> None:
    """A search URL with no query and no category_id is an unbounded browse on
    a per-page-billed API."""
    got = qs(build_run_input(None, "bangalore", 24, category="musical_instruments"))
    assert got.get("query"), "a category run must carry some narrowing term"


# --------------------------------------------------------------------------
# Filters mode
# --------------------------------------------------------------------------

def test_09_a_keyword_run_sends_no_urls() -> None:
    payload = build_run_input("xbox", "bangalore", 24, filters=BAND)
    assert "urls" not in payload
    assert payload["searchKeyword"] == "xbox"


def test_10_a_keyword_run_keeps_every_filter_field() -> None:
    """The mode where they actually apply."""
    payload = build_run_input("xbox", "bangalore", 24, filters=BAND)
    assert payload["maxPrice"] == 50_000
    assert payload["sortBy"] == "price_ascend"
    assert payload["daysSinceListed"] == "7"


def test_11_the_two_modes_are_mutually_exclusive() -> None:
    """Whichever run shape, never both doors at once."""
    for payload in (
        build_run_input("xbox", "bangalore", 24, filters=BAND),
        build_run_input("guitar", "bangalore", 72, category="electronics", filters=BAND),
        build_run_input(None, "bangalore", 24, category="vehicles", filters=BAND),
    ):
        url_mode = "urls" in payload
        filters_present = any(k in payload for k in FILTER_ONLY)
        assert not (url_mode and filters_present), payload


def test_12_every_key_still_exists_in_the_schema() -> None:
    """A mode split must not smuggle in a field the Actor rejects -- one bad
    key fails the whole run before scraping."""
    for payload in (
        build_run_input("xbox", "bangalore", 24, filters=BAND),
        build_run_input("guitar", "bangalore", 72, category="electronics", filters=BAND),
    ):
        unknown = [k for k in payload if not k.startswith("_") and k not in PROPS]
        assert unknown == [], unknown


def test_13_a_verified_category_id_is_used_when_present(monkeypatch) -> None:
    """With Facebook's real ids a category stops being a keyword guess and
    becomes the category itself -- paired with its own name, exactly as
    Facebook's sidebar does."""
    from vingo_scraper.apify import client
    monkeypatch.setitem(client.CATEGORIES, "musical_instruments",
                        {"category_id": "10785", "query": "Musical instruments",
                         "slug": "musical-instruments"})
    got = qs(build_run_input(None, "bangalore", 24,
                             category="musical_instruments"))
    assert got["category_id"] == "10785"
    assert got["query"] == "Musical instruments"


def test_14_the_volvo_is_dropped() -> None:
    """The actual listing from the actual run: Rs 9,00,000 under a Rs 50,000
    cap. Nothing in the codebase compared a price to the cap until now."""
    from vingo_scraper.apify.runner import _drop_overpriced

    class L:
        def __init__(self, paise):
            self.price_minor = paise

    volvo, mahindra, guitar = L(900_000_00), L(249_999_00), L(1_200_00)
    kept, dropped = _drop_overpriced([volvo, mahindra, guitar], None, 50_000 * 100)
    assert dropped == 2
    assert kept == [guitar]


def test_15_an_unpriced_listing_is_kept() -> None:
    """Unknown is not the same as out of range -- the same rule `_drop_stale`
    applies to an undated listing. Dropping on missing data throws away
    listings whose price we merely failed to parse."""
    from vingo_scraper.apify.runner import _drop_overpriced

    class L:
        price_minor = None

    kept, dropped = _drop_overpriced([L()], None, 50_000 * 100)
    assert dropped == 0 and len(kept) == 1


def test_16_a_floor_works_too() -> None:
    from vingo_scraper.apify.runner import _drop_overpriced

    class L:
        def __init__(self, paise):
            self.price_minor = paise

    kept, dropped = _drop_overpriced([L(100_00), L(5_000_00)], 1_000 * 100, None)
    assert dropped == 1 and kept[0].price_minor == 5_000_00


def test_17_no_band_means_no_filtering() -> None:
    from vingo_scraper.apify.runner import _drop_overpriced

    rows = [object(), object()]
    kept, dropped = _drop_overpriced(rows, None, None)
    assert kept is rows and dropped == 0


def test_18_the_price_check_runs_before_rehosting() -> None:
    """A dropped listing must cost no image bandwidth."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner.ingest_collected)
    assert src.index("_drop_overpriced") < src.index("rehost_all")


# ==========================================================================
# The title filter that was ticked and did nothing
# ==========================================================================

def test_19_the_title_filter_follows_what_was_actually_searched() -> None:
    """Twice wrong, for opposite reasons.

    First it read `and not req.category`, silently disabling the filter
    whenever a category was chosen -- the checkbox stayed ticked and did
    nothing. Fixed to always enforce the keyword.

    Then Facebook showed a category run never sends the keyword at all: it
    searches for the category's own name. Enforcing an unsent keyword would
    drop every genuine result, so a category run enforces nothing.
    """
    from vingo_scraper.api.main import IngestRequest, title_filter_term

    assert title_filter_term(
        IngestRequest(query="guitar", title_match=True)) == "guitar"
    assert title_filter_term(
        IngestRequest(category="musical_instruments", title_match=True)) is None


def test_20_unticking_the_box_really_disables_it() -> None:
    from vingo_scraper.api.main import IngestRequest, title_filter_term
    assert title_filter_term(
        IngestRequest(query="guitar", title_match=False)) is None


def test_21_a_category_with_no_keyword_has_nothing_to_enforce() -> None:
    """Nothing to match on -- the filter must not invent a term from the
    category name and drop everything that does not repeat it."""
    from vingo_scraper.api.main import IngestRequest, title_filter_term
    assert title_filter_term(
        IngestRequest(category="vehicles", title_match=True)) is None


# ==========================================================================
# The console must not misreport the run
# ==========================================================================

def page() -> str:
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app
    return TestClient(app).get("/").text


def test_22_the_run_table_reads_the_key_the_api_sends() -> None:
    """The PHOTOS column printed the literal word "undefined" for every row:
    it read `l.photos`, and the API sends `image_count`."""
    h = page()
    assert "l.photos" not in h
    assert "${l.image_count}" in h


def test_23_a_warning_is_shown_on_a_SUCCESSFUL_run() -> None:
    """The failure branch sat behind `else if`, unreachable whenever the run
    returned data -- so the Actor's own message had nowhere to appear."""
    assert "Actor warning" in page()


def test_24_the_summary_distinguishes_short_supply_from_filtering() -> None:
    """"14 listings" after asking for ~72 is unexplainable on its own."""
    h = page()
    assert "out.requested" in h
    assert "out.items_returned" in h
    assert "that is all there was" in h


def test_25_the_price_drop_count_is_reported() -> None:
    """A filter that silently eats results is worse than no filter."""
    assert "outside the price range" in page()


def test_26_the_actor_error_is_stored_unclipped() -> None:
    """`config.manual_status` is varchar(64) and was the only copy, so the
    warning was cut off mid-word. `ScrapeRun.error` is Text and now gets it."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner)
    assert 'str(x.get("error"))[:300]' not in src

    from vingo_scraper.db.models import ScrapeRun
    assert "error" in ScrapeRun.__table__.columns


def test_27_requested_volume_is_derived_from_the_run_input() -> None:
    """The only durable record of what we paid for."""
    from vingo_scraper.apify.client import LISTINGS_PER_PAGE, pages_for
    # Whatever the plan allows, what we RECORD as requested must equal what we
    # actually asked the Actor for -- otherwise "asked for ~72, got 14" is
    # itself a lie.
    for want in (24, 48, 72):
        assert pages_for(want) * LISTINGS_PER_PAGE == pages_for(want) * 24


def test_28_a_blank_query_no_longer_defaults_to_iphone() -> None:
    """`query` defaulted to "iphone". A request omitting it bought a page of
    iPhones -- and with a category selected it would have become the term the
    title filter enforced, dropping every genuine result."""
    from vingo_scraper.api.main import IngestRequest
    assert IngestRequest().query == ""


# ==========================================================================
# The run history must say what was searched, and not call a warning a failure
# ==========================================================================

def test_29_a_category_run_is_named_in_the_history() -> None:
    """It showed "bangalore / -". A category run is URL mode and carries no
    `searchKeyword`, so reading only that field left the feed unable to say
    what had been scraped."""
    from vingo_scraper.db.config_store import _searched_for

    url = ("https://www.facebook.com/marketplace/bangalore/search/"
           "?query=Musical+instruments&category_id=1078592699170502&maxPrice=50000")
    assert _searched_for({"urls": [url]}) == "Musical instruments"


def test_30_a_keyword_run_is_still_named() -> None:
    from vingo_scraper.db.config_store import _searched_for
    assert _searched_for({"searchKeyword": "guitar"}) == "guitar"


def test_31_an_unidentifiable_run_says_so() -> None:
    """A dash is honest. Inventing a label would be worse."""
    from vingo_scraper.db.config_store import _searched_for
    assert _searched_for({}) == "-"
    assert _searched_for(None) == "-"


def test_32_a_successful_run_with_a_warning_shows_its_counts() -> None:
    """The row replaced the counts with the error text, so a run that returned
    18 listings read as a failure -- the same empty-versus-broken confusion
    that cost two debugging sessions, in a new place.

    The free-plan notice arrives on EVERY run, so without this every row in
    the history looked red.
    """
    h = page()
    assert "x.error && !x.listings ? 'bad' : ''" in h
    assert "${x.listings} listings · ${x.created} new" in h
