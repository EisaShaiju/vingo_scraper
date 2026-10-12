"""Push newly stored listings to Polaris.

The console is where listings are found; Polaris is where they become Vingo
listings. This is the handoff: one POST per newly created row, carrying enough
for Polaris to build the listing without calling back.

Four things this module refuses to do, each because the alternative has already
cost something somewhere:

**It never sends an `fbcdn.net` URL.** Facebook's signed image URLs expire in
hours, so a payload carrying one hands Polaris a picture that works in testing
and is dead by the time anyone looks. Only `storage_url` goes out, and
`build_payload` drops images that do not have one yet.

**It never fails ingestion.** By the time this runs the listing is scraped,
paid for, rehosted and committed. Polaris being down is not a reason to lose
any of that, so every failure is caught, counted and logged rather than raised.

**It only fires for rows we just created.** A re-observed listing is not news.
Without that check a daily sweep over the same city would re-notify Polaris
about every listing it already holds, every day.

**It is off until configured.** An empty `polaris_webhook_url` disables it;
`polaris_dry_run` logs the exact payload without sending. There is no live
endpoint yet, and a misconfigured sweep firing 46 unexpected POSTs at whatever
the URL happens to point at is worse than firing none.

NOTE: rows are sent at `pending_review`, which is a deliberate departure from
the project's "nothing auto-publishes" invariant -- Polaris becomes the review
surface instead. See `review_status` in the payload: it is included precisely
so the receiver can tell, rather than having to assume.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from vingo_scraper.config import settings

log = structlog.get_logger(__name__)

TIMEOUT_S = 10.0
# Enough to ride out a restart, short enough that a sweep is not held up by a
# receiver that is simply down. The listing is already stored either way.
MAX_ATTEMPTS = 3


def enabled() -> bool:
    return bool(settings.polaris_webhook_url)


def build_payload(listing: Any, *, location: str) -> dict:
    """What Polaris needs to build the listing, and nothing it cannot use.

    `storage_url` only. A payload is a place an `fbcdn.net` URL can leak into
    production without any of our own tests noticing, because ours all check
    what we SERVE.
    """
    images = [
        img.storage_url for img in (getattr(listing, "images", None) or [])
        if getattr(img, "storage_url", None)
    ]
    seller = getattr(listing, "seller", None)
    price_minor = getattr(listing, "price_minor", None)

    return {
        "source": "facebook_marketplace",
        "fb_listing_id": listing.fb_listing_id,
        "listing_url": listing.listing_url,
        "title": listing.title,
        "description": getattr(listing, "description", None),
        # Minor units, as held. Sending rupees would invite the reader to guess
        # which it is -- and `amount_with_offset` has already been misread once
        # at 100x on this project.
        "price_minor": price_minor,
        "currency": getattr(listing, "currency", "INR"),
        "category": getattr(listing, "category", None),
        "location": location,
        "location_text": getattr(listing, "location_text", None),
        "posted_at": (
            listing.posted_at.isoformat()
            if getattr(listing, "posted_at", None) else None
        ),
        "quality_score": getattr(listing, "quality_score", None),
        "images": images,
        "seller": None if seller is None else {
            "fb_seller_id": getattr(seller, "fb_seller_id", None),
            "name": getattr(seller, "name", None),
            "profile_url": getattr(seller, "profile_url", None),
        },
        # Stated, not implied: these rows have NOT been reviewed by anyone.
        "review_status": "pending_review",
    }


async def _post_one(client: httpx.AsyncClient, payload: dict) -> bool:
    headers = {"Content-Type": "application/json"}
    if settings.polaris_webhook_token:
        headers["Authorization"] = f"Bearer {settings.polaris_webhook_token}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client.post(
                settings.polaris_webhook_url, json=payload,
                headers=headers, timeout=TIMEOUT_S,
            )
            if resp.status_code < 300:
                return True
            # 4xx is our payload being wrong and will not improve on a retry;
            # 5xx might. Retrying a rejected payload just multiplies the noise
            # in someone else's logs.
            if resp.status_code < 500:
                log.warning("polaris.rejected",
                            fb_listing_id=payload.get("fb_listing_id"),
                            status=resp.status_code, body=resp.text[:200])
                return False
            log.warning("polaris.server_error", status=resp.status_code,
                        attempt=attempt)
        except Exception as exc:                    # noqa: BLE001
            log.warning("polaris.post_failed", attempt=attempt,
                        error=f"{type(exc).__name__}: {exc}"[:160])
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2 ** (attempt - 1))
    return False


async def notify(listings: list, *, location: str) -> tuple[int, int]:
    """POST one payload per listing. Returns (sent, failed).

    Call this AFTER the database session has closed. An HTTP request made with
    a transaction still open holds a connection for the length of someone
    else's outage, and a sweep doing that across hundreds of rows exhausts the
    pool.
    """
    if not listings:
        return (0, 0)

    payloads = [build_payload(x, location=location) for x in listings]

    if not enabled():
        log.info("polaris.disabled", skipped=len(payloads))
        return (0, 0)

    if settings.polaris_dry_run:
        # The deliverable until there is a real endpoint: the exact shape, in
        # the logs, agreed with whoever is building the receiver.
        for p in payloads:
            log.info("polaris.dry_run", payload=p)
        return (0, 0)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_post_one(client, p) for p in payloads),
            return_exceptions=True,
        )

    sent = sum(1 for r in results if r is True)
    failed = len(results) - sent
    log.info("polaris.notified", sent=sent, failed=failed)
    return (sent, failed)
