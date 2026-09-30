"""Regression tests against a REAL Apify dataset.

`tests/fixtures/apify_dataset_sample.json` is genuine output from
`apify/facebook-marketplace-scraper` (10 iPhone listings, Mumbai, Sept 2026),
not hand-written.

This file exists because of a specific failure: the parser was originally built
from the Actor's published field documentation, which advertises Facebook's
snake_case names (`marketplace_listing_title`, `listing_price`). The Actor
actually emits camelCase (`listingTitle`, `listingPrice`). Everything passed
against hand-made fixtures and parsed 0 of 10 real items.

So: assert against real bytes, and keep asserting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vingo_scraper.extract.fb_payload import parse_payloads
from vingo_scraper.extract.schema import Condition, ExtractionMethod, stable_source_key

FIXTURE = Path(__file__).parent / "fixtures" / "apify_dataset_sample.json"


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return json.loads(FIXTURE.read_text("utf-8"))


@pytest.fixture(scope="module")
def listings(items):
    return parse_payloads(items, city="mumbai", category="mobile_phones")


def test_every_item_parses(items, listings):
    """The regression that matters: 0-of-10 must never happen again."""
    assert len(listings) == len(items) == 10


def test_actor_uses_camelcase_not_documented_snake_case(items):
    """Pin the actual contract, so a doc-driven 'fix' fails loudly."""
    item = items[0]
    assert "listingTitle" in item
    assert "listingPrice" in item
    assert "primaryListingPhoto" in item
    # The documented names are simply absent.
    assert "marketplace_listing_title" not in item
    assert "marketplace_listing_seller" not in item


def test_prices_are_realistic_not_currency_converted(listings):
    """Guards the worst available bug.

    `listingPrice.amount_with_offset_in_currency` looks like minor units but is
    a USD-cents conversion: a Rs 40,000 phone reports 41526. Using it would
    price every listing at roughly 1/100th and still look plausible. Real
    second-hand iPhones are thousands of rupees, never tens.
    """
    prices = [x.price_major for x in listings if x.price_minor]
    assert prices
    assert all(1_000 <= p <= 500_000 for p in prices), prices
    assert max(prices) > 10_000


def test_price_matches_display_string(items, listings):
    """Parsed value must equal what Facebook shows the user."""
    by_id = {x.fb_listing_id: x for x in listings}
    for item in items:
        shown = item["listingPrice"].get("formatted_amount_zeros_stripped")
        if not shown:
            continue
        expected = int(float(item["listingPrice"]["amount"]) * 100)
        assert by_id[item["id"]].price_minor == expected, shown


def test_required_fields_present(listings):
    for listing in listings:
        assert listing.title
        assert listing.price_minor is not None
        assert listing.listing_url.startswith("https://www.facebook.com/marketplace/item/")
        assert listing.images
        assert listing.location_text
        assert listing.posted_at is not None
        assert listing.extraction_method is ExtractionMethod.APIFY


def test_iso_timestamps_parse(listings):
    """The Actor sends ISO 8601 with a Z, not unix seconds."""
    assert all(x.posted_at.tzinfo is not None for x in listings)
    assert all(x.posted_at.year >= 2025 for x in listings)


def test_conditions_recognised(listings):
    """Display-text dialect ('Used - Good') must map, not fall to UNKNOWN."""
    known = [x for x in listings if x.condition is not Condition.UNKNOWN]
    assert len(known) >= 8
    assert {x.condition for x in listings} <= set(Condition)


def test_images_are_fbcdn_and_not_yet_servable(listings):
    """Straight from the Actor, images still point at Facebook."""
    for listing in listings:
        for img in listing.images:
            assert "fbcdn" in img.source_url
            assert img.storage_url is None
            assert not img.is_servable


def test_no_seller_data_returned(listings):
    """This Actor returns no seller fields at all.

    Recorded deliberately: it is a DPDP benefit, and if a future Actor starts
    returning sellers, this test failing is the prompt to make a decision
    rather than silently start storing personal data.
    """
    assert all(x.seller is None for x in listings)


def test_source_key_is_stable_across_derivatives(items):
    """Facebook serves several sizes of one photo; identity must survive that.

    Without this, every re-ingest inserts duplicate image rows forever.
    """
    a = ("https://scontent-mia5-1.xx.fbcdn.net/v/t39.84726-6/817651258_n.jpg"
         "?stp=c0.124.261.261a_dst-jpg_p261x260&oh=00_AAA&oe=6AB3DE3E")
    b = ("https://scontent.fboi1-1.fna.fbcdn.net/v/t39.84726-6/817651258_n.jpg"
         "?stp=dst-jpg_s960x960_tt6&oh=00_BBB&oe=6AB3DE3E")
    assert stable_source_key(a) == stable_source_key(b)
    assert stable_source_key(a) == "/v/t39.84726-6/817651258_n.jpg"

    # And it is populated automatically on real data.
    listings = parse_payloads(items, city="mumbai")
    for listing in listings:
        for img in listing.images:
            assert img.source_key
            assert "?" not in img.source_key
            assert not img.source_key.startswith("http")


def test_reparsing_is_stable(items):
    """Same input, same output -- no time- or order-dependent drift."""
    a = parse_payloads(items, city="mumbai")
    b = parse_payloads(items, city="mumbai")
    assert [x.fb_listing_id for x in a] == [x.fb_listing_id for x in b]
    assert [x.price_minor for x in a] == [x.price_minor for x in b]


def test_duplicate_items_collapse(items):
    assert len(parse_payloads(items + items, city="mumbai")) == len(items)
