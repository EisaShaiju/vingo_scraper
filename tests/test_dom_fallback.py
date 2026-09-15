"""Tests for the DOM fallback extractor.

The guarantee under test: when GraphQL extraction yields nothing, we still
produce valid Listing objects on the same contract, tagged DOM_FALLBACK so the
monitor can see the degradation.

Note the HTML below uses deliberately meaningless hashed class names, mirroring
what Facebook actually ships. If a change to dom.py ever makes these tests
depend on those class names, the fallback has become as fragile as the thing it
is meant to back up.
"""

from __future__ import annotations

import json

from vingo_scraper.extract.dom import parse_html
from vingo_scraper.extract.schema import ExtractionMethod, Listing, ReviewStatus

SEARCH_HTML = """
<html><body>
  <div class="x1n2onr6 x1ja2u2z">
    <a class="x1i10hfl xjbqb8w" href="/marketplace/item/9988776655/?ref=search">
      <img class="xt7dq6l" src="https://scontent.example.com/tile_a.jpg"/>
      <span class="x1lliihq">&#8377;42,000</span>
      <span class="x1lliihq">iPhone 13 128GB Midnight</span>
      <span class="x1lliihq">Bandra, Mumbai</span>
    </a>
    <a class="x1i10hfl xjbqb8w" href="/marketplace/item/1122334455/">
      <img class="xt7dq6l" src="https://scontent.example.com/tile_b.jpg"/>
      <span class="x1lliihq">Rs. 8,500</span>
      <span class="x1lliihq">Godrej 3-door almirah</span>
      <span class="x1lliihq">Andheri West</span>
    </a>
    <a class="x1i10hfl" href="/marketplace/item/5566778899/">
      <span class="x1lliihq">Free</span>
      <span class="x1lliihq">Moving boxes, pickup only</span>
      <span class="x1lliihq">Powai</span>
    </a>
    <a class="nav" href="/marketplace/you/selling/">Selling</a>
    <a class="nav" href="/marketplace/notifications/">Notifications</a>
  </div>
</body></html>
"""


def test_extracts_only_real_listings():
    """Nav links under /marketplace/ must not be mistaken for listings."""
    listings = parse_html(SEARCH_HTML, city="mumbai", category="electronics")
    assert len(listings) == 3
    assert {x.fb_listing_id for x in listings} == {
        "9988776655",
        "1122334455",
        "5566778899",
    }


def test_tagged_as_fallback():
    """The monitor keys off this -- a mislabelled row hides the degradation."""
    listings = parse_html(SEARCH_HTML, city="mumbai")
    assert all(x.extraction_method is ExtractionMethod.DOM_FALLBACK for x in listings)


def test_title_price_and_location_split():
    by_id = {x.fb_listing_id: x for x in parse_html(SEARCH_HTML, city="mumbai")}

    iphone = by_id["9988776655"]
    assert iphone.title == "iPhone 13 128GB Midnight"
    assert iphone.price_minor == 4_200_000
    assert iphone.location_text == "Bandra, Mumbai"
    assert iphone.images[0].source_url.endswith("tile_a.jpg")

    almirah = by_id["1122334455"]
    assert almirah.title == "Godrej 3-door almirah"
    assert almirah.price_minor == 850_000


def test_free_listing_zero_not_none():
    by_id = {x.fb_listing_id: x for x in parse_html(SEARCH_HTML)}
    boxes = by_id["5566778899"]
    assert boxes.price_minor == 0
    assert boxes.title == "Moving boxes, pickup only"


def test_same_contract_as_graphql():
    """Downstream code must not be able to tell the paths apart structurally."""
    listings = parse_html(SEARCH_HTML, city="mumbai")
    assert all(isinstance(x, Listing) for x in listings)
    assert all(x.review_status is ReviewStatus.PENDING_REVIEW for x in listings)
    assert all("/marketplace/item/" in x.listing_url for x in listings)
    assert all(x.scraped_at is not None for x in listings)


def test_embedded_json_preferred_over_anchors():
    """Inlined JSON is better data than rendered text, so it wins.

    It must still be tagged DOM_FALLBACK even though a GraphQL-shaped parser
    read it -- the tag records which *path* ran, not which parser.
    """
    payload = {
        "require": [
            {
                "listing": {
                    "__typename": "GroupCommerceProductItem",
                    "id": "7777777777",
                    "marketplace_listing_title": "Sony WH-1000XM4 headphones",
                    "listing_price": {"amount_with_offset": "1500000"},
                    "location_vanity_or_city_and_state": "Koramangala, Bengaluru",
                    "marketplace_listing_seller": {"id": "555", "name": "Anil K."},
                }
            }
        ]
    }
    html = (
        "<html><body><script>"
        + json.dumps(payload)
        + "</script>"
        + SEARCH_HTML
        + "</body></html>"
    )

    listings = parse_html(html, city="bengaluru")
    ids = {x.fb_listing_id for x in listings}
    assert "7777777777" in ids

    sony = next(x for x in listings if x.fb_listing_id == "7777777777")
    assert sony.title == "Sony WH-1000XM4 headphones"
    assert sony.price_minor == 1_500_000
    assert sony.seller.name == "Anil K."
    assert sony.extraction_method is ExtractionMethod.DOM_FALLBACK


def test_empty_and_junk_html_are_safe():
    assert parse_html("") == []
    assert parse_html("<html><body><p>nothing here</p></body></html>") == []
    assert parse_html("<<<not really html") == []


def test_no_dependency_on_hashed_class_names():
    """Strip every class attribute; extraction must be unaffected.

    This is the regression guard that keeps the fallback structural.
    """
    import re

    stripped = re.sub(r'class="[^"]*"', "", SEARCH_HTML)
    assert len(parse_html(stripped, city="mumbai")) == 3
