"""The handoff to Polaris.

Everything here guards the same asymmetry: by the time a webhook fires, the
listing has been scraped, paid for, rehosted and committed. Polaris being down,
slow or wrong must not undo any of that.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from vingo_scraper.apify import runner
from vingo_scraper.config import settings
from vingo_scraper.extract.schema import (
    ExtractionMethod,
    ImageStatus,
    Listing,
    ListingImage,
)
from vingo_scraper.pipeline import polaris


def a_listing(**kw) -> Listing:
    base = {
        "fb_listing_id": "123456789",
        "listing_url": "https://www.facebook.com/marketplace/item/123456789",
        "title": "Clarion QX8006AA car stereo",
        "price_minor": 800000,
        "extraction_method": ExtractionMethod.APIFY,
        "images": [
            ListingImage(
                source_url="https://scontent.xx.fbcdn.net/v/t45/abc.jpg",
                source_key="/v/t45/abc.jpg",
                storage_url="https://storage.googleapis.com/vingo/abc.jpg",
                status=ImageStatus.STORED,
            ),
        ],
    }
    return Listing(**{**base, **kw})


@pytest.fixture(autouse=True)
def _restore():
    before = (settings.polaris_webhook_url, settings.polaris_dry_run,
              settings.polaris_webhook_token)
    yield
    (settings.polaris_webhook_url, settings.polaris_dry_run,
     settings.polaris_webhook_token) = before


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------

def test_01_no_fbcdn_url_is_ever_sent() -> None:
    """Invariant 1, at the one boundary our other tests cannot see.

    Every existing fbcdn assertion checks what we SERVE. A payload leaving for
    someone else's system is exactly where an expiring URL slips out: it works
    in testing and is dead hours later, in a database we do not own.
    """
    payload = polaris.build_payload(a_listing(), location="bangalore")
    assert "fbcdn.net" not in str(payload)
    assert payload["images"] == ["https://storage.googleapis.com/vingo/abc.jpg"]


def test_02_an_unstored_image_is_omitted_not_sent_raw() -> None:
    """One image failing to rehost must not drag its fbcdn URL into the
    payload -- per-image status exists so a bad photo costs that photo."""
    listing = a_listing(images=[
        ListingImage(source_url="https://scontent.xx.fbcdn.net/v/t45/x.jpg",
                     status=ImageStatus.FAILED),
    ])
    payload = polaris.build_payload(listing, location="bangalore")
    assert payload["images"] == []
    assert "fbcdn.net" not in str(payload)


def test_03_price_goes_out_in_minor_units_and_says_so() -> None:
    """`amount_with_offset` was read as paise when it was cents on this
    project -- a 100x error that looked entirely plausible. The key names the
    unit so the receiver cannot repeat it."""
    payload = polaris.build_payload(a_listing(), location="bangalore")
    assert payload["price_minor"] == 800000
    assert payload["currency"] == "INR"
    assert "price" not in payload          # no ambiguous second spelling


def test_04_the_payload_states_that_nothing_was_reviewed() -> None:
    """These rows fire at pending_review. Stated rather than implied, so the
    receiver can decide instead of assuming a human saw them."""
    payload = polaris.build_payload(a_listing(), location="bangalore")
    assert payload["review_status"] == "pending_review"


def test_05_a_listing_with_no_seller_still_builds() -> None:
    payload = polaris.build_payload(a_listing(), location="bangalore")
    assert payload["seller"] is None


# --------------------------------------------------------------------------
# sending, and refusing to
# --------------------------------------------------------------------------

def test_06_an_empty_url_sends_nothing() -> None:
    """Off until configured. There is no live endpoint, and a sweep firing
    forty POSTs at a half-configured URL is worse than firing none."""
    settings.polaris_webhook_url = ""
    sent, failed = asyncio.run(polaris.notify([a_listing()], location="bangalore"))
    assert (sent, failed) == (0, 0)
    assert polaris.enabled() is False


def test_07_dry_run_sends_nothing_even_with_a_url() -> None:
    settings.polaris_webhook_url = "https://polaris.example/hook"
    settings.polaris_dry_run = True

    async def boom(*a, **kw):
        raise AssertionError("dry run must not open a connection")

    import httpx
    real = httpx.AsyncClient.post
    httpx.AsyncClient.post = boom
    try:
        sent, failed = asyncio.run(
            polaris.notify([a_listing()], location="bangalore"))
    finally:
        httpx.AsyncClient.post = real
    assert (sent, failed) == (0, 0)


def test_08_nothing_to_send_is_not_an_error() -> None:
    assert asyncio.run(polaris.notify([], location="bangalore")) == (0, 0)


def test_09_a_4xx_is_not_retried() -> None:
    """Our payload being wrong will not improve on a retry; retrying it just
    multiplies the noise in someone else's logs."""
    src = inspect.getsource(polaris._post_one)
    assert "if resp.status_code < 500:" in src
    assert "return False" in src


# --------------------------------------------------------------------------
# where it fires from
# --------------------------------------------------------------------------

def test_10_only_newly_created_rows_fire() -> None:
    """A re-observed listing is not news. Without this a daily sweep over one
    city re-notifies Polaris about everything it already holds, every day."""
    src = inspect.getsource(runner._persist)
    assert "if was_created:" in src
    assert "fresh.append(listing)" in src


def test_11_the_post_happens_outside_the_database_session() -> None:
    """An HTTP call with a transaction open holds a pooled connection for the
    length of someone else's outage. Across a sweep that exhausts the pool,
    and the symptom shows up nowhere near the cause."""
    src = inspect.getsource(runner._persist)
    assert src.index("async with session_scope()") < src.index("polaris.notify")
    body = src[src.index("polaris.notify"):]
    assert "session" not in body


def test_12_a_webhook_failure_does_not_lose_the_listings() -> None:
    """The listings are already paid for and committed. Nothing Polaris does
    may undo that."""
    src = inspect.getsource(runner._persist)
    after = src[src.index("finalize_scrape_run"):]
    assert "try:" in after
    assert "except Exception" in after
    assert "result.polaris_failed = len(fresh)" in after


def test_13_the_result_counts_both_outcomes() -> None:
    r = runner.IngestResult()
    assert r.polaris_sent == 0 and r.polaris_failed == 0
