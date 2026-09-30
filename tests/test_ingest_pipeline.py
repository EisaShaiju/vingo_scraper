"""End-to-end wiring: Apify -> parse -> rehost -> persist.

Persistence is stubbed (it needs Postgres), but the ordering guarantee is not:
these assert that images are rehosted BEFORE anything is handed to the database.
That ordering is the whole reason the media pipeline exists -- rehosting after
a write would routinely find fbcdn urls already expired.
"""

from __future__ import annotations

import httpx
import respx

from tests.test_apify_client import FakeClient
from vingo_scraper.apify import runner
from vingo_scraper.extract.schema import ImageStatus
from vingo_scraper.media.storage import InMemoryStore

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
IMG_A = "https://scontent.xx.fbcdn.net/v/t45/a.jpg?oh=00_AF&oe=68C1"
IMG_B = "https://scontent.xx.fbcdn.net/v/t45/b.jpg?oh=00_AF&oe=68C1"

ITEMS = [
    {
        "id": "1001",
        "marketplace_listing_title": "iPhone 13 128GB",
        "marketplace_listing_description": {"text": "Barely used, with box."},
        "listing_price": {"amount_with_offset": "4200000"},
        "listingUrl": "https://www.facebook.com/marketplace/item/1001/",
        "primary_listing_photo": {"image": {"uri": IMG_A}},
        "marketplace_listing_seller": {"id": "s1", "name": "Rohit S."},
        "location": {"reverse_geocode": {"city": "Mumbai"}},
    },
    {
        "id": "1002",
        "marketplace_listing_title": "Godrej almirah",
        "listing_price": {"formatted_amount": "Rs. 8,500"},
        "primary_listing_photo": {"image": {"uri": IMG_B}},
        "location": {"reverse_geocode": {"city": "Mumbai"}},
    },
]


def _patch(monkeypatch, store, items=ITEMS, persisted=None):
    """Wire a fake Apify client and in-memory store into the runner."""
    fake = FakeClient(items=items)

    async def fake_run_actor(*args, **kwargs):
        from vingo_scraper.apify.client import run_actor
        return await run_actor(*args, client=fake, **kwargs)

    async def fake_persist(result, run):
        if persisted is not None:
            # Snapshot exactly what persistence would have received.
            persisted.extend(
                (img.status, img.storage_url, img.source_url)
                for listing in result.listings
                for img in listing.images
            )
        return len(result.listings), 0

    monkeypatch.setattr(runner, "run_actor", fake_run_actor)
    monkeypatch.setattr(runner, "_persist", fake_persist)
    monkeypatch.setattr(
        "vingo_scraper.media.storage.SupabaseStore", lambda *a, **k: store
    )
    return fake


@respx.mock
async def test_full_ingest(monkeypatch):
    respx.get(IMG_A).mock(return_value=httpx.Response(200, content=JPEG))
    respx.get(IMG_B).mock(return_value=httpx.Response(200, content=JPEG))

    store = InMemoryStore()
    _patch(monkeypatch, store)

    result = await runner.ingest(
        search_query="iphone", location="mumbai", max_items=10
    )

    assert result.error is None
    assert result.items_returned == 2
    assert len(result.listings) == 2
    assert result.images_stored == 2
    assert result.images_failed == 0
    assert result.created == 2

    listing = next(x for x in result.listings if x.fb_listing_id == "1001")
    assert listing.title == "iPhone 13 128GB"
    assert listing.description == "Barely used, with box."
    assert listing.price_minor == 4_200_000
    assert listing.seller.name == "Rohit S."


@respx.mock
async def test_images_are_rehosted_before_persistence(monkeypatch):
    """The ordering guarantee, asserted at the persistence boundary."""
    respx.get(IMG_A).mock(return_value=httpx.Response(200, content=JPEG))
    respx.get(IMG_B).mock(return_value=httpx.Response(200, content=JPEG))

    persisted: list[tuple] = []
    _patch(monkeypatch, InMemoryStore(), persisted=persisted)

    await runner.ingest(search_query="iphone", location="mumbai", max_items=10)

    assert len(persisted) == 2
    for status, storage_url, source_url in persisted:
        # By the time the DB sees it, it is already ours.
        assert status is ImageStatus.STORED
        assert storage_url and "fbcdn" not in storage_url
        # Provenance kept, but it is not the servable url.
        assert "fbcdn" in source_url


@respx.mock
async def test_dry_run_touches_neither_storage_nor_db(monkeypatch):
    store = InMemoryStore()
    persisted: list[tuple] = []
    _patch(monkeypatch, store, persisted=persisted)

    result = await runner.ingest(
        search_query="iphone", location="mumbai", max_items=10, dry_run=True
    )

    assert len(result.listings) == 2
    assert store.objects == {}
    assert persisted == []
    assert result.created == 0


@respx.mock
async def test_partial_image_failure_still_ingests(monkeypatch):
    """A dead image costs one photo, never the listing."""
    respx.get(IMG_A).mock(return_value=httpx.Response(200, content=JPEG))
    respx.get(IMG_B).mock(return_value=httpx.Response(403))  # expired

    _patch(monkeypatch, InMemoryStore())
    result = await runner.ingest(
        search_query="iphone", location="mumbai", max_items=10
    )

    assert len(result.listings) == 2
    assert result.images_stored == 1
    assert result.images_failed == 1


async def test_apify_failure_surfaces_as_error(monkeypatch):
    from vingo_scraper.apify.client import ApifyRunError

    async def boom(*a, **k):
        raise ApifyRunError("run status=FAILED")

    monkeypatch.setattr(runner, "run_actor", boom)
    result = await runner.ingest(
        search_query="x", location="mumbai", max_items=10
    )

    assert result.error == "run status=FAILED"
    assert result.listings == []


async def test_empty_result_is_not_an_error(monkeypatch):
    """Zero listings is a density signal, not a failure -- and the exit code
    must distinguish the two."""
    _patch(monkeypatch, InMemoryStore(), items=[])
    result = await runner.ingest(
        search_query="obscure", location="mumbai", max_items=10
    )

    assert result.error is None
    assert result.listings == []
    assert result.items_returned == 0
