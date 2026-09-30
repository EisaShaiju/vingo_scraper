"""Golden tests for the GraphQL parser.

These run against frozen payloads with zero network calls, so a parser
regression is caught in CI without touching Facebook. When FB changes shape,
the fix loop is: capture a fresh payload, drop it in fixtures/, watch this go
red, adjust the key lists, watch it go green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vingo_scraper.extract.fb_payload import parse_payloads
from vingo_scraper.extract.schema import Condition, ExtractionMethod, ReviewStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def search_payload() -> dict:
    return json.loads((FIXTURES / "marketplace_search_sample.json").read_text("utf-8"))


def test_finds_all_listings(search_payload):
    listings = parse_payloads([search_payload], city="mumbai", category="electronics")
    assert len(listings) == 3


def test_core_fields(search_payload):
    listings = {
        listing.fb_listing_id: listing
        for listing in parse_payloads([search_payload], city="mumbai")
    }

    iphone = listings["1122334455667788"]
    assert iphone.title.startswith("iPhone 13")
    # amount_with_offset is already minor units and must be used verbatim.
    assert iphone.price_minor == 4_200_000
    assert iphone.price_major == 42_000.0
    assert iphone.condition is Condition.USED_LIKE_NEW
    assert iphone.location_text == "Bandra, Mumbai"
    assert iphone.seller.fb_seller_id == "998877665544"
    assert iphone.posted_at is not None
    assert iphone.extraction_method is ExtractionMethod.APIFY


def test_price_falls_back_to_formatted_string(search_payload):
    listings = {
        listing.fb_listing_id: listing for listing in parse_payloads([search_payload])
    }
    # No amount/amount_with_offset -- only "Rs. 8,500".
    assert listings["2233445566778899"].price_minor == 850_000


def test_free_listing_is_zero_not_none(search_payload):
    """A free item must be 0, distinct from 'price could not be read' (None)."""
    listings = {
        listing.fb_listing_id: listing for listing in parse_payloads([search_payload])
    }
    boxes = listings["3344556677889900"]
    assert boxes.price_minor == 0
    assert boxes.price_minor is not None


def test_alternate_title_key(search_payload):
    """custom_title is used when marketplace_listing_title is absent."""
    listings = {
        listing.fb_listing_id: listing for listing in parse_payloads([search_payload])
    }
    assert listings["3344556677889900"].title == "Free moving boxes - pickup only"


def test_description_whitespace_collapsed(search_payload):
    listings = {
        listing.fb_listing_id: listing for listing in parse_payloads([search_payload])
    }
    desc = listings["3344556677889900"].description
    assert "  " not in desc
    assert desc.startswith("About 15 sturdy")


def test_images_primary_flagged_and_deduped(search_payload):
    listings = {
        listing.fb_listing_id: listing for listing in parse_payloads([search_payload])
    }
    images = listings["1122334455667788"].images
    assert len(images) == 3
    assert images[0].is_primary is True
    assert sum(img.is_primary for img in images) == 1
    assert len({img.source_url for img in images}) == 3
    # Rehosting is off by default; nothing should be rehosted yet.
    assert all(img.storage_url is None for img in images)


def test_everything_lands_pending_review(search_payload):
    """Nothing may reach buyers without a human approving it."""
    listings = parse_payloads([search_payload])
    assert all(x.review_status is ReviewStatus.PENDING_REVIEW for x in listings)


def test_listing_url_built_when_absent(search_payload):
    listings = parse_payloads([search_payload])
    assert all("/marketplace/item/" in x.listing_url for x in listings)


def test_survives_wrapper_restructuring(search_payload):
    """The whole point of the tree-walk: renamed wrappers must not break us.

    We rebuild the payload under completely different wrapper keys and an extra
    nesting layer. The listing objects are untouched, so extraction must be
    identical.
    """
    edges = search_payload["data"]["marketplace_search"]["feed_units"]["edges"]
    mutated = {
        "data": {
            "viewer": {
                "marketplace_feed_stories_v2": {
                    "some_new_connection": {
                        "results": [{"wrapped": edge["node"]} for edge in edges]
                    }
                }
            }
        }
    }
    assert len(parse_payloads([mutated])) == 3


def test_duplicate_listings_collapse(search_payload):
    """Same payload twice must not yield doubled rows."""
    assert len(parse_payloads([search_payload, search_payload])) == 3


def test_ignores_non_listing_nodes():
    """Id-bearing nodes without title+price must not be harvested."""
    noise = {
        "data": {
            "viewer": {
                "id": "123",
                "actor": {"id": "456", "name": "Someone"},
                "notifications": [{"id": "789", "title": "A notification"}],
            }
        }
    }
    assert parse_payloads([noise]) == []


def test_empty_and_malformed_payloads_are_safe():
    assert parse_payloads([]) == []
    assert parse_payloads([{}]) == []
    assert parse_payloads([{"data": None}]) == []
    assert parse_payloads([{"data": {"marketplace_search": None}}]) == []
