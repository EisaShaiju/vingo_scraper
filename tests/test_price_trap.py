"""Regression tests for the price-field trap and attribute extraction.

Both bugs here were found by running against real data from two different
Actors, and both produce *plausible* wrong answers rather than errors — which
is what makes them dangerous. A listings table full of Rs 808 iPhones looks
like cheap stock, not like a parser bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vingo_scraper.extract.fb_payload import (
    _extract_attributes,
    _extract_condition,
    _extract_price,
    parse_payloads,
)
from vingo_scraper.extract.schema import Condition

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------
# the trap: `amount_with_offset` means different things on different sources
# --------------------------------------------------------------------------

def test_curious_coder_offset_is_usd_cents_not_paise():
    """The exact shape that turned Rs 78,000 into Rs 807.92.

    curious_coder sets `amount_with_offset` to the SAME value as
    `amount_with_offset_in_currency` — a USD-cents figure. Reading it as INR
    minor units is wrong by ~100x.
    """
    node = {
        "listing_price": {
            "formatted_amount_zeros_stripped": "₹78,000",
            "amount_with_offset_in_currency": "80792",
            "amount": "78000.00",
            "currency": "INR",
            "amount_with_offset": "80792",
        }
    }
    assert _extract_price(node) == 7_800_000        # Rs 78,000 in paise
    assert _extract_price(node) != 80_792           # the bug


def test_apify_actor_offset_variant_also_rejected():
    """The other Actor's spelling of the same trap."""
    node = {
        "listing_price": {
            "amount": "40000.00",
            "amount_with_offset_in_currency": "41526",   # USD cents
            "formatted_amount_zeros_stripped": "₹40,000",
        }
    }
    assert _extract_price(node) == 4_000_000


def test_amount_wins_over_every_offset_field():
    node = {"listing_price": {"amount": "1500.00", "amount_with_offset": "999999"}}
    assert _extract_price(node) == 150_000


def test_formatted_string_used_when_amount_absent():
    node = {"listing_price": {"formatted_amount": "Rs. 8,500"}}
    assert _extract_price(node) == 850_000


def test_corroborated_offset_accepted_as_last_resort():
    """With no `amount` and no display string, an offset that is NOT simply a
    copy of the currency-converted field is the best we have."""
    node = {
        "listing_price": {
            "amount_with_offset": "250000",
            "amount_with_offset_in_currency": "3012",   # differs -> plausible
        }
    }
    assert _extract_price(node) == 250_000


def test_uncorroborated_offset_refused():
    """Both offsets identical means the 'minor units' field is the converted
    one. Refuse rather than return a confidently wrong price."""
    node = {
        "listing_price": {
            "amount_with_offset": "80792",
            "amount_with_offset_in_currency": "80792",
        }
    }
    assert _extract_price(node) is None


# --------------------------------------------------------------------------
# attributes / condition
# --------------------------------------------------------------------------

def test_condition_read_from_attribute_data():
    """curious_coder has no top-level `condition`; it lives in attributes."""
    node = {"attribute_data": [
        {"attribute_name": "Condition", "value": "used_like_new",
         "label": "Used - like new"},
    ]}
    assert _extract_condition(node) == "Used - like new"


def test_top_level_condition_still_preferred():
    node = {"condition": "Used - Good",
            "attribute_data": [{"attribute_name": "Condition", "label": "New"}]}
    assert _extract_condition(node) == "Used - Good"


def test_attributes_flattened_for_spec_extra_details():
    """Spec: 'any other details shown on the listing (brand, size, model)'."""
    node = {"attribute_data": [
        {"attribute_name": "Brand", "label": "Apple"},
        {"attribute_name": "Model", "label": "iPhone 14 Pro"},
        {"attribute_name": "Condition", "label": "Used - Good"},
    ]}
    attrs = _extract_attributes(node)
    assert attrs == {"Brand": "Apple", "Model": "iPhone 14 Pro",
                     "Condition": "Used - Good"}


