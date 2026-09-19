"""Tests for the Apify trigger layer.

Offline: the client is faked. What matters here is the translation from our
(search_query, location, max_items) signature into the Actor's input schema,
and that a failed run is never mistaken for an empty one.
"""

from __future__ import annotations

import pytest

from vingo_scraper.apify import client as apify
from vingo_scraper.apify.client import ApifyRunError, build_run_input, run_actor
from vingo_scraper.apify.urls import category_url, search_url, to_start_urls

# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------

def test_search_url_matches_actor_documented_form():
    """Actor accepts /marketplace/<loc>/search/?query=<term> -- note the slash."""
    url = search_url("mumbai", "iphone", sort_by_new=False)
    assert url == "https://www.facebook.com/marketplace/mumbai/search/?query=iphone"


def test_search_url_encodes_multiword_queries():
    url = search_url("delhi", "washing machine", sort_by_new=False)
    assert "query=washing+machine" in url


def test_category_url():
    assert category_url("pune", "furniture") == (
        "https://www.facebook.com/marketplace/pune/furniture"
    )


def test_to_start_urls_wraps_in_actor_shape():
    assert to_start_urls(["https://x/1", "https://x/2"]) == [
        {"url": "https://x/1"},
        {"url": "https://x/2"},
    ]


# --------------------------------------------------------------------------
# Input translation -- the signature conflict resolution
# --------------------------------------------------------------------------

def test_our_params_become_actor_params():
    """Callers say (query, location, max_items); the Actor hears its own schema."""
    run_input = build_run_input("iphone", "mumbai", 50)

    assert set(run_input) == {"startUrls", "resultsLimit", "includeListingDetails"}
    assert run_input["resultsLimit"] == 50
    assert run_input["includeListingDetails"] is True
    assert len(run_input["startUrls"]) == 1
    assert "marketplace/mumbai/search/" in run_input["startUrls"][0]["url"]

    # The Actor has no such fields; leaking them would be silently ignored.
    for leaked in ("searchQuery", "location", "maxItems"):
        assert leaked not in run_input


def test_results_limit_is_always_set():
    """Unset means UNLIMITED on a per-result-billed Actor. Never allow it."""
    assert build_run_input("x", "mumbai", 10)["resultsLimit"] == 10


def test_include_details_can_be_disabled():
    ri = build_run_input("x", "mumbai", 10, include_details=False)
    assert ri["includeListingDetails"] is False


def test_category_mode():
    ri = build_run_input(None, "pune", 10, category="furniture")
    assert ri["startUrls"][0]["url"].endswith("/pune/furniture")


def test_requires_query_or_category():
    with pytest.raises(ValueError):
        build_run_input(None, "mumbai", 10)


def test_max_items_is_clamped(monkeypatch):
    """A typo in --max-items is a billing incident, so the cap is server-side."""
    monkeypatch.setattr(apify.settings, "apify_max_items_hard_cap", 500)
    assert apify._clamp(10_000) == 500
    assert apify._clamp(100) == 100
    assert apify._clamp(0) == 1


# --------------------------------------------------------------------------
# Run handling
# --------------------------------------------------------------------------

class FakeRun:
    def __init__(self, status="SUCCEEDED", dataset="ds1", cost=0.05):
        self.id = "run123"
        self.status = status
        self.default_dataset_id = dataset
        self.usage_total_usd = cost


class FakeDataset:
    def __init__(self, items):
        self._items = items

    async def iterate_items(self):
        for item in self._items:
            yield item


class FakeActor:
    def __init__(self, run):
        self._run = run
        self.called_with = None

    async def call(self, **kwargs):
        self.called_with = kwargs
        return self._run


class FakeClient:
    def __init__(self, run=None, items=None):
        self._actor = FakeActor(run if run is not None else FakeRun())
        self._items = items or []

    def actor(self, _id):
        return self._actor

    def dataset(self, _id):
        return FakeDataset(self._items)


async def test_successful_run_returns_items():
    items = [{"id": "1", "marketplace_listing_title": "A", "listing_price": {}}]
    fake = FakeClient(items=items)

    run = await run_actor("iphone", "mumbai", 25, client=fake)

    assert run.succeeded
    assert run.run_id == "run123"
    assert run.items == items
    assert run.cost_usd == 0.05
    assert run.cost_per_item == 0.05


async def test_actor_receives_billing_guards():
    """max_items and max_total_charge_usd are enforced by Apify, not the Actor."""
    fake = FakeClient()
    await run_actor("iphone", "mumbai", 25, client=fake)

    kwargs = fake._actor.called_with
    assert kwargs["max_items"] == 25
    assert kwargs["max_total_charge_usd"] > 0
    assert kwargs["run_input"]["resultsLimit"] == 25


async def test_failed_run_raises_rather_than_returning_empty():
    """The whole point: a failed run must not look like 'no inventory'.

    These lead to opposite decisions -- retry versus tell the client Marketplace
    is empty here.
    """
    fake = FakeClient(run=FakeRun(status="FAILED"))
    with pytest.raises(ApifyRunError, match="FAILED"):
        await run_actor("iphone", "mumbai", 25, client=fake)


async def test_timed_out_run_raises():
    fake = FakeClient(run=FakeRun(status="TIMED-OUT"))
    with pytest.raises(ApifyRunError):
        await run_actor("iphone", "mumbai", 25, client=fake)


async def test_none_run_raises():
    fake = FakeClient(run=None)
    fake._actor._run = None
    with pytest.raises(ApifyRunError):
        await run_actor("iphone", "mumbai", 25, client=fake)


async def test_success_without_dataset_raises():
    fake = FakeClient(run=FakeRun(dataset=None))
    with pytest.raises(ApifyRunError, match="no dataset"):
        await run_actor("iphone", "mumbai", 25, client=fake)


async def test_empty_dataset_is_success_not_error():
    """A genuinely empty search succeeds with zero items. That is a density
    signal, and must stay distinguishable from a failure."""
    fake = FakeClient(items=[])
    run = await run_actor("obscure thing", "mumbai", 25, client=fake)
    assert run.succeeded
    assert run.items == []
    assert run.cost_per_item is None


def test_attr_reads_both_object_and_dict_spellings():
    """Apify has moved between typed models and plain dicts across versions."""
    assert apify._attr(FakeRun(), "default_dataset_id", "defaultDatasetId") == "ds1"
    assert apify._attr({"defaultDatasetId": "ds2"}, "default_dataset_id",
                       "defaultDatasetId") == "ds2"
    assert apify._attr({}, "nope") is None


async def test_end_to_end_parse_from_fake_dataset():
    """Dataset items flow through the existing parser untouched."""
    from vingo_scraper.extract.fb_payload import parse_payloads
    from vingo_scraper.extract.schema import ExtractionMethod

    items = [{
        "id": "555",
        "marketplace_listing_title": "Godrej almirah",
        "listing_price": {"amount_with_offset": "850000"},
        "listingUrl": "https://www.facebook.com/marketplace/item/555/",
        "primary_listing_photo": {"image": {"uri": "https://scontent.x/a.jpg"}},
        "location": {"reverse_geocode": {"city": "Pune"}},
    }]
    fake = FakeClient(items=items)
    run = await run_actor("almirah", "pune", 10, client=fake)

    listings = parse_payloads(run.items, city="pune")
    assert len(listings) == 1
    listing = listings[0]
    assert listing.title == "Godrej almirah"
    assert listing.price_minor == 850_000
    assert listing.location_text == "Pune"
    assert listing.extraction_method is ExtractionMethod.APIFY
    # Images start unrehosted -- not servable until the media pipeline runs.
    assert not listing.images[0].is_servable
