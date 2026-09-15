"""Unit tests for value normalization. No network, no browser."""

from __future__ import annotations

import pytest

from vingo_scraper.extract.schema import Condition
from vingo_scraper.pipeline.normalize import (
    clean_text,
    parse_condition,
    parse_price_to_minor,
    parse_timestamp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u20b912,500", 1_250_000),
        ("Rs. 12500", 1_250_000),
        ("INR 12,500.50", 1_250_050),
        ("12,500", 1_250_000),
        (15000, 1_500_000),
        (15000.5, 1_500_050),
        # Indian short forms -- common in vehicle and property listings.
        ("1.2 lakh", 12_000_000),
        ("\u20b945k", 4_500_000),
        ("2.5 cr", 250_000_000_0),
    ],
)
def test_price_parsing(raw, expected):
    assert parse_price_to_minor(raw) == expected


def test_free_is_zero():
    assert parse_price_to_minor("Free") == 0


@pytest.mark.parametrize("raw", ["", None, "abc", "   "])
def test_unparseable_price_is_none(raw):
    """None means 'could not read', which is NOT the same as free (0)."""
    assert parse_price_to_minor(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # GraphQL enum-token dialect.
        ("used_like_new", Condition.USED_LIKE_NEW),
        ("USED_LIKE_NEW", Condition.USED_LIKE_NEW),
        ("used_good", Condition.USED_GOOD),
        ("NEW", Condition.NEW),
        # DOM display-text dialect -- must land on the same values.
        ("Used - Like New", Condition.USED_LIKE_NEW),
        ("Used  -  Good", Condition.USED_GOOD),
        ("Brand New", Condition.NEW),
        # Unknown / missing.
        (None, Condition.UNKNOWN),
        ("refurbished-ish", Condition.UNKNOWN),
    ],
)
def test_condition_both_dialects(raw, expected):
    assert parse_condition(raw) is expected


def test_timestamp_seconds_and_millis_agree():
    assert parse_timestamp(1726387200) == parse_timestamp(1726387200000)


@pytest.mark.parametrize("raw", [None, "not-a-number", ""])
def test_bad_timestamp_is_none(raw):
    assert parse_timestamp(raw) is None


def test_clean_text_collapses_whitespace():
    assert clean_text("  a   b \n c ") == "a b c"


def test_clean_text_truncates():
    assert clean_text("x" * 50, limit=10).startswith("x" * 10)
    assert len(clean_text("x" * 50, limit=10)) == 11  # + ellipsis
