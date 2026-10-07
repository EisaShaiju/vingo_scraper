"""Our run input must conform to the Actor's LIVE input schema.

Why this file exists: every category search returned nothing, and the reason
was a field SHAPE, not a field name. `urls` is correct and always was -- but
the schema types it as an array with editor `stringList`, prefilled with a bare
URL string, while we were sending the legacy Actor's `[{"url": ...}]` objects.

Apify fails the ENTIRE run on bad input, before scraping, so the symptom was a
button that did nothing. No test could catch it, because every test we had
asserted our payload against our own assumptions.

The fixture is the real thing, fetched from
`GET /v2/actor-builds/{id}` -> `inputSchema`. To refresh it:

    python -c "import json,urllib.request;t=...;  # token from .env
    d=json.load(urllib.request.urlopen(
      f'https://api.apify.com/v2/acts/curious_coder~facebook-marketplace?token={t}'))['data']
    b=d['taggedBuilds']['latest']['buildId']
    s=json.load(urllib.request.urlopen(
      f'https://api.apify.com/v2/actor-builds/{b}?token={t}'))['data']['inputSchema']
    open('tests/fixtures/curious_coder_input_schema.json','w').write(
      json.dumps(json.loads(s) if isinstance(s,str) else s, indent=2))"

If the Actor changes under us, these tests fail here rather than on a paid run.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from vingo_scraper.apify.client import (
    DAYS_SINCE_LISTED_CHOICES,
    SORT_NEWEST,
    build_run_input,
)

SCHEMA = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "curious_coder_input_schema.json")
    .read_text(encoding="utf-8")
)
PROPS: dict = SCHEMA["properties"]

# Keys we attach for our own bookkeeping and strip before sending.
PRIVATE_PREFIX = "_"


def payloads() -> list[tuple[str, dict]]:
    """Every shape of input the console can produce."""
    return [
        ("keyword", build_run_input("xbox", "bangalore", 24)),
        ("category", build_run_input(None, "bangalore", 72, category="electronics")),
        ("category+keyword",
         build_run_input("laptop", "bangalore", 48, category="electronics")),
        ("filtered", build_run_input(
            "sofa", "bangalore", 24,
            filters={"daysSinceListed": "1", "strictFiltering": True,
                     "minPrice": 1000, "maxPrice": 50_000})),
    ]


# --------------------------------------------------------------------------
# conformance
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,payload", payloads(), ids=lambda v: v if isinstance(v, str) else "")
def test_01_every_key_we_send_exists_in_the_schema(name: str, payload: dict) -> None:
    """One unrecognised field fails the whole run -- and still costs nothing
    to catch here."""
    unknown = [k for k in payload if not k.startswith(PRIVATE_PREFIX) and k not in PROPS]
    assert unknown == [], f"{name}: Actor has no such input(s): {unknown}"


@pytest.mark.parametrize("name,payload", payloads(), ids=lambda v: v if isinstance(v, str) else "")
def test_02_every_value_matches_its_declared_type(name: str, payload: dict) -> None:
    kinds = {"string": str, "integer": int, "boolean": bool, "array": list,
             "object": dict, "number": (int, float)}
    for key, value in payload.items():
        if key.startswith(PRIVATE_PREFIX):
            continue
        want = kinds.get(PROPS[key].get("type"))
        if want is None:
            continue
        assert isinstance(value, want), (
            f"{name}: {key} is {type(value).__name__}, schema says {PROPS[key]['type']}"
        )


# --------------------------------------------------------------------------
# the bug this file was written for
# --------------------------------------------------------------------------

def test_03_urls_is_a_list_of_plain_strings() -> None:
    """The actual cause of "category search does nothing".

    `editor: stringList` means bare URLs. We were sending the legacy Actor's
    `[{"url": ...}]`, which fails validation before anything is scraped.
    """
    assert PROPS["urls"]["editor"] == "stringList"
    payload = build_run_input(None, "bangalore", 24, category="vehicles")
    assert all(isinstance(u, str) for u in payload["urls"])
    assert payload["urls"][0].startswith("https://www.facebook.com/marketplace/")


def test_04_the_schemas_own_prefill_is_a_bare_string() -> None:
    """Independent of our code: the Actor itself documents the shape."""
    assert all(isinstance(u, str) for u in PROPS["urls"]["prefill"])


def test_05_a_keyword_scopes_a_category_instead_of_replacing_it() -> None:
    """Electronics + "laptop" is laptops within electronics, not all of it.

    The keyword now rides INSIDE the URL rather than in `searchKeyword`: the
    Actor discards Filters-mode fields whenever `urls` is supplied, so a split
    across both modes is a filter that silently does nothing.
    `tests/test_actor_modes.py` owns that rule.
    """
    payload = build_run_input("laptop", "bangalore", 24, category="electronics")
    assert "searchKeyword" not in payload
    assert "query=laptop" in payload["urls"][0]


def test_06_a_category_alone_sends_no_keyword() -> None:
    payload = build_run_input(None, "bangalore", 24, category="vehicles")
    assert "searchKeyword" not in payload


def test_07_a_keyword_alone_sends_no_urls() -> None:
    payload = build_run_input("xbox", "bangalore", 24)
    assert "urls" not in payload
    assert payload["searchKeyword"] == "xbox"


# --------------------------------------------------------------------------
# our constants vs the schema's own enums
# --------------------------------------------------------------------------

def test_08_recency_choices_match_the_actor_exactly() -> None:
    assert tuple(PROPS["daysSinceListed"]["enum"]) == DAYS_SINCE_LISTED_CHOICES


def test_09_our_sort_is_a_value_the_actor_accepts() -> None:
    assert SORT_NEWEST in PROPS["sortBy"]["enum"]


def test_10_there_is_still_no_category_parameter() -> None:
    """The reason a category has to be a URL at all. If this ever fails, the
    Actor grew a real category input and `urls` can stop carrying it."""
    assert not [k for k in PROPS if "categor" in k.lower()]


# ==========================================================================
# Volume -- the "always 24 listings" bug
# ==========================================================================

def test_11_the_page_ceiling_follows_the_PLAN_not_a_constant() -> None:
    """Volume is a property of the Apify account, not of the code.

    MEASURED on the free plan -- the Actor answers every request with roughly
    one page whatever it is asked for:

        pages asked -> items returned
           1              25
           2              21, 21, 16
           3              26, 28, 21, 26

    An earlier version of this test asserted 1/2/3 pages from 24/48/72, which
    was true of our payload and false of the world: the console promised
    "~72 listings" on a plan that cannot deliver them.
    """
    from vingo_scraper.apify.client import build_run_input as b
    from vingo_scraper.apify.client import max_pages

    for want in (24, 48, 72):
        assert b("x", "bangalore", want)["maxPagesPerUrl"] <= max_pages()


def test_11b_a_paid_plan_restores_the_pages(monkeypatch) -> None:
    """The ceiling is configuration, so upgrading lifts it with no code change."""
    from vingo_scraper.apify import client
    from vingo_scraper.config import settings

    monkeypatch.setattr(settings, "apify_max_pages", 3)
    assert client.pages_for(72) == 3
    assert client.build_run_input("x", "bangalore", 72)["maxPagesPerUrl"] == 3


def test_12_the_api_ceiling_does_not_undo_the_page_math() -> None:
    """The two numbers lived in different files and disagreed."""
    from vingo_scraper.apify.client import (
        MAX_ITEMS_CEILING,
        MAX_PAGES,
        build_run_input,
        pages_for,
    )
    assert pages_for(MAX_ITEMS_CEILING) == MAX_PAGES
    clamped = max(1, min(72, MAX_ITEMS_CEILING))
    assert build_run_input("x", "bangalore", clamped)["maxPagesPerUrl"] == MAX_PAGES


def test_13_volume_is_bounded() -> None:
    """A typo in the form must not buy twenty pages."""
    from vingo_scraper.apify.client import MAX_PAGES, pages_for
    assert pages_for(10_000) == MAX_PAGES
    assert pages_for(0) == 1
    assert pages_for(-5) == 1


def test_14_the_console_offers_only_what_the_plan_can_deliver() -> None:
    """A selector option the Actor will not honour is a label that lies, which
    is how "Items: 8" and "3 pages, ~72" both got shipped."""
    from fastapi.testclient import TestClient

    from vingo_scraper.api.main import app
    from vingo_scraper.apify.client import LISTINGS_PER_PAGE, max_pages

    html = TestClient(app).get("/").text
    offered = re.findall(r'<option value="(\d+)">[^<]*listings', html)
    assert offered, "no volume options found"
    for value in offered:
        assert int(value) <= LISTINGS_PER_PAGE * max_pages(), value


# ==========================================================================
# Recency -- one control, two destinations
# ==========================================================================

def test_15_a_day_window_reaches_apify() -> None:
    """Day granularity is what Facebook can filter on, so it cuts the bill."""
    from vingo_scraper.api.main import IngestRequest
    assert IngestRequest(days_since_listed="1").to_filters()["daysSinceListed"] == "1"


def test_16_an_hour_window_never_reaches_apify() -> None:
    """Facebook has no hourly filter. Sending one fails the run."""
    from vingo_scraper.api.main import IngestRequest
    sent = IngestRequest(max_age_hours=6).to_filters()
    assert "max_age_hours" not in sent and "hours" not in str(sent)


def test_17_only_schema_values_are_ever_sent_as_recency() -> None:
    from vingo_scraper.api.main import IngestRequest
    for choice in DAYS_SINCE_LISTED_CHOICES:
        sent = IngestRequest(days_since_listed=choice).to_filters()
        assert sent.get("daysSinceListed", "") in PROPS["daysSinceListed"]["enum"]
