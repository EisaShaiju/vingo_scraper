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

from vingo_scraper.apify.client import (
    LISTINGS_PER_PAGE,
    ApifyRunError,
    run_actor,
)
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
    # Dropped by the quality gate before any image was fetched. Kept as a
    # count because the rows themselves are discarded -- without this the
    # reject rate would be invisible and a wrong threshold would survive.
    rejected: int = 0
    rejected_reasons: dict = field(default_factory=dict)
    # Dropped for being older than the caller asked for. Separate from
    # `rejected` because stale is not the same failure as low quality.
    stale_dropped: int = 0
    overpriced: int = 0
    # What we PAID for, so a short result can be explained instead of guessed
    # at. Derived from the run input, which is the only durable record of it.
    requested: int = 0
    # Dropped because only the DESCRIPTION matched the search, not the title.
    off_topic: int = 0
    off_topic_examples: list = field(default_factory=list)
    # True when the Actor returned nothing because everything was already in
    # its cache -- a successful no-op, not an empty search.
    all_cached: bool = False

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


def _drop_stale(listings, max_age_hours: int) -> tuple[list, int]:
    """Keep only listings posted within `max_age_hours`.

    A listing with no `posted_at` is KEPT. Unknown age is not the same as old,
    and discarding on missing data would silently throw away every listing
    whose timestamp we failed to parse.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    fresh, dropped = [], 0
    for item in listings:
        posted = getattr(item, "posted_at", None)
        if posted is not None and posted < cutoff:
            dropped += 1
        else:
            fresh.append(item)
    if dropped:
        log.info("ingest.stale_dropped", count=dropped, max_age_hours=max_age_hours)
    return fresh, dropped


def _drop_overpriced(listings, min_minor: int | None, max_minor: int | None):
    """Enforce the price band on what actually came back.

    This exists because the Actor silently did not. Its schema splits input
    into two modes -- Filters, or `urls` you supply -- and in URL mode the
    price fields are discarded. A run capped at Rs 50,000 returned a
    Rs 9,00,000 Volvo and a Rs 2,49,999 Mahindra, and nothing anywhere in this
    codebase compared a listing's price to the cap. Recency had a backstop;
    price never did.

    A listing with NO readable price is KEPT, for the same reason `_drop_stale`
    keeps an undated one: unknown is not the same as out of range, and
    discarding on missing data throws away listings whose price we merely
    failed to parse.
    """
    if min_minor is None and max_minor is None:
        return listings, 0

    kept, dropped = [], 0
    for item in listings:
        price = getattr(item, "price_minor", None)
        if price is None:
            kept.append(item)
            continue
        too_cheap = min_minor is not None and price < min_minor
        too_dear = max_minor is not None and price > max_minor
        if too_cheap or too_dear:
            dropped += 1
        else:
            kept.append(item)
    if dropped:
        log.info("ingest.overpriced_dropped", count=dropped,
                 min_minor=min_minor, max_minor=max_minor)
    return kept, dropped


async def ingest_collected(
    run, *, location: str, trigger: str = "automation",
    max_age_hours: int | None = None, search_term: str | None = None,
    min_price: int | None = None, max_price: int | None = None,
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
    from vingo_scraper.pipeline.relevance import filter_relevant

    result = IngestResult()
    result.run_id = run.run_id
    result.cost_usd = run.cost_usd
    result.items_returned = len(run.items)
    # The Actor reports its own failures as dataset ITEMS carrying an `error`
    # key rather than by failing the run. Left unhandled they count as
    # listings the parser mysteriously dropped -- the console showed
    # "1 listings, 0 new" for what was actually an error message.
    actor_errors = [
        # Not clipped. `ScrapeRun.error` is a Text column, and the only other
        # copy -- `config.manual_status` -- is varchar(64), which cut the
        # Actor's own warning off mid-word: "Number of records scraped per run
        # by this actor is li…". A warning you cannot read is not a warning.
        str(x.get("error")) for x in run.items
        if isinstance(x, dict) and x.get("error") and not x.get("id")
    ]
    if actor_errors:
        result.error = actor_errors[0]
        result.items_returned = len(run.items) - len(actor_errors)
        log.error("ingest.actor_error", error=actor_errors[0])

    result.listings = parse_payloads(run.items, city=location)

    log.info("ingest.parsed", items=result.items_returned,
             listings=len(result.listings), actor_errors=len(actor_errors))
    if not result.listings:
        # Zero listings from a cached run means "nothing new since last time",
        # which is the cache working. Zero from an uncached run means the
        # search genuinely found nothing. Collapsing the two makes every
        # correct re-run look like a failure.
        if (run.run_input or {}).get("onlyNewListings"):
            result.error = None
            result.all_cached = True
        # Record the run even with nothing to show for it. `scrape_run` is the
        # audit trail, and "we searched and Marketplace had none" is exactly
        # the result worth being able to look up later -- it is also what the
        # console's history reads, so without this an empty run leaves the
        # operator wondering whether anything happened at all.
        await _persist(result, run, trigger=trigger)
        return result

    # 1. Structured facts out of freeform description text.
    extract_all(result.listings)

    # 2. Quality gate. Runs BEFORE any image is fetched, which is the point:
    #    a low-effort listing costs no bandwidth and no storage. Nothing is
    #    discarded -- rows land flagged, and the reject rate stays measurable.
    # Description and the full photo set both arrive only with details. A
    # cheap run yields one image and an empty description on every listing, so
    # the gate has to be told what was fetched or it scores our cost setting.
    details_known = bool((run.run_input or {}).get("getListingDetails"))

    # Pages x page size is what we bought. `maxPagesPerUrl` is the curious_coder
    # knob; `resultsLimit` belongs to the legacy Actor.
    _input = run.run_input or {}
    if _input.get("maxPagesPerUrl"):
        result.requested = int(_input["maxPagesPerUrl"]) * LISTINGS_PER_PAGE
    elif _input.get("resultsLimit"):
        result.requested = int(_input["resultsLimit"])
    result.listings, quality_summary = assess_all(
        result.listings, details_known=details_known
    )
    result.rejected = quality_summary["rejected"]
    result.rejected_reasons = quality_summary["rejected_reasons"]
    result.quality_flagged = result.rejected  # kept for the older API shape

    # 2a. Relevance. Facebook matches DESCRIPTION text, so a search for
    #     "cycles" returns MacBooks whose listing says "battery cycle count".
    #     Before rehosting, so a dropped listing costs no bandwidth.
    if search_term:
        result.listings, rel = filter_relevant(result.listings, search_term)
        result.off_topic = rel.dropped
        result.off_topic_examples = rel.examples

    # 2a-ii. Price. The Actor accepts minPrice/maxPrice and then ignores them
    #     whenever we drive it with `urls`, so the cap is enforced here too.
    #     Prices arrive in rupees from the form and are held in paise.
    result.listings, result.overpriced = _drop_overpriced(
        result.listings,
        None if min_price is None else min_price * 100,
        None if max_price is None else max_price * 100,
    )

    # 2b. Freshness. The Actor's finest window is one day, so anything tighter
    #     is applied here -- it narrows what we KEEP, never what we paid for.
    if max_age_hours:
        result.listings, result.stale_dropped = _drop_stale(
            result.listings, max_age_hours
        )

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
            # Never written until now, so every Actor warning was lost the
            # moment the 64-char status was overwritten.
            error=result.error,
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
