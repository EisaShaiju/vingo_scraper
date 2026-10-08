"""The seven requirements, ten tests each.

One section per requirement from the brief, so a failure names the promise it
breaks rather than the function it broke in. All offline -- Apify is faked, so
the whole file costs nothing to run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vingo_scraper.apify.client import (
    CURIOUS_CODER,
    DAYS_SINCE_LISTED_CHOICES,
    SORT_NEWEST,
    EmptySearchError,
    build_run_input,
    merge_filters,
)
from vingo_scraper.config import CATEGORIES, settings
from vingo_scraper.extract.schema import ExtractionMethod, Listing, ListingImage
from vingo_scraper.pipeline.quality import MIN_PHOTOS, assess, assess_all

STATIC = "src/vingo_scraper/api/static/index.html"


def L(*, photos=3, desc="a perfectly ordinary description of the item",
      price=4_500_000, fid="1", posted=None):
    return Listing(
        fb_listing_id=fid, listing_url=f"https://fb.com/marketplace/item/{fid}",
        title="iPhone 13 128GB", description=desc, price_minor=price,
        images=[ListingImage(source_url=f"https://x/{fid}-{i}.jpg")
                for i in range(photos)],
        posted_at=posted, extraction_method=ExtractionMethod.APIFY,
    )


def page() -> str:
    import pathlib
    return pathlib.Path(STATIC).read_text(encoding="utf-8")


# ==========================================================================
# 1. Deployment: served fast, served safely
# ==========================================================================

def test_1_01_caddyfile_exists():
    import pathlib
    assert pathlib.Path("deploy/Caddyfile").exists()


def test_1_02_caddy_proxies_to_localhost_only():
    """Caddy is the only public thing; uvicorn must stay on loopback."""
    import pathlib
    cfg = pathlib.Path("deploy/Caddyfile").read_text(encoding="utf-8")
    assert "127.0.0.1:8000" in cfg


def test_1_03_caddy_requires_a_password():
    """/api/automation/start spends money -- it is never open to the internet."""
    import pathlib
    cfg = pathlib.Path("deploy/Caddyfile").read_text(encoding="utf-8")
    assert "basic_auth" in cfg or "basicauth" in cfg


def test_1_04_service_binds_loopback_not_all_interfaces():
    import pathlib
    unit = pathlib.Path("deploy/comet.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in unit
    assert "0.0.0.0" not in unit


def test_1_05_database_is_local_so_latency_is_not_network():
    """The Supabase era paid ~250ms per query to Seoul; local is sub-ms."""
    from vingo_scraper.db.session import is_pooled
    assert not is_pooled("postgresql+psycopg://comet:p@127.0.0.1:5432/comet")


def test_1_06_local_engine_pools_connections():
    from sqlalchemy.pool import NullPool

    from vingo_scraper.db.session import engine_options
    _, kwargs = engine_options("postgresql+psycopg://c:p@127.0.0.1:5432/comet")
    assert kwargs.get("poolclass") is not NullPool
    assert kwargs["pool_size"] >= 1


def test_1_07_full_page_setting_exists():
    """Volume is bought by the page; taking 8 of 24 wastes what we paid for."""
    assert hasattr(settings, "apify_accept_full_page")


def test_1_08_run_input_requests_one_page_for_a_small_ask():
    payload = build_run_input("iphone", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert payload["maxPagesPerUrl"] == 1


def test_1_09_two_phase_runner_is_still_two_functions():
    """Collapsing it back into one blocking call reintroduces the timeout."""
    from vingo_scraper.apify import client
    assert callable(client.start_run)
    assert callable(client.collect_run)


def test_1_10_private_keys_never_reach_apify():
    """One unrecognised input field fails the run -- after it has charged."""
    import inspect

    from vingo_scraper.apify import client
    src = inspect.getsource(client.start_run)
    assert 'startswith("_")' in src


# ==========================================================================
# 2. One page, no tabs
# ==========================================================================

def test_2_01_one_tab_per_question():
    """Search, Leads and Catalog answer three different questions.

    The earlier Manual/Automated split was wrong for the opposite reason: those
    were the SAME question split in half. A tab earns its place by answering
    something the others cannot -- "what did I just get", "who do I message
    first", "what do we hold".
    """
    h = page()
    assert h.count('class="tab ') + h.count('class="tab"') == 3
    for panel in ("p-manual", "p-leads", "p-catalog"):
        assert f'data-panel="{panel}"' in h
    # Automation is gone, not hidden behind another tab.
    assert 'data-panel="p-auto"' not in h


def test_2_02_the_search_tab_opens_first():
    """You arrive to do something, not to browse what you already have."""
    h = page()
    assert '<button class="tab on" data-panel="p-manual">' in h
    assert '<section class="panel on" id="p-manual">' in h


def test_2_03_both_sections_present_at_once():
    """Ingest and catalog, visible together. The automation section is gone."""
    h = page()
    for sid in ("p-manual", "p-catalog"):
        assert f'id="{sid}"' in h
    assert 'id="p-auto"' not in h


def test_2_04_sections_are_not_hidden_behind_a_class():
    h = page()
    assert '<div class="panel"' not in h


def test_2_05_one_ingest_button():
    assert page().count('id="m-run"') == 1


def test_2_06_the_old_hero_form_is_gone():
    """It duplicated the manual form and described storage we no longer use."""
    h = page()
    assert 'id="run"' not in h
    assert "Supabase" not in h


def test_2_07_catalog_renders_without_a_click():
    assert "loadListings" in page()


def test_2_08_no_automation_markup_remains():
    """Automation was removed, not hidden. A hidden panel gets switched on."""
    h = page()
    for dead in ('id="a-rot"', 'id="a-start"', 'id="a-stop"', "loadAutomation"):
        assert dead not in h, dead


def test_2_09_the_ingest_form_is_the_whole_control_surface():
    h = page()
    # One search field, not a category select beside a keyword box: Facebook
    # cannot honour both, so the console no longer lets them be expressed.
    assert 'id="m-run"' in h and 'id="m-search"' in h and 'id="m-cats"' in h


def test_2_10_page_is_served_at_root(tmp_path):
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


# ==========================================================================
# 3. Manual and automated never run together
# ==========================================================================

def test_3_01_lock_helper_exists():
    from vingo_scraper.db import config_store
    assert hasattr(config_store, "ingestion_lock")


def test_3_02_lock_is_advisory_not_a_boolean_column():
    """An advisory lock dies with its session, so an orphan is impossible."""
    import inspect

    from vingo_scraper.db import config_store
    src = inspect.getsource(config_store.ingestion_lock)
    assert "pg_try_advisory_lock" in src


def test_3_03_lock_try_not_wait():
    """Trying and failing is what lets a manual click return 409 at once."""
    import inspect

    from vingo_scraper.db import config_store
    src = inspect.getsource(config_store.ingestion_lock)
    assert "pg_advisory_lock(" not in src.replace("pg_try_advisory_lock(", "")


def test_3_04_lock_released_in_a_finally():
    import inspect

    from vingo_scraper.db import config_store
    src = inspect.getsource(config_store.ingestion_lock)
    assert "finally:" in src and "pg_advisory_unlock" in src


def test_3_05_busy_error_carries_the_holder():
    from vingo_scraper.db.config_store import IngestionBusy
    assert IngestionBusy("automation").holder == "automation"


def test_3_06_stale_lock_expires():
    """A wedged process must not block ingestion forever."""
    from vingo_scraper.db import config_store
    assert config_store.LOCK_STALE_S > 0


def test_3_07_manual_start_is_wrapped_in_the_lock():
    import inspect

    from vingo_scraper.api import main
    assert 'ingestion_lock("manual")' in inspect.getsource(main.start_ingest)


def test_3_08_no_second_runner_can_take_the_lock():
    """Automation is gone; manual is the only holder."""
    from vingo_scraper.api import main
    assert not hasattr(main, "_run_tick")


def test_3_09_ui_shows_a_busy_banner():
    assert "Ingestion busy" in page() or "already in progress" in page()


def test_3_10_busy_is_409_not_500():
    import inspect

    from vingo_scraper.api import main
    assert "409" in inspect.getsource(main.start_ingest)


# ==========================================================================
# 4. Quality: >= 2 photos required, description optional
# ==========================================================================

def test_4_01_two_photos_is_the_floor():
    assert MIN_PHOTOS == 2


def test_4_02_one_photo_is_rejected():
    assert assess(L(photos=1)).hard_reject is True


def test_4_03_zero_photos_is_rejected():
    assert assess(L(photos=0)).hard_reject is True


def test_4_04_two_photos_passes():
    rep = assess(L(photos=2))
    assert rep.hard_reject is False and rep.passed is True


def test_4_05_no_description_is_kept():
    """The business answer to 'no description' is to contact the seller."""
    rep = assess(L(desc=""))
    assert rep.hard_reject is False


def test_4_06_short_description_is_kept_but_scores_lower():
    rich = assess(L(desc="a" * 80 + " genuinely descriptive text here"))
    thin = assess(L(desc="x"))
    assert thin.hard_reject is False
    assert thin.score < rich.score


def test_4_07_placeholder_price_is_rejected():
    assert assess(L(price=100)).hard_reject is True


def test_4_08_rejected_listings_never_reach_the_caller():
    kept, summary = assess_all([L(fid="a"), L(fid="b", photos=1)])
    assert [x.fb_listing_id for x in kept] == ["a"]
    assert summary["rejected"] == 1


def test_4_09_reject_reasons_are_tallied():
    """Discarding makes the rate invisible unless the count survives."""
    _, summary = assess_all([L(fid=str(i), photos=1) for i in range(3)])
    assert sum(summary["rejected_reasons"].values()) == 3


def test_4_10_gate_runs_before_rehosting():
    """A rejected listing must cost no bandwidth and no storage."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner.ingest_collected)
    assert src.index("assess_all") < src.index("rehost_all")


