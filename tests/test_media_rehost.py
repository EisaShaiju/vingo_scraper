"""Tests for the media pipeline.

The load-bearing one is `test_no_fbcdn_url_survives_as_servable`. Everything
else here supports it: the whole point of this subsystem is that no expiring
Facebook URL ever ends up as the thing we render.

All offline -- network is stubbed with respx.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
import respx

from vingo_scraper.extract.schema import ExtractionMethod, ImageStatus, Listing, ListingImage
from vingo_scraper.media.rehost import (
    NotAnImageError,
    rehost_all,
    sniff_image,
    storage_key,
)
from vingo_scraper.media.storage import InMemoryStore

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
HTML_ERROR = b"<!DOCTYPE html><html><body>URL signature expired</body></html>"

FB = "https://scontent.xx.fbcdn.net/v/t45/{n}.jpg?oh=00_AF&oe=68C1ABCD"


def make_listing(fb_id: str, urls: list[str]) -> Listing:
    return Listing(
        fb_listing_id=fb_id,
        listing_url=f"https://www.facebook.com/marketplace/item/{fb_id}/",
        title=f"Listing {fb_id}",
        extraction_method=ExtractionMethod.APIFY,
        images=[
            ListingImage(source_url=u, is_primary=(i == 0))
            for i, u in enumerate(urls)
        ],
    )


# --------------------------------------------------------------------------
# sniffing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("data", "ctype", "ext"),
    [
        (JPEG, "image/jpeg", "jpg"),
        (PNG, "image/png", "png"),
        (GIF, "image/gif", "gif"),
        (WEBP, "image/webp", "webp"),
    ],
)
def test_sniff_recognises_formats(data, ctype, ext):
    assert sniff_image(data) == (ctype, ext)


def test_sniff_rejects_html():
    """An expired fbcdn URL commonly returns an HTML error page with a 200."""
    with pytest.raises(NotAnImageError):
        sniff_image(HTML_ERROR)


def test_sniff_ignores_url_extension():
    """Identification is from bytes, never the .jpg in the URL."""
    with pytest.raises(NotAnImageError):
        sniff_image(b"not an image at all")


def test_storage_key_is_content_addressed():
    digest = hashlib.sha256(JPEG).hexdigest()
    key = storage_key("123", digest, "jpg")
    assert key == f"listings/123/{digest[:16]}.jpg"
    # Same bytes -> same key -> re-upload overwrites instead of duplicating.
    assert storage_key("123", digest, "jpg") == key


# --------------------------------------------------------------------------
# the invariant
# --------------------------------------------------------------------------

@respx.mock
async def test_no_fbcdn_url_survives_as_servable():
    """THE invariant: nothing renderable may point at Facebook.

    `source_url` keeps the fbcdn link for provenance and retry, but
    `storage_url` -- the only field anything downstream renders -- must never
    contain it.
    """
    urls = [FB.format(n=i) for i in range(3)]
    for u in urls:
        respx.get(u).mock(return_value=httpx.Response(200, content=JPEG))

    listings = [make_listing("111", urls)]
    store = InMemoryStore()
    listings, stored, failed = await rehost_all(listings, store=store)

    assert stored == 3
    assert failed == 0
    for img in listings[0].images:
        assert img.status is ImageStatus.STORED
        assert img.storage_url is not None
        assert "fbcdn" not in img.storage_url
        assert img.storage_url.startswith("https://test.storage/public/")
        assert img.is_servable
        # provenance retained, but not servable
        assert "fbcdn" in img.source_url


@respx.mock
async def test_content_type_is_set_on_upload():
    """Supabase defaults objects to text/html; images must override it."""
    url = FB.format(n=1)
    respx.get(url).mock(return_value=httpx.Response(200, content=PNG))

    store = InMemoryStore()
    await rehost_all([make_listing("222", [url])], store=store)

    assert len(store.objects) == 1
    _data, content_type = next(iter(store.objects.values()))
    assert content_type == "image/png"


# --------------------------------------------------------------------------
# failure isolation
# --------------------------------------------------------------------------

@respx.mock
async def test_dead_url_does_not_lose_the_listing():
    """One expired image must cost one image, not the whole listing."""
    good, dead = FB.format(n=1), FB.format(n=2)
    respx.get(good).mock(return_value=httpx.Response(200, content=JPEG))
    respx.get(dead).mock(return_value=httpx.Response(404))

    listings, stored, failed = await rehost_all(
        [make_listing("333", [good, dead])], store=InMemoryStore()
    )

    assert stored == 1
    assert failed == 1
    listing = listings[0]
    assert len(listing.images) == 2
    assert listing.images[0].status is ImageStatus.STORED
    assert listing.images[1].status is ImageStatus.FAILED
    assert listing.images[1].error
    assert listing.images[1].storage_url is None


@respx.mock
async def test_html_error_page_is_rejected_not_uploaded():
    """Expired URLs that 200 with HTML must never reach storage."""
    url = FB.format(n=1)
    respx.get(url).mock(return_value=httpx.Response(200, content=HTML_ERROR))

    store = InMemoryStore()
    listings, stored, failed = await rehost_all(
        [make_listing("444", [url])], store=store
    )

    assert stored == 0
    assert failed == 1
    assert listings[0].images[0].status is ImageStatus.REJECTED
    assert store.objects == {}  # nothing written


@respx.mock
async def test_oversized_image_is_refused():
    from vingo_scraper.config import settings

    url = FB.format(n=1)
    huge = JPEG + b"\x00" * (settings.max_image_bytes + 1)
    respx.get(url).mock(return_value=httpx.Response(200, content=huge))

    listings, stored, failed = await rehost_all(
        [make_listing("555", [url])], store=InMemoryStore()
    )
    assert stored == 0
    assert listings[0].images[0].status is ImageStatus.FAILED


# --------------------------------------------------------------------------
# idempotency and dedupe
# --------------------------------------------------------------------------

@respx.mock
async def test_identical_bytes_upload_once():
    """The same photo reposted across listings is stored a single time."""
    a, b = FB.format(n=1), FB.format(n=2)
    respx.get(a).mock(return_value=httpx.Response(200, content=JPEG))
    respx.get(b).mock(return_value=httpx.Response(200, content=JPEG))

    store = InMemoryStore()
    listings, stored, _ = await rehost_all(
        [make_listing("666", [a]), make_listing("777", [b])], store=store
    )

    assert stored == 2
    # Both listings resolve, but only one object was written.
    assert len(store.objects) == 1
    assert listings[0].images[0].sha256 == listings[1].images[0].sha256


@respx.mock
async def test_rerun_is_idempotent():
    """Re-ingesting produces the same key and the same URL."""
    url = FB.format(n=1)
    respx.get(url).mock(return_value=httpx.Response(200, content=JPEG))
    store = InMemoryStore()

    first, _, _ = await rehost_all([make_listing("888", [url])], store=store)
    keys_after_first = set(store.objects)
    second, _, _ = await rehost_all([make_listing("888", [url])], store=store)

    assert set(store.objects) == keys_after_first
    assert first[0].images[0].storage_url == second[0].images[0].storage_url


@respx.mock
async def test_listing_with_no_images_is_unharmed():
    listings, stored, failed = await rehost_all(
        [make_listing("999", [])], store=InMemoryStore()
    )
    assert (stored, failed) == (0, 0)
    assert listings[0].title == "Listing 999"


@respx.mock
async def test_concurrency_is_bounded():
    """Downloads must respect the semaphore rather than opening all at once."""
    from vingo_scraper.config import settings

    live = 0
    peak = 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return httpx.Response(200, content=JPEG)

    urls = [FB.format(n=i) for i in range(20)]
    for u in urls:
        respx.get(u).mock(side_effect=handler)

    await rehost_all([make_listing("1000", urls)], store=InMemoryStore())
    assert peak <= settings.image_concurrency
