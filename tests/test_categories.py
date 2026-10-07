"""Facebook's categories, as Facebook actually defines them.

Every id here was read off a real Marketplace URL by hand on 2026-10-07,
walking all 18 sidebar entries in Bengaluru. That pass should never have to be
repeated, so the values are pinned as literals: edit one and this file fails.

The finding that made it necessary: **a category is a numeric `category_id` in
a search URL, not a path slug.** The hyphenated paths this project used to
build -- `musical-instruments`, `home-goods`, `sporting-goods` and the rest --
do not exist. A wrong path does not 404; Facebook serves the generic
Marketplace feed, which is why a search for Musical instruments returned 1000L
IBC totes, a townhouse and a Rs 9,00,000 Volvo.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from vingo_scraper.apify.client import build_run_input
from vingo_scraper.config import CATEGORIES

# Read off Facebook's own URLs. Not derived, not guessed.
VERIFIED_IDS = {
    "vehicles": "546583916084032",
    "classifieds": "759201041254543",
    "clothing": "677457442746983",
    "electronics": "479353692612078",
    "entertainment": "529595287638583",
    "family": "891748581240437",
    "garden_outdoor": "1109541402725436",
    "hobbies": "459026188375950",
    "home_goods": "753380185098614",
    "home_improvement": "476291743038859",
    "musical_instruments": "1078592699170502",
    "office_supplies": "586894672179024",
    "pet_supplies": "197143151659643",
    "property_sale": "1270772586445798",
    "sporting_goods": "391335928190702",
    "toys_games": "199404184572737",
}

# The one exception: Facebook gave a path and no id.
NO_ID = {"property_rent"}


def qs(key: str, **kw) -> dict:
    payload = build_run_input(kw.pop("q", None), "bangalore", 24,
                              category=key, **kw)
    return {k: v[0] for k, v in
            parse_qs(urlparse(payload["urls"][0]).query).items()}


# --------------------------------------------------------------------------
# the verified data
# --------------------------------------------------------------------------

def test_01_every_category_is_accounted_for() -> None:
    assert set(CATEGORIES) == set(VERIFIED_IDS) | NO_ID


@pytest.mark.parametrize("key,want", sorted(VERIFIED_IDS.items()))
def test_02_the_id_is_the_one_facebook_gave(key: str, want: str) -> None:
    assert CATEGORIES[key].get("category_id") == want


@pytest.mark.parametrize("key", sorted(VERIFIED_IDS))
def test_03_every_category_sends_its_id(key: str) -> None:
    """Closes the coverage gap: all 16, not the 4 that happened to be used in
    other tests."""
    assert qs(key)["category_id"] == VERIFIED_IDS[key]


@pytest.mark.parametrize("key", sorted(set(VERIFIED_IDS) | NO_ID))
def test_04_every_category_carries_a_search_term(key: str) -> None:
    """Facebook pairs the id WITH a query. A URL with neither is an unbounded
    browse on a per-result-billed API."""
    assert qs(key).get("query"), key


@pytest.mark.parametrize("key", sorted(set(VERIFIED_IDS) | NO_ID))
def test_05_no_category_builds_a_bare_path(key: str) -> None:
    """The shape that served the generic feed."""
    url = build_run_input(None, "bangalore", 24, category=key)["urls"][0]
    assert "/search/" in url, f"{key} built a path, not a search"
    assert "?" in url


def test_06_a_keyword_cannot_scope_a_category() -> None:
    """Written an hour earlier asserting the opposite, on the assumption that
    `?query=guitar&category_id=<instruments>` would mean "guitars within
    instruments". Facebook disproved it on the first paid run: it returns what
    the query asks for and discards the id. The category wins here because the
    endpoint refuses the combination outright."""
    got = qs("musical_instruments", q="guitar")
    assert got["query"] == "Musical instruments"
    assert got["category_id"] == VERIFIED_IDS["musical_instruments"]


def test_07_property_rent_has_no_id_and_says_so() -> None:
    """Deliberate, not an oversight -- Facebook gave a path for this one."""
    assert "category_id" not in CATEGORIES["property_rent"]
    assert qs("property_rent")["query"] == "Property for rent"
    assert "category_id" not in qs("property_rent")


# --------------------------------------------------------------------------
# what was removed, and why
# --------------------------------------------------------------------------

def test_08_mobile_phones_is_gone() -> None:
    """Not in Facebook's category menu at all -- phones live under
    Electronics. Offering it returned the generic feed."""
    assert "mobile_phones" not in CATEGORIES


def test_09_the_dropdown_matches_the_config_exactly() -> None:
    """A label with no backing entry is a button that silently misbehaves; an
    entry with no label is a category nobody can reach."""
    import re

    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    html = TestClient(app).get("/").text
    block = html[html.index('<select id="m-cat">'):]
    block = block[:block.index("</select>")]
    offered = {v for v in re.findall(r'<option value="([a-z_]*)"', block) if v}

    assert offered == set(CATEGORIES), {
        "in the dropdown only": sorted(offered - set(CATEGORIES)),
        "in the config only": sorted(set(CATEGORIES) - offered),
    }


# ==========================================================================
# Category XOR keyword -- Facebook cannot search inside a category
# ==========================================================================

def test_10_a_category_run_uses_the_categorys_own_name() -> None:
    """Never a leftover keyword.

    The run that proved this sent
        ?query=iphone&category_id=<musical instruments>&maxPrice=50000
    and got iPhones under Rs 50,000. maxPrice was honoured, query was honoured,
    `category_id` was discarded -- Facebook treats it as a breadcrumb, not a
    filter, whenever a query contradicts it.
    """
    got = qs("musical_instruments", q="iphone")
    assert got["query"] == "Musical instruments"
    assert "iphone" not in got["query"].lower()
    assert got["category_id"] == VERIFIED_IDS["musical_instruments"]


def test_11_the_api_refuses_both_rather_than_guessing() -> None:
    """A rejected request costs nothing. A silently altered one costs a page
    of scraping -- which this project has now paid for four times."""
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    r = TestClient(app).post("/api/ingest",
                             json={"category": "musical_instruments",
                                   "query": "guitar", "location": "bangalore"})
    assert r.status_code == 400
    assert "cannot be combined" in r.text


def test_12_the_refusal_explains_why() -> None:
    """"Invalid input" sends someone to the code. Naming the cause does not."""
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    body = TestClient(app).post("/api/ingest",
                                json={"category": "vehicles", "query": "honda",
                                      "location": "bangalore"}).text
    assert "ignores the category" in body


def test_13_either_one_alone_is_accepted() -> None:
    """The refusal must not break the two cases that do work."""
    from vingo_scraper.api.main import IngestRequest, title_filter_term

    assert title_filter_term(IngestRequest(query="guitar")) == "guitar"
    assert title_filter_term(IngestRequest(category="vehicles")) is None


def test_14_the_title_filter_never_enforces_an_unsent_keyword() -> None:
    """A category run searches for the category's name, so the keyword never
    reaches Facebook. Enforcing it locally would drop every genuine result."""
    from vingo_scraper.api.main import IngestRequest, title_filter_term

    both = IngestRequest(category="musical_instruments", query="guitar",
                         title_match=True)
    assert title_filter_term(both) is None


def test_15_the_keyword_box_is_not_pre_filled() -> None:
    """It carried `value="iphone"`. Selecting a category and pressing Run sent
    that leftover along, which is how a Musical-instruments search returned
    iPhones. The API-side default was removed earlier; this is its other half.
    """
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    html = TestClient(app).get("/").text
    assert 'value="iphone"' not in html
    assert 'id="m-q" placeholder=' in html


def test_16_the_console_enforces_the_exclusivity() -> None:
    html = page_html()
    assert "category XOR keyword" in html
    assert "kw.disabled" in html and "cat.disabled" in html


def test_17_the_console_explains_the_restriction() -> None:
    """A control that greys out with no reason reads as a bug."""
    assert "Facebook cannot search inside a category" in page_html()


def page_html() -> str:
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app
    return TestClient(app).get("/").text
