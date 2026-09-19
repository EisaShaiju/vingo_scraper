"""Persistence: upsert listings, sellers, images, and run records.

Everything here must be idempotent. Re-running the same search is the normal
case, not an edge case -- catalogs are refreshed, not built once.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from vingo_scraper.db import models
from vingo_scraper.extract import schema

log = structlog.get_logger(__name__)


async def upsert_seller(
    session: AsyncSession, seller: schema.Seller | None
) -> models.Seller | None:
    """Insert or fetch a seller by its Facebook id."""
    if seller is None or not seller.fb_seller_id:
        return None

    stmt = (
        insert(models.Seller)
        .values(
            fb_seller_id=seller.fb_seller_id,
            name=seller.name,
            profile_url=seller.profile_url,
        )
        # Names change; the id does not. Refresh the name so the display value
        # doesn't go stale, but never create a second row.
        .on_conflict_do_update(
            index_elements=["fb_seller_id"],
            set_={"name": seller.name},
        )
        .returning(models.Seller.id)
    )
    seller_id = (await session.execute(stmt)).scalar_one()
    return await session.get(models.Seller, seller_id)


async def upsert_listing(
    session: AsyncSession,
    listing: schema.Listing,
    *,
    scrape_run_id=None,
) -> tuple[models.Listing, bool]:
    """Insert or update one listing. Returns (row, was_created).

    `first_seen_at` and `review_status` are deliberately NOT overwritten on
    conflict: re-observing a listing must not reset a human's review decision
    back to pending, or we would re-queue work that was already done.
    """
    seller_row = await upsert_seller(session, listing.seller)
    now = datetime.now(UTC)

    values = {
        "fb_listing_id": listing.fb_listing_id,
        "listing_url": listing.listing_url,
        "title": listing.title,
        "description": listing.description,
        "price_minor": listing.price_minor,
        "currency": listing.currency,
        "condition": listing.condition,
        "city": listing.city,
        "location_text": listing.location_text,
        "category": listing.category,
        "seller_id": seller_row.id if seller_row else None,
        "scrape_run_id": scrape_run_id,
        "posted_at": listing.posted_at,
        "extraction_method": listing.extraction_method,
        "first_seen_at": now,
        "last_seen_at": now,
    }

    existing = await session.scalar(
        select(models.Listing).where(
            models.Listing.fb_listing_id == listing.fb_listing_id
        )
    )

    stmt = (
        insert(models.Listing)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["fb_listing_id"],
            set_={
                k: values[k]
                for k in (
                    "listing_url", "title", "description", "price_minor",
                    "condition", "location_text", "category", "seller_id",
                    "scrape_run_id", "posted_at", "last_seen_at",
                )
            },
        )
        .returning(models.Listing.id)
    )
    listing_id = (await session.execute(stmt)).scalar_one()
    await session.flush()

    row = await session.get(models.Listing, listing_id)
    return row, existing is None


async def upsert_images(
    session: AsyncSession,
    listing_row: models.Listing,
    images: list[schema.ListingImage],
) -> int:
    """Upsert every image for one listing in a bounded number of round trips.

    The per-image version issued a SELECT and an INSERT each, which against a
    pooler in another region turned ~60 images into ~120 sequential round trips
    and roughly a minute of wall clock. One SELECT for the whole listing plus
    in-memory matching keeps it at two.
    """
    if not images:
        return 0

    existing_rows = (await session.scalars(
        select(models.ListingImage).where(
            models.ListingImage.listing_id == listing_row.id
        )
    )).all()

    by_key = {r.source_key: r for r in existing_rows if r.source_key}
    by_sha = {r.sha256: r for r in existing_rows if r.sha256}

    touched = 0
    for image in images:
        key = image.source_key or schema.stable_source_key(image.source_url)
        row = by_key.get(key) or (by_sha.get(image.sha256) if image.sha256 else None)

        if row is None:
            row = models.ListingImage(listing_id=listing_row.id, source_key=key)
            session.add(row)
            by_key[key] = row

        row.source_key = key
        row.source_url = image.source_url  # refresh the expiring url
        row.storage_url = image.storage_url or row.storage_url
        row.status = image.status
        row.sha256 = image.sha256 or row.sha256
        row.content_type = image.content_type or row.content_type
        row.size_bytes = image.size_bytes or row.size_bytes
        row.is_primary = image.is_primary
        row.error = image.error
        touched += 1

    return touched


async def upsert_image(
    session: AsyncSession,
    listing_row: models.Listing,
    image: schema.ListingImage,
) -> models.ListingImage:
    """Insert or update one image row.

    Keyed on (listing_id, source_key) -- the fbcdn URL *path*, which is stable
    per photo. Deliberately NOT keyed on sha256: Facebook serves different
    derivatives (thumbnail vs full size) of the same photo across runs, so the
    bytes and their hash change while the photo does not. Keying on the hash
    made every re-ingest insert duplicates.
    """
    key = image.source_key or schema.stable_source_key(image.source_url)

    existing = await session.scalar(
        select(models.ListingImage).where(
            models.ListingImage.listing_id == listing_row.id,
            models.ListingImage.source_key == key,
        )
    )
    if existing is None and image.sha256:
        existing = await session.scalar(
            select(models.ListingImage).where(
                models.ListingImage.listing_id == listing_row.id,
                models.ListingImage.sha256 == image.sha256,
            )
        )

    if existing is not None:
        existing.source_key = key
        existing.source_url = image.source_url  # refresh the (expiring) url
        existing.storage_url = image.storage_url or existing.storage_url
        existing.status = image.status
        existing.sha256 = image.sha256 or existing.sha256
        existing.content_type = image.content_type or existing.content_type
        existing.size_bytes = image.size_bytes or existing.size_bytes
        existing.is_primary = image.is_primary
        existing.error = image.error
        return existing

    row = models.ListingImage(
        listing_id=listing_row.id,
        source_url=image.source_url,
        source_key=key,
        storage_url=image.storage_url,
        status=image.status,
        is_primary=image.is_primary,
        sha256=image.sha256,
        content_type=image.content_type,
        size_bytes=image.size_bytes,
        error=image.error,
    )
    session.add(row)
    return row


async def create_scrape_run(
    session: AsyncSession, *, actor_id: str, run_input: dict,
    run_id: str | None = None, status: str = "RUNNING",
    started_at: datetime | None = None,
) -> models.ScrapeRun:
    row = models.ScrapeRun(
        actor_id=actor_id, run_id=run_id, run_input=run_input,
        status=status, started_at=started_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def finalize_scrape_run(
    session: AsyncSession, run_row: models.ScrapeRun, **fields
) -> models.ScrapeRun:
    for key, value in fields.items():
        if hasattr(run_row, key):
            setattr(run_row, key, value)
    run_row.finished_at = run_row.finished_at or datetime.now(UTC)
    return run_row


async def find_stored_image(
    session: AsyncSession, sha256: str
) -> models.ListingImage | None:
    """Any already-stored row with these exact bytes.

    Lets the media pipeline skip a download+upload entirely when the same photo
    has been seen before -- reposts are common on Marketplace.
    """
    return await session.scalar(
        select(models.ListingImage).where(
            models.ListingImage.sha256 == sha256,
            models.ListingImage.status == schema.ImageStatus.STORED,
        ).limit(1)
    )
