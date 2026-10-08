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


# ==========================================================================
# One box, two meanings
# ==========================================================================
#
# Facebook has ONE search box, and picking a category fills it. Two separate
# controls invited a combination the platform cannot honour -- sending
# `?query=iphone&category_id=<musical instruments>` returned iPhones, because
# `category_id` loses to a conflicting query. An earlier fix greyed one control
# out when the other was set; this removes the ability to express it at all.


def console() -> str:
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app
    return TestClient(app).get("/").text


def label_map() -> dict[str, str]:
    """`CATEGORY_BY_LABEL` as the browser receives it."""
    import re

    block = console()
    block = block[block.index("const CATEGORY_BY_LABEL = {"):]
    block = block[:block.index("};")]
    return {m[0].lower(): m[1]
            for m in re.findall(r"'([^']+)':\s*'([^']+)'", block)}


def datalist() -> set[str]:
    import re

    html = console()
    block = html[html.index('<datalist id="m-cats">'):]
    block = block[:block.index("</datalist>")]
    return set(re.findall(r'<option value="([^"]+)">', block))


# --------------------------------------------------------------------------
# the control itself
# --------------------------------------------------------------------------

def test_10_there_is_one_search_field_not_two() -> None:
    """Two controls for one decision invite the combination, then block it."""
    html = console()
    assert 'id="m-search"' in html
    assert 'id="m-cat"' not in html
    assert 'id="m-q"' not in html


def test_11_the_box_is_not_pre_filled() -> None:
    """It carried `value="iphone"`. Picking a category and pressing Run sent
    that leftover along, which is how a Musical-instruments search returned
    iPhones."""
    html = console()
    assert 'value="iphone"' not in html
    assert 'placeholder="Type a keyword, or pick a category"' in html


# --------------------------------------------------------------------------
# the resolver -- what the typed value means
# --------------------------------------------------------------------------

def test_12_every_category_is_pickable() -> None:
    """The whole contract: picking "Musical instruments" must browse the
    category, not search for the words."""
    want = {v["query"] for v in CATEGORIES.values()}
    assert datalist() == want


def test_13_every_label_resolves_to_its_config_key() -> None:
    """All 17, so a pick can never silently become a keyword search."""
    got = label_map()
    for key, meta in CATEGORIES.items():
        assert got.get(meta["query"].lower()) == key, meta["query"]


def test_14_the_map_and_the_config_cannot_drift() -> None:
    """A label with no key is a pick that quietly searches instead of
    browsing; a key with no label is a category nobody can reach."""
    assert set(label_map().values()) == set(CATEGORIES)


def test_15_matching_is_case_insensitive() -> None:
    """The box is typed into as well as picked from."""
    html = console()
    assert "text.toLowerCase()" in html


def test_16_anything_else_is_a_keyword() -> None:
    """"guitar" and "Musical instrument" (singular) are searches, not browses
    -- only an exact category name selects the category."""
    got = label_map()
    for typed in ("guitar", "musical instrument", "musical", "iphone", "xbox"):
        assert typed not in got, typed


def test_17_an_empty_box_selects_nothing() -> None:
    """So the endpoint's 400 still guards a blank run."""
    html = console()
    assert "if (!text) return { category: null, query: '' };" in html


# --------------------------------------------------------------------------
# the API guard behind it
# --------------------------------------------------------------------------

def test_18_the_api_still_refuses_both() -> None:
    """The console can no longer send both, but a stale tab or a direct call
    can. Four inputs have now been accepted and quietly altered in this
    project; the guard costs nothing."""
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app

    r = TestClient(app).post("/api/ingest",
                             json={"category": "musical_instruments",
                                   "query": "guitar", "location": "bangalore"})
    assert r.status_code == 400
    assert "cannot be combined" in r.text


def test_19_the_title_filter_never_enforces_an_unsent_keyword() -> None:
    """A category run searches for the category's own name, so the keyword
    never reaches Facebook. Enforcing it locally would drop every result."""
    from vingo_scraper.api.main import IngestRequest, title_filter_term

    assert title_filter_term(IngestRequest(query="guitar")) == "guitar"
    assert title_filter_term(IngestRequest(category="vehicles")) is None