# ==========================================================================
# 5. Never re-scrape what we already hold
# ==========================================================================

def test_5_01_actor_cache_is_off_by_default():
    """With no scheduled runner, the cache only hides results.

    It told the Actor "skip anything you already gave us", so a person typing
    xbox got 0 listings for $0.0000 and a console that looked broken.
    """
    assert settings.apify_cache_store_id == ""
    payload = build_run_input("iphone", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert "onlyNewListings" not in payload
    assert "cacheStorageId" not in payload


def test_5_02_cache_can_be_re_enabled(monkeypatch):
    """Kept working for when a scheduled sweep returns."""
    monkeypatch.setattr("vingo_scraper.config.settings.apify_cache_store_id", "s1")
    payload = build_run_input("iphone", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert payload["onlyNewListings"] is True
    assert payload["cacheStorageId"] == "s1"


def test_5_03_database_side_dedup_exists():
    from vingo_scraper.apify import runner
    assert callable(runner._split_known)


def test_5_04_dedup_runs_before_rehosting():
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner.ingest_collected)
    assert src.index("_split_known") < src.index("rehost_all")


def test_5_05_known_listings_only_touch_last_seen():
    """Re-observing must not reset a human's review decision."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner._touch_last_seen)
    # The UPDATE sets exactly one column. Checking the docstring would pass
    # on a comment; this checks the statement.
    body = src[src.index(".values("):]
    assert "last_seen_at" in body
    assert "review_status" not in body
    assert "first_seen_at" not in body


def test_5_06_dedup_failure_keeps_listings_rather_than_dropping_them():
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner._split_known)
    assert "except Exception" in src and "return list(listings), []" in src


def test_5_07_an_all_cached_run_is_flagged_not_called_empty():
    from vingo_scraper.apify.runner import IngestResult
    assert IngestResult().all_cached is False


def test_5_08_dedup_is_one_query_not_n():
    import inspect

    from vingo_scraper.apify import runner
    assert ".in_(" in inspect.getsource(runner._split_known)


def test_5_09_cache_id_is_configurable():
    assert isinstance(settings.apify_cache_store_id, str)


def test_5_10_db_dedup_is_documented_as_not_saving_money():
    """Apify charges when it scrapes, before any id reaches us."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner.ingest_collected)
    assert "does NOT save Apify spend" in src or "not save" in src.lower()


# ==========================================================================
# 6. Freshness
# ==========================================================================

def test_6_01_newest_first_is_the_default():
    assert merge_filters()["sortBy"] == SORT_NEWEST


def test_6_02_a_recency_window_is_always_set():
    assert merge_filters()["daysSinceListed"] in DAYS_SINCE_LISTED_CHOICES


def test_6_03_a_tighter_window_is_available_to_the_operator():
    """Manual chooses its own window; there is no scheduled runner to pin one."""
    assert set(DAYS_SINCE_LISTED_CHOICES) >= {"1", "7", "30"}


def test_6_04_the_actor_has_no_hourly_option():
    """Finer windows cannot be requested from Facebook -- only applied after."""
    assert "1" in DAYS_SINCE_LISTED_CHOICES
    assert not any(c.endswith("h") for c in DAYS_SINCE_LISTED_CHOICES)


def test_6_05_manual_can_override_the_window():
    assert merge_filters({"daysSinceListed": "30"})["daysSinceListed"] == "30"


def test_6_06_window_can_be_cleared():
    assert merge_filters({"daysSinceListed": ""})["daysSinceListed"] == ""


def test_6_07_post_filter_drops_old_listings():
    from vingo_scraper.apify.runner import _drop_stale
    old = L(fid="old", posted=datetime.now(UTC) - timedelta(hours=48))
    new = L(fid="new", posted=datetime.now(UTC) - timedelta(hours=1))
    fresh, dropped = _drop_stale([old, new], 6)
    assert [x.fb_listing_id for x in fresh] == ["new"]
    assert dropped == 1


def test_6_08_unknown_age_is_kept_not_dropped():
    """Unknown is not old. Dropping on missing data loses real listings."""
    from vingo_scraper.apify.runner import _drop_stale
    fresh, dropped = _drop_stale([L(fid="x", posted=None)], 6)
    assert len(fresh) == 1 and dropped == 0


def test_6_09_no_window_means_no_post_filtering():
    from vingo_scraper.apify.runner import _drop_stale
    items = [L(fid=str(i), posted=datetime(2020, 1, 1, tzinfo=UTC)) for i in range(3)]
    fresh, dropped = _drop_stale(items, 99_999)
    assert len(fresh) == 3 and dropped == 0


def test_6_10_stale_count_is_reported_separately_from_rejects():
    """Too old and too thin are different failures."""
    from vingo_scraper.apify.runner import IngestResult
    r = IngestResult()
    assert r.stale_dropped == 0 and r.rejected == 0


# ==========================================================================
# 7. Category filtering
# ==========================================================================

def test_7_01_the_real_marketplace_categories_are_present():
    for name in ("vehicles", "electronics", "property_rent", "musical_instruments"):
        assert name in CATEGORIES


def test_7_02_every_category_has_a_slug_and_a_query():
    for name, spec in CATEGORIES.items():
        assert spec["slug"] and spec["query"], name


def test_7_03_a_category_becomes_a_search_url_carrying_its_filters():
    """A category used to be a bare browse path with no query string at all,
    so every filter rode in Actor fields that URL mode discards. A run capped
    at Rs 50,000 returned a Rs 9,00,000 Volvo."""
    payload = build_run_input(None, "bangalore", 8, category="electronics",
                              actor_id=CURIOUS_CODER,
                              filters={"maxPrice": 50000})
    url = payload["urls"][0]
    assert isinstance(url, str)          # stringList, not [{"url": ...}]
    assert "maxPrice=50000" in url
    assert "maxPrice" not in payload     # not ALSO as a field -- one mode only


def test_7_04_a_category_alone_still_narrows_the_search():
    """A search URL with no term and no category id is an unbounded browse on
    a per-page-billed API."""
    payload = build_run_input(None, "bangalore", 8, category="vehicles",
                              actor_id=CURIOUS_CODER)
    assert "searchKeyword" not in payload
    assert "query=" in payload["urls"][0]


def test_7_04b_a_category_and_a_keyword_are_mutually_exclusive():
    """Facebook cannot search inside a category -- `?query=iphone&category_id=
    <musical instruments>` returned iPhones on a real run. The console offers
    one or the other; the API refuses both rather than quietly altering the
    request."""
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    r = TestClient(app).post("/api/ingest",
                             json={"category": "electronics", "query": "laptop",
                                   "location": "bangalore"})
    assert r.status_code == 400


def test_7_05_a_keyword_sends_no_url():
    payload = build_run_input("xbox", "bangalore", 8, actor_id=CURIOUS_CODER)
    assert payload["searchKeyword"] == "xbox"
    assert "urls" not in payload


def test_7_06_empty_input_is_refused():
    with pytest.raises(EmptySearchError):
        build_run_input(None, "bangalore", 8)


def test_7_07_the_refusal_names_the_fix():
    with pytest.raises(EmptySearchError, match="category or a keyword"):
        build_run_input("", "bangalore", 8, category="")


def test_7_08_unknown_category_falls_back_to_its_own_name():
    payload = build_run_input(None, "bangalore", 8, category="made-up",
                              actor_id=CURIOUS_CODER)
    # An unverified category falls back to its own words as the search term,
    # rather than a path Facebook may not have -- a wrong category path does
    # not 404, it serves the generic feed, which is indistinguishable from a
    # working search until you read the results.
    assert "query=made-up" in payload["urls"][0]


def test_7_09_every_category_is_selectable_by_hand():
    """With no sweep, the operator picks -- so every category Facebook HAS
    must be reachable.

    Was `>= 18`. It is 17 now, and the missing one is the point: `mobile_phones`
    does not exist in Facebook's category menu -- phones live under Electronics
    -- so offering it returned the generic Marketplace feed. A count is the
    wrong assertion when the right number is "however many Facebook has";
    `tests/test_categories.py` pins the identities instead.
    """
    assert len(CATEGORIES) == 17
    assert "mobile_phones" not in CATEGORIES


def test_7_10_every_category_is_offered_in_the_ui():
    """Reachable from the single search box rather than a separate select --
    the two controls were merged because Facebook cannot honour both at once.
    `tests/test_categories.py` owns the label/key mapping."""
    h = page()
    block = h[h.index('<datalist id="m-cats">'):]
    block = block[:block.index("</datalist>")]
    for meta in CATEGORIES.values():
        assert f'<option value="{meta["query"]}">' in block, meta["query"]


async def test_8_01_an_actor_error_item_is_reported_not_counted_as_a_listing(
    monkeypatch,
):
    """The Actor reports failures as dataset ITEMS with an `error` key.

    Unhandled, the console showed "1 listings · 0 new" for what was an error
    message -- indistinguishable from a parser bug, and it sent us looking for
    one.
    """
    from vingo_scraper.apify import runner
    from vingo_scraper.apify.client import ActorRun

    async def no_persist(result, run, *, trigger="manual"):
        return 0, 0

    monkeypatch.setattr(runner, "_persist", no_persist)

    run = ActorRun(
        run_id="r1", actor_id="a", status="SUCCEEDED", run_input={},
        items=[{"error": "Search returned no results for this query"}],
        cost_usd=0.00005,
    )
    result = await runner.ingest_collected(run, location="bangalore")

    assert "no results" in (result.error or "")
    # The error item is not a listing we failed to parse.
    assert result.items_returned == 0
    assert result.listings == []


def test_8_02_a_long_actor_error_cannot_break_the_status_write():
    """Postgres does not truncate -- it raises and loses the whole UPDATE.

    Surfacing Actor errors put a 300-character message into a varchar(64)
    column, so every manual run ended in a red DataError even when the scrape
    itself had succeeded.
    """
    from vingo_scraper.db.config_store import STATUS_MAX, _fit

    long = "failed: " + ("Number of records scraped per run is limited. " * 10)
    assert len(_fit(long)) <= STATUS_MAX
    assert _fit(None) is None
    assert _fit("ok") == "ok"
    # Newlines in the message must not survive into a status field.
    assert "\n" not in _fit("failed:\n\n  limit reached\n")


# ==========================================================================
# Manual is the whole product now
# ==========================================================================

def test_9_01_automation_endpoints_are_gone():
    """Removed, not disabled. A dormant second runner still shares the lock."""
    from vingo_scraper.api.main import app
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    for dead in ("/api/automation", "/api/automation/start", "/api/cron/tick",
                 "/api/demo/status", "/api/demo/start"):
        assert dead not in paths, dead


def test_9_02_the_manual_endpoints_remain():
    from vingo_scraper.api.main import app
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert {"/api/ingest", "/api/ingest/collect", "/api/manual"} <= paths


def test_9_03_the_lock_still_guards_a_double_click():
    """Two runners became one, but one operator clicking twice still pays twice."""
    import inspect

    from vingo_scraper.api import main
    assert 'ingestion_lock("manual")' in inspect.getsource(main.start_ingest)


def test_9_04_a_run_in_flight_is_reported_as_scraping():
    assert "Scraping" in page()


def test_9_05_an_empty_result_says_so():
    """Silence after a run reads as a broken button."""
    assert "No listings matched" in page()


def test_9_06_rejections_are_explained_in_the_ui():
    assert "under 2 photos" in page()


def test_9_07_the_page_collects_without_being_asked():
    """A 45s wait the operator has to poll by hand is not a working button."""
    h = page()
    assert "collectManual" in h
    assert "setInterval" in h


def test_9_08_two_phase_is_kept_even_with_one_runner():
    """The Actor's 10.8-47.1s variance did not go away with automation."""
    import inspect

    from vingo_scraper.api import main
    assert "start_run" in inspect.getsource(main.start_ingest)
    assert "collect_run" in inspect.getsource(main.collect_ingest)


def test_9_09_manual_history_is_still_filtered_by_trigger():
    import inspect

    from vingo_scraper.api import main
    assert 'trigger="manual"' in inspect.getsource(main.manual_status)


def test_9_10_no_automation_imports_linger():
    import inspect

    from vingo_scraper.api import main
    src = inspect.getsource(main)
    assert "AUTOMATION_FILTERS" not in src
    assert "automation_cells" not in src


# ==========================================================================
# The UI changes this session
# ==========================================================================

def test_10_01_a_run_in_flight_is_unmissable():
    """A 13px spinner inside a text line reads as a dead button for 50s."""
    h = page()
    assert 'class="working"' in h and 'class="ring"' in h
    assert "Scraping Marketplace" in h


def test_10_02_progress_names_the_phase_and_counts_seconds():
    h = page()
    assert "Waiting for Facebook" in h
    assert "elapsed" in h


def test_10_03_the_search_tab_shows_only_this_runs_listings():
    h = page()
    assert "showRunResults" in h
    assert "since_run=" in h


def test_10_04_volume_is_offered_honestly():
    """"Items: 8" was a lie -- volume is bought by the page. So was
    "3 pages, ~72 listings": the free plan returns ~one page whatever is
    asked for (measured: 1 page -> 25 items, 3 pages -> 26)."""
    h = page()
    assert "~24 listings" in h
    assert 'type="number" value="8"' not in h
    assert "~72 listings" not in h


def test_10_04b_the_free_plan_limit_is_stated_on_screen():
    """An unexplained ceiling reads as a broken control."""
    h = page()
    assert "free plan" in h
    assert "16" in h and "28" in h       # the measured range, not a round number


def test_10_05_recency_defaults_to_any_time():
    """A 7-day window hid most of what Marketplace actually shows."""
    h = page()
    assert '<option value="" selected>Any time</option>' in h


def test_10_06_the_title_filter_is_on_by_default():
    h = page()
    assert 'id="m-strict" checked' in h


def test_10_07_off_topic_drops_are_reported():
    """A filter that silently eats results is worse than no filter."""
    assert "off-topic" in page()


def test_10_08_the_catalog_can_be_filtered():
    h = page()
    assert 'id="cat-filter"' in h and "catFilter" in h


def test_10_09_the_catalog_count_appears_on_its_tab():
    assert 'id="t-count"' in page()


def test_10_10_progress_stops_when_a_start_fails():
    """Otherwise the spinner runs forever on a 409 or a 400."""
    h = page()
    assert "if(!r.ok) showProgress(false);" in h