def test_attributes_tolerate_junk():
    assert _extract_attributes({}) == {}
    assert _extract_attributes({"attribute_data": None}) == {}
    assert _extract_attributes({"attribute_data": ["nonsense", 42]}) == {}


# --------------------------------------------------------------------------
# against the real 47-listing Bengaluru capture
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bengaluru():
    path = FIXTURES / "curious_coder_bengaluru.json"
    if not path.exists():
        pytest.skip("real capture not present")
    return json.loads(path.read_text("utf-8"))


def test_real_capture_parses_completely(bengaluru):
    out = parse_payloads(bengaluru, city="bangalore")
    assert len(out) == len(bengaluru)


def test_real_prices_are_realistic(bengaluru):
    """The bug produced Rs 800-ish phones. Real second-hand iPhones in
    Bengaluru are thousands, so a median under Rs 5,000 means it is back."""
    out = parse_payloads(bengaluru, city="bangalore")
    prices = sorted(x.price_major for x in out if x.price_minor)
    assert prices, "no prices parsed"
    median = prices[len(prices) // 2]
    assert median > 5_000, f"median Rs {median} — offset trap is back"
    assert all(p < 10_000_000 for p in prices)


def test_real_condition_populated(bengaluru):
    out = parse_payloads(bengaluru, city="bangalore")
    known = [x for x in out if x.condition is not Condition.UNKNOWN]
    assert len(known) == len(out)


# --------------------------------------------------------------------------
# contact extraction — the false-positive that already bit us once
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Facebook listing ids are 16 digits and CONTAIN valid-looking
        # 10-digit runs. Scanning them invents phone numbers.
        ("4328838274095723", False),
        ("1637054228007986", False),
        ("https://facebook.com/marketplace/item/1800637251260742/", False),
        # Genuine Indian mobile formats.
        ("9876543210", True),
        ("call me on 9876543210", True),
        ("+91 98765 43210", True),
        ("098765-43210", True),
        # Not an Indian mobile (must start 6-9).
        ("1234567890", False),
    ],
)
def test_phone_extraction_boundaries(text, expected):
    from vingo_scraper.extract.fb_payload import extract_contacts

    phone, _ = extract_contacts(text)
    assert bool(phone) is expected


def test_contacts_only_scan_seller_authored_text(bengaluru):
    """Across 47 real listings no seller published a phone.

    If this ever returns a number, check it is real before celebrating -- the
    likeliest cause is an id leaking into the scanned text again.
    """
    from vingo_scraper.extract.fb_payload import parse_payloads

    out = parse_payloads(bengaluru, city="bangalore")
    with_phone = [x for x in out if x.seller and x.seller.phone]
    assert with_phone == []


def test_seller_profile_url_built_from_pfbid(bengaluru):
    from vingo_scraper.extract.fb_payload import parse_payloads

    out = parse_payloads(bengaluru, city="bangalore")
    named = [x for x in out if x.seller and x.seller.name]
    assert len(named) == 3          # measured coverage, not aspiration
    for listing in named:
        assert listing.seller.profile_url.startswith("https://www.facebook.com/")


def test_spec_fields_populated_on_real_capture(bengaluru):
    """Every Phase 1 'must have' we can obtain, checked at once."""
    from vingo_scraper.extract.fb_payload import parse_payloads

    out = parse_payloads(bengaluru, city="bangalore")
    n = len(out)
    assert sum(1 for x in out if x.title) == n
    assert sum(1 for x in out if x.price_minor is not None) == n
    assert sum(1 for x in out if x.description) == n
    assert sum(1 for x in out if x.images) == n
    assert sum(1 for x in out if x.location_text) == n
    assert sum(1 for x in out if x.posted_at) == n
    assert sum(1 for x in out if x.listing_url) == n
    assert sum(1 for x in out if x.fb_category_id) == n
    assert sum(1 for x in out if x.attributes) == n
