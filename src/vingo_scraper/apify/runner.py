"""End-to-end ingest: Apify -> parse -> rehost images -> Supabase.

Ordering here is deliberate and not rearrangeable: images are rehosted *before*
anything is persisted. Facebook CDN URLs expire in hours, so a design that
writes listings first and rehosts later would routinely find dead URLs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from vingo_scraper.apify.client import ApifyRunError, run_actor
from vingo_scraper.extract.fb_payload import parse_payloads
from vingo_scraper.extract.schema import Listing
from vingo_scraper.media.rehost import rehost_all

log = structlog.get_logger(__name__)


@dataclass
class IngestResult:
    listings: list[Listing] = field(default_factory=list)
    items_returned: int = 0
    created: int = 0
    updated: int = 0
    images_stored: int = 0
    images_failed: int = 0
    cost_usd: float | None = None
    run_id: str | None = None
    error: str | None = None
    # Listings already in the database, skipped before any image was fetched.
    skipped_duplicates: int = 0
    quality_flagged: int = 0

    def summary(self) -> str:
        lines = [
            f"  items returned : {self.items_returned}",
            f"  listings parsed: {len(self.listings)}",
            f"  created        : {self.created}",
            f"  updated        : {self.updated}",
            f"  images stored  : {self.images_stored}",
            f"  images failed  : {self.images_failed}",
        ]
        if self.cost_usd is not None:
            per = (
                f" (${self.cost_usd / len(self.listings):.5f}/listing)"
                if self.listings
                else ""
            )
            lines.append(f"  apify cost     : ${self.cost_usd:.4f}{per}")
        if self.error:
            lines.append(f"  error          : {self.error}")
        return "\n".join(lines)


# Optional progress sink: (stage, payload) -> awaitable. Used by the demo
# console to stream the pipeline live; None in CLI/tests.
ProgressHook = Callable[[str, dict], Awaitable[None]]


async def ingest(
    *,
    search_query: str,
    location: str,
    max_items: int = 100,
    include_details: bool = True,
    dry_run: bool = False,
    on_event: ProgressHook | None = None,
) -> IngestResult:
    result = IngestResult()

    async def emit(stage: str, **payload) -> None:
        if on_event is not None:
            await on_event(stage, payload)

    # 1. Trigger the Actor and collect its dataset.
    await emit("apify:start", query=search_query, location=location,
               max_items=max_items)
    try:
        run = await run_actor(
            search_query, location, max_items, include_details=include_details
        )
    except ApifyRunError as exc:
        result.error = str(exc)
        await emit("error", message=str(exc))
        return result

    result.run_id = run.run_id
    result.cost_usd = run.cost_usd
    result.items_returned = len(run.items)

    # 2. Parse. The Actor returns Facebook's own field names, so the existing
    #    tree-walking parser handles dataset items directly.
    result.listings = parse_payloads(run.items, city=location)
    log.info("ingest.parsed", items=result.items_returned,
             listings=len(result.listings))
    await emit("apify:done", items=result.items_returned, run_id=run.run_id,
               cost_usd=run.cost_usd)
    await emit("parse:done", listings=len(result.listings),
               sample=[{"title": x.title,
                        "price": x.price_major,
                        "images": len(x.images)}
                       for x in result.listings[:5]])

    if not result.listings:
        await emit("empty", message="no listings returned")
        return result

    # 3. Rehost images BEFORE persisting. See module docstring.
    if dry_run:
        log.info("ingest.dry_run", note="skipping rehost and persistence")
        await emit("dry_run", message="skipped rehost + persistence")
        return result

    from vingo_scraper.media.storage import get_store

    total_images = sum(len(x.images) for x in result.listings)
    await emit("rehost:start", total=total_images)

    store = get_store()
    result.listings, result.images_stored, result.images_failed = await rehost_all(
        result.listings, store=store
    )
    await emit("rehost:done", stored=result.images_stored,
               failed=result.images_failed, total=total_images)

    # 4. Persist.
    await emit("persist:start", listings=len(result.listings))
    result.created, result.updated = await _persist(result, run)
    await emit("persist:done", created=result.created, updated=result.updated)
    await emit("complete", **{
        "listings": len(result.listings), "created": result.created,
        "updated": result.updated, "images_stored": result.images_stored,
        "images_failed": result.images_failed, "cost_usd": result.cost_usd,
    })
    return result


async def ingest_collected(
    run, *, location: str, trigger: str = "automation"
) -> IngestResult:
    """Parse, rehost and persist a run that has already finished.

    The second half of `ingest()`, callable on its own so a serverless tick can
    start the Actor in one request and do this work in a later one. The Actor's
    runtime varies by 4x between identical runs, which is enough to overrun a
    60s function ceiling when the two are done together.
    """
    from vingo_scraper.extract.description import extract_all
    from vingo_scraper.media.storage import get_store
    from vingo_scraper.pipeline.quality import assess_all

    result = IngestResult()
    result.run_id = run.run_id
    result.cost_usd = run.cost_usd
    result.items_returned = len(run.items)
    result.listings = parse_payloads(run.items, city=location)

    log.info("ingest.parsed", items=result.items_returned,
             listings=len(result.listings))
    if not result.listings:
        return result

    # 1. Structured facts out of freeform description text.
    extract_all(result.listings)

    # 2. Quality gate. Runs BEFORE any image is fetched, which is the point:
    #    a low-effort listing costs no bandwidth and no storage. Nothing is
    #    discarded -- rows land flagged, and the reject rate stays measurable.
    result.listings, quality_summary = assess_all(result.listings)
    result.quality_flagged = quality_summary["flagged"]

    # 3. Duplicates. Checked before rehosting, so a listing we already hold
    #    costs zero image downloads on a re-scrape -- only `last_seen_at`
    #    moves. This does NOT save Apify spend: the Actor charges when it
    #    scrapes, long before any id reaches us. `onlyNewListings` on the
    #    Actor is what avoids the charge; this avoids the work.
    fresh, known = await _split_known(result.listings)
    result.skipped_duplicates = len(known)
    if known:
        await _touch_last_seen(known)
        log.info("ingest.duplicates_skipped", count=len(known),
                 saved_images=sum(len(x.images) for x in known))

    if not fresh:
        result.listings = []
        return result

    # 4. Rehost BEFORE persisting -- see the module docstring. Facebook's URLs
    #    expire, so a later batch job would find them already dead.
    result.listings = fresh
    result.listings, result.images_stored, result.images_failed = await rehost_all(
        result.listings, store=get_store()
    )
    result.created, result.updated = await _persist(result, run, trigger=trigger)
    return result


async def _split_known(listings) -> tuple[list, list]:
    """(not yet in the database, already there). One query, not N."""
    from sqlalchemy import select

    from vingo_scraper.db import models
    from vingo_scraper.db.session import session_scope

    ids = [x.fb_listing_id for x in listings if x.fb_listing_id]
    if not ids:
        return list(listings), []
    try:
        async with session_scope() as s:
            rows = await s.scalars(
                select(models.Listing.fb_listing_id).where(
                    models.Listing.fb_listing_id.in_(ids)
                )
            )
            known_ids = set(rows.all())
    except Exception as exc:
        # A dedup check that cannot run must not drop listings on the floor.
        # Treat everything as fresh: re-ingesting is wasteful, losing data is
        # worse, and the upsert is idempotent anyway.
        log.warning("ingest.dedup_check_failed", error=str(exc)[:160])
        return list(listings), []

    fresh = [x for x in listings if x.fb_listing_id not in known_ids]
    known = [x for x in listings if x.fb_listing_id in known_ids]
    return fresh, known


async def _touch_last_seen(listings) -> None:
    """Mark known listings as seen again, without touching anything else.

    Deliberately narrow: `review_status`, `first_seen_at` and images stay as
    they are. Re-observing a listing is not a reason to undo a human's review
    decision or re-date its discovery.
    """
    from datetime import UTC, datetime

    from sqlalchemy import update

    from vingo_scraper.db import models
    from vingo_scraper.db.session import session_scope

    ids = [x.fb_listing_id for x in listings]
    try:
        async with session_scope() as s:
            await s.execute(
                update(models.Listing)
                .where(models.Listing.fb_listing_id.in_(ids))
                .values(last_seen_at=datetime.now(UTC))
            )
    except Exception as exc:
        log.warning("ingest.touch_failed", error=str(exc)[:160])


async def _persist(result: IngestResult, run, *, trigger: str = "manual") -> tuple[int, int]:
    from vingo_scraper.db import repository as repo
    from vingo_scraper.db.session import session_scope

    created = updated = 0
    async with session_scope() as session:
        run_row = await repo.create_scrape_run(
            session,
            actor_id=run.actor_id,
            run_input=run.run_input,
            run_id=run.run_id,
            status=run.status,
            started_at=run.started_at,
            trigger=trigger,
        )

        for listing in result.listings:
            row, was_created = await repo.upsert_listing(
                session, listing, scrape_run_id=run_row.id
            )
            created += was_created
            updated += not was_created

            # Batched: one SELECT per listing rather than two round trips per
            # image. Matters because the pooler is in another region.
            await repo.upsert_images(session, row, listing.images)

        await repo.finalize_scrape_run(
            session,
            run_row,
            items_returned=result.items_returned,
            items_ingested=len(result.listings),
            images_stored=result.images_stored,
            images_failed=result.images_failed,
            cost_usd=result.cost_usd,
            finished_at=run.finished_at,
        )

    return created, updated


def run_ingest(
    *,
    search_query: str,
    location: str,
    max_items: int = 100,
    include_details: bool = True,
    dry_run: bool = False,
) -> int:
    """Sync entry point for the CLI. Returns a process exit code."""
    # Must happen before asyncio.run() -- see the function's docstring.
    from vingo_scraper.db.session import ensure_compatible_event_loop

    ensure_compatible_event_loop()

    result = asyncio.run(
        ingest(
            search_query=search_query,
            location=location,
            max_items=max_items,
            include_details=include_details,
            dry_run=dry_run,
        )
    )

    print()
    print(result.summary())
    print()

    if result.error:
        return 2
    if not result.listings:
        # Distinguish "ran fine, nothing matched" from "failed" -- these lead
        # to opposite decisions about whether Marketplace has inventory here.
        print("  No listings returned. This is a density signal, not a failure.")
        return 3
    if result.images_failed and not result.images_stored:
        print("  WARNING: every image failed to rehost -- listings are not servable.")
        return 4
    return 0
