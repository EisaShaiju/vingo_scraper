"""Ingestion console and API.

**One runner: manual.** You type a category or a keyword, it scrapes, the
catalog fills. Automation existed alongside this and was removed -- it shared
the lock, the budget and the screen while nobody was using it, and a
half-finished second runner costs more attention than it returns. The git
history has it when it is wanted again.

A run is two requests, never one long one: `start_run` commits the money and
returns, `collect_run` picks the dataset up afterwards. The Actor takes
10.8-47.1s for identical work, so waiting inline means a timeout kills the
request *after* Apify has charged.

The ingestion lock stays. It is now preventing one operator from
double-clicking rather than two runners colliding, which is a smaller problem
but the same money.

Everything the page shows is read back out of the database, not from an
in-process object -- in-memory state does not error under concurrency, it
quietly disagrees with itself.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import false as sa_false
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from vingo_scraper.config import settings
from vingo_scraper.db import models
from vingo_scraper.db.session import session_scope
from vingo_scraper.extract.schema import ImageStatus, ReviewStatus
from vingo_scraper.pipeline import lead_score

log = structlog.get_logger(__name__)

STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    # Nothing to tear down: automation state lives in `system_config`, and
    # ticks are driven by HTTP requests rather than a background loop. An
    # in-process loop was the original design and it does not survive
    # serverless -- see docs/automation.md.


app = FastAPI(
    title="Comet Ingestion Console", docs_url="/api/docs", lifespan=_lifespan
)


class DemoStartRequest(BaseModel):
    runs: int = 3
    interval_seconds: int = 180
    search_query: str | None = None
    location: str | None = None
    max_pages: int = 1


class AutomationRequest(BaseModel):
    interval_s: int = 180
    per_cell: int = 10
    # Defaults ON. Measured on the VM: without details the Actor returns no
    # description at all and a single photo, and the spec requires both. The
    # cheap run costs $0.0005/listing against $0.0015, but it cannot produce
    # the deliverable -- so the saving is not a saving.
    include_details: bool = True
    # 5 = one full sweep of the Bengaluru category grid.
    runs: int = 5
    city: str | None = None
    # Deliberately open on category 1 rather than continuing the rotation.
    restart: bool = False


class IngestRequest(BaseModel):
    # No default search term. A leftover "iphone" here meant a request that
    # omitted `query` silently bought a page of iPhones -- and with a category
    # selected it would also have become the term the title filter enforced,
    # dropping every genuine result that did not say "iphone". The endpoint
    # already refuses a request with neither a category nor a keyword.
    query: str = ""
    # Bengaluru first, per the client spec. This defaulted to Mumbai long
    # after that decision was made.
    location: str = "bangalore"
    max_items: int = 10
    dry_run: bool = False
    # Optional category; when set it supplies the search term, so the manual
    # panel and the automation grid can run the identical query.
    category: str | None = None
    # ON for the same reason as automation: no description and one photo
    # without it, and the spec asks for both.
    include_details: bool = True
    # Marketplace filters. Validated against the Actor's real schema in
    # `client.merge_filters` -- unknown keys are dropped rather than sent,
    # because Apify fails the whole run on an unrecognised input field.
    min_price: int | None = None
    max_price: int | None = None
    sort_by: str | None = None
    days_since_listed: str | None = None
    # Finer than the Actor's one-day floor. Applied AFTER fetching, so it
    # narrows what we keep rather than what we pay for -- said plainly in the
    # UI, because a filter that looks like it saves money and does not is
    # worse than no filter.
    max_age_hours: int | None = None
    # Keep only listings whose TITLE matches the keyword. On by default:
    # Facebook matches description text, so "cycles" returns MacBooks saying
    # "battery cycle count". Off gives you Facebook's raw relevance.
    title_match: bool = True
    # Ask Facebook to be stricter too. Costs nothing and happens upstream.
    strict: bool = True

    def to_filters(self) -> dict:
        return {
            "minPrice": self.min_price,
            "maxPrice": self.max_price,
            "sortBy": self.sort_by,
            "daysSinceListed": self.days_since_listed,
            "strictFiltering": self.strict or None,
        }


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# --------------------------------------------------------------------------
# reads -- all straight from Supabase
# --------------------------------------------------------------------------

@app.get("/api/stats")
async def stats() -> dict:
    try:
        return await _stats()
    except Exception as exc:
        log.warning("stats.db_unavailable", error=str(exc)[:160])
        return {
            "listings": 0, "images": 0, "images_stored": 0, "runs": 0,
            "pending_review": 0, "cost_usd": 0.0, "fbcdn_leaks": 0,
            "last_run_at": None, "db_available": False,
            "db_error": _friendly_db_error(exc),
            "supabase_project": "comet (local)",
            "bucket": settings.gcs_bucket,
        }


def _friendly_db_error(exc: Exception) -> str:
    """Turn a psycopg wall-of-text into something actionable."""
    msg = str(exc)
    if "getaddrinfo failed" in msg or "ENOTFOUND" in msg or "not found" in msg:
        return "Postgres unreachable - check VINGO_DATABASE_URL in /etc/comet.env"
    if "password authentication" in msg:
        return "Database password rejected"
    if "prepared statement" in msg:
        return "Pooler prepared-statement error - set prepare_threshold=None"
    return f"{type(exc).__name__}: {msg[:120]}"


async def _stats() -> dict:
    async with session_scope() as s:
        listings = await s.scalar(select(func.count(models.Listing.id)))
        images = await s.scalar(select(func.count(models.ListingImage.id)))
        stored = await s.scalar(
            select(func.count(models.ListingImage.id)).where(
                models.ListingImage.status == ImageStatus.STORED
            )
        )
        runs = await s.scalar(select(func.count(models.ScrapeRun.id)))
        pending = await s.scalar(
            select(func.count(models.Listing.id)).where(
                models.Listing.review_status == ReviewStatus.PENDING_REVIEW
            )
        )
        cost = await s.scalar(select(func.coalesce(func.sum(models.ScrapeRun.cost_usd), 0.0)))
        last = await s.scalar(
            select(models.ScrapeRun).order_by(models.ScrapeRun.started_at.desc()).limit(1)
        )
        # The invariant, checked live so the demo can show it holding.
        leaked = await s.scalar(
            select(func.count(models.ListingImage.id)).where(
                models.ListingImage.storage_url.like("%fbcdn%")
            )
        )

    return {
        "listings": listings or 0,
        "images": images or 0,
        "images_stored": stored or 0,
        "runs": runs or 0,
        "pending_review": pending or 0,
        "cost_usd": round(cost or 0.0, 4),
        "fbcdn_leaks": leaked or 0,
        "last_run_at": last.started_at.isoformat() if last else None,
        "supabase_project": "comet (local)",
        "bucket": settings.gcs_bucket,
        "db_available": True,
        "db_error": None,
    }


# --------------------------------------------------------------------------
# outreach: addressing the person, and what to say
# --------------------------------------------------------------------------
# Both of these live here rather than in the browser so one definition is
# shared by the UI, the CSV and the tests. The UI used to be the only place a
# contact route existed, which is how it kept drifting from what we hold.

MESSENGER_THREAD = "https://www.facebook.com/messages/t/{}"


def messenger_url(fb_seller_id: str | None) -> str | None:
    """The Messenger conversation with that seller, or None if we cannot build
    one.

    **`pfbid` tokens DO resolve under /messages/t/.** Verified 2026-10-14 in a
    logged-in browser: the link opens the real thread, headed with the seller's
    name and "You aren't friends on Facebook".

    This function used to require `isdigit()`, on my assumption that only a
    numeric user id addresses a thread. Every seller id the Actor actually
    returns is a `pfbid` -- 0 of 3 numeric in the frozen real fixture -- so
    that check returned None for **every live row** and the console fell back
    to the listing page 100% of the time while the docs claimed 91% coverage.
    The numeric ids I tested against came from a hand-written fixture.

    The lesson, which is why this paragraph is long: a validation rule invented
    without checking real data is indistinguishable from a working feature,
    because the fallback it triggers looks deliberate.

    Still rejects anything that is neither -- a URL built from a name or an
    empty string lands nowhere, and the caller needs to be able to say so.
    """
    seller_id = (fb_seller_id or "").strip()
    if not seller_id:
        return None
    if not (seller_id.isdigit() or seller_id.startswith("pfbid")):
        return None
    return MESSENGER_THREAD.format(seller_id)


def draft_message(title: str, price_inr: float | None,
                  seller_name: str | None = None) -> str:
    """The opener, built from this listing's own facts.

    It opens by saying who is writing and what Vingo is. Without that, a
    stranger reading it has no reason to answer -- the first version led with
    "Saw your <item>", which reads like a haggling buyer, not an approach.

    The ask for a number is IN the message because that is the one thing we
    cannot scrape -- Facebook publishes phone on 0% of listings, so the seller
    volunteering it is the only route to one. WhatsApp is named explicitly
    because in India that is usually the channel people expect.

    Three paragraphs separated by blank lines. Both Messenger and the
    Marketplace composer SEND on Enter, so this has to survive being pasted as
    one block rather than being typed line by line.

    Copied, never sent: automated sending needs a logged-in account driven by
    code, which breaks Facebook's terms and realistically ends in a ban that
    costs the scraping too.
    """
    greeting = f"Hi {seller_name.split()[0]}!" if seller_name else "Hi!"
    item = f'"{title}"' if title else "your listing"
    priced = f" listed at ₹{int(price_inr):,}" if price_inr else ""
    return (
        f"{greeting} We're from team Vingo -- we list your item for you and "
        "bring you serious buyers, free.\n\n"
        f"We saw your {item}{priced} on Marketplace. Would you like us to put "
        "it on Vingo as well?\n\n"
        "If you're interested, reply with your phone or WhatsApp number and "
        "we'll set it up for you."
    )


@app.get("/api/listings")
async def listings(
    limit: int = 60,
    offset: int = 0,
    since_run: str | None = None,
    search: str | None = None,
    outreach: str | None = None,
    trigger: str | None = None,
    has_contact: bool = False,
    since_ts: str | None = None,
    order: str = "recent",
) -> dict:
    """The catalog, the results of one run, or the lead queue.

    `order=lead` sorts by the computed lead score. That sort happens in Python
    rather than SQL because the score is not stored -- see
    `pipeline.lead_score` for why. It therefore scores the `limit` rows the
    query returns, so a lead queue wants a generous limit.

    `since_ts` scopes to runs that started at or after it -- what the Bulk tab
    uses to show one sweep rather than every sweep ever run.
    """
    try:
        return await _listings(limit, since_run, offset=offset, search=search,
                               outreach=outreach, trigger=trigger,
                               has_contact=has_contact, since_ts=since_ts,
                               order=order)
    except Exception as exc:
        # Keep the console up when Postgres is genuinely down -- but SAY SO.
        # Returning a bare empty list made a crashed query indistinguishable
        # from an empty catalog, and twice sent someone hunting for a scraper
        # bug while 237 listings sat in the database.
        log.warning("listings.db_unavailable", error=str(exc)[:160])
        return {"listings": [], "count": 0, "db_available": False,
                "error": type(exc).__name__ + ": " + str(exc)[:200]}


async def _listings(limit: int, since_run: str | None, *, offset: int = 0,
                    search: str | None = None, outreach: str | None = None,
                    trigger: str | None = None, has_contact: bool = False,
                    since_ts: str | None = None,
                    order: str = "recent") -> dict:
    async with session_scope() as s:
        q = select(models.Listing)
        if since_run:
            run = await s.scalar(
                select(models.ScrapeRun).where(models.ScrapeRun.run_id == since_run)
            )
            if run:
                q = q.where(models.Listing.scrape_run_id == run.id)
        if since_ts:
            # A sweep is MANY runs -- one `ScrapeRun` per cell -- so `since_run`
            # above, which takes a single run_id, cannot scope one. Without this
            # the Bulk tab showed every bulk row ever ingested and successive
            # sweeps piled into one view.
            #
            # A bad timestamp must narrow to nothing rather than silently
            # widening to everything: an unfiltered view that looks filtered is
            # how the last sweep's rows got read as this one's.
            try:
                cutoff = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))
            except ValueError:
                cutoff = None
            q = q.where(
                models.Listing.scrape_run_id.in_(
                    select(models.ScrapeRun.id).where(
                        models.ScrapeRun.started_at >= cutoff
                    )
                ) if cutoff else sa_false()
            )
        if search:
            # Server-side so the lead queue can be filtered across everything
            # held, not just the page the browser happens to have.
            q = q.where(models.Listing.title.ilike(f"%{search}%"))
        if outreach:
            q = q.where(models.Listing.outreach_status == outreach)
        if has_contact:
            # "Sellers who gave us a number" -- the only segment that matters
            # once outreach is running. Contact lives on the Seller, not the
            # listing, so one captured number qualifies every listing by that
            # person.
            q = q.where(
                models.Listing.seller_id.in_(
                    select(models.Seller.id).where(
                        or_(models.Seller.phone.isnot(None),
                            models.Seller.email.isnot(None))
                    )
                )
            )
        if trigger:
            # Bulk sweeps are tagged on their ScrapeRun, so the Bulk tab can
            # show only its own catalog while Search and Catalog keep showing
            # exactly what they always did.
            q = q.where(
                models.Listing.scrape_run_id.in_(
                    select(models.ScrapeRun.id)
                    .where(models.ScrapeRun.trigger == trigger)
                )
            )

        # Scoring needs a pool to score. Ordering by lead score in SQL is not
        # possible without storing it, so take a wider slice and sort after.
        want = max(limit, 300) if order == "lead" else limit
        q = q.order_by(models.Listing.last_seen_at.desc())
        # Eager-load price history: scoring reads it for every row, and a
        # lazy load per row is the same per-listing round-trip pattern that
        # took persist from 8s to 60s once already.
        q = q.options(selectinload(models.Listing.price_history))
        rows = (await s.scalars(q.limit(want).offset(offset))).all()

        out = []
        for r in rows:
            imgs = (
                await s.scalars(
                    select(models.ListingImage)
                    .where(models.ListingImage.listing_id == r.id)
                    .order_by(models.ListingImage.is_primary.desc())
                )
            ).all()
            stored = [i for i in imgs if i.status == ImageStatus.STORED and i.storage_url]
            seller = await s.get(models.Seller, r.seller_id) if r.seller_id else None
            out.append({
                "id": str(r.id),
                "fb_listing_id": r.fb_listing_id,
                "seller_name": seller.name if seller else None,
                "seller_url": seller.profile_url if seller else None,
                "seller_phone": seller.phone if seller else None,
                "seller_email": seller.email if seller else None,
                # The numeric Facebook user id. Present on ~91% of listings
                # (measured 2026-10-11 with include_details on) and the only
                # thing that can address a Messenger thread directly --
                # /messages/t/<id> opens the conversation with that person,
                # where the listing URL opens Marketplace's own composer.
                "fb_seller_id": seller.fb_seller_id if seller else None,
                "messenger_url": messenger_url(
                    seller.fb_seller_id if seller else None),
                "draft_message": draft_message(
                    r.title,
                    (r.price_minor / 100) if r.price_minor is not None else None,
                    seller.name if seller else None,
                ),
                "attributes": r.attributes or {},
                "is_sold": r.is_sold,
                "title": r.title,
                "price_inr": (r.price_minor / 100) if r.price_minor is not None else None,
                "condition": r.condition.value,
                "location": r.location_text,
                "listing_url": r.listing_url,
                "review_status": r.review_status.value,
                "posted_at": r.posted_at.isoformat() if r.posted_at else None,
                "last_seen_at": r.last_seen_at.isoformat(),
                "image_count": len(stored),
                # Our URL. Never fbcdn -- that is the whole point.
                "thumb": stored[0].storage_url if stored else None,
                # Served through /api/image: the bucket is private, so the
                # direct storage URL 403s and the browser shows a grey box.
                "thumb_id": str(stored[0].id) if stored else None,
                "images": [i.storage_url for i in stored[:6]],
                "category": r.category,
                "quality_score": r.quality_score,
                "description_facts": r.description_facts or {},
                "outreach_status": r.outreach_status or "new",
                "contacted_at": r.contacted_at.isoformat() if r.contacted_at else None,
                "outreach_note": r.outreach_note,
                "lead": lead_score.score_listing(
                    r,
                    price_history=list(r.price_history or []),
                    # Already fetched above -- passing it stops the scorer
                    # lazy-loading the relationship, which async SQLAlchemy
                    # refuses to do mid-request.
                    photo_count=len(stored),
                ).to_dict(),
            })

        if order == "lead":
            out.sort(key=lambda d: (-d["lead"]["score"], -len(d["lead"]["signals"])))
            out = out[:limit]
    return {"listings": out, "count": len(out)}


@app.get("/api/listings/{listing_id}")
async def listing_detail(listing_id: str) -> dict:
    """Every Phase 1 field for one listing, for the detailed view."""
    return await _listing(listing_id)


async def _listing(listing_id: str) -> dict:
    """Split from the handler the same way `_stats` and `_listings` are, so
    the payload shape can be asserted without standing up a database."""
    async with session_scope() as s:
        row = await s.get(models.Listing, listing_id)
        if row is None:
            raise HTTPException(404, "unknown listing")

        imgs = (await s.scalars(
            select(models.ListingImage)
            .where(models.ListingImage.listing_id == row.id)
            .order_by(models.ListingImage.is_primary.desc())
        )).all()
        seller = await s.get(models.Seller, row.seller_id) if row.seller_id else None
        history = (await s.scalars(
            select(models.ListingPriceHistory)
            .where(models.ListingPriceHistory.listing_id == row.id)
            .order_by(models.ListingPriceHistory.seen_at.desc())
            .limit(20)
        )).all()

        return {
            "id": str(row.id),
            "fb_listing_id": row.fb_listing_id,
            "title": row.title,
            "description": row.description,
            "price_inr": (row.price_minor / 100) if row.price_minor is not None else None,
            "currency": row.currency,
            "condition": row.condition.value,
            "city": row.city,
            "location": row.location_text,
            "category": row.category,
            "fb_category_id": row.fb_category_id,
            "attributes": row.attributes or {},
            "quality_score": row.quality_score,
            "quality_flags": row.quality_flags or [],
            "description_facts": row.description_facts or {},
            "listing_url": row.listing_url,
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
            "first_seen_at": row.first_seen_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat(),
            "review_status": row.review_status.value,
            "is_sold": row.is_sold,
            "is_pending": row.is_pending,
            "outreach_status": row.outreach_status or "new",
            "outreach_note": row.outreach_note,
            # Present on ~91% of listings -- measured 2026-10-11 over 96 items
            # from six runs, with `include_details` on. The 6.4% this project
            # quoted for weeks came from 47 fixtures captured BEFORE that flag
            # and was never re-measured. Absence is still normal on the
            # remaining ~9%, so it is not an error.
            "messenger_url": messenger_url(seller.fb_seller_id if seller else None),
            "draft_message": draft_message(
                row.title,
                (row.price_minor / 100) if row.price_minor is not None else None,
                seller.name if seller else None,
            ),
            "seller": {
                "fb_seller_id": seller.fb_seller_id,
                "name": seller.name,
                "profile_url": seller.profile_url,
                "phone": seller.phone,
                "email": seller.email,
                "rating": seller.rating,
            } if seller else None,
            "images": [{
                "id": str(i.id),
                "url": f"/api/image/{i.id}",
                "storage_url": i.storage_url,
                "is_primary": i.is_primary,
                "status": i.status.value,
                "size_bytes": i.size_bytes,
                "content_type": i.content_type,
            } for i in imgs],
            "price_history": [{
                "price_inr": (h.price_minor / 100) if h.price_minor is not None else None,
                "is_sold": h.is_sold,
                "seen_at": h.seen_at.isoformat(),
            } for h in history],
        }


@app.get("/api/image/{image_id}")
async def image(image_id: str):
    """Stream one rehosted photo from object storage.

    The bucket is NOT public -- `storage.googleapis.com/...` 403s for a
    browser, which is why thumbnails rendered as empty grey boxes. Making the
    bucket world-readable would fix it too, but the org policy here forbids
    public buckets (the same policy that forbids an external IP), and the spec
    says the data stays internal. Serving through the app uses the VM's own
    credentials and keeps everything behind whatever guards the console.
    """
    from fastapi.responses import Response

    from vingo_scraper.media.storage import get_store

    try:
        async with session_scope() as s:
            row = await s.get(models.ListingImage, image_id)
            if row is None or not row.storage_url:
                raise HTTPException(404, "unknown image")
            # The object key is the path after the bucket name. Derived rather
            # than stored because `storage_url` is already the single source of
            # truth, and a second column could disagree with it.
            key = row.storage_url.split(f"{settings.gcs_bucket}/", 1)[-1]
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("image.lookup_failed", error=str(exc)[:160])
        raise HTTPException(503, "database unavailable") from exc

    try:
        data, content_type = get_store().download(key)
    except Exception as exc:
        log.warning("image.fetch_failed", key=key, error=str(exc)[:160])
        raise HTTPException(404, "image not in storage") from exc

    # Content-addressed keys never change, so this is safe to cache hard.
    return Response(content=data, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/runs")
async def runs(limit: int = 10) -> dict:
    try:
        return await _runs(limit)
    except Exception as exc:
        log.warning("runs.db_unavailable", error=str(exc)[:160])
        return {"runs": [], "db_available": False}


async def _runs(limit: int) -> dict:
    async with session_scope() as s:
        rows = (await s.scalars(
            select(models.ScrapeRun)
            .order_by(models.ScrapeRun.started_at.desc())
            .limit(limit)
        )).all()
        return {"runs": [{
            "id": str(r.id),
            "run_id": r.run_id,
            "actor_id": r.actor_id,
            "status": r.status,
            "items_returned": r.items_returned,
            "items_ingested": r.items_ingested,
            "images_stored": r.images_stored,
            "images_failed": r.images_failed,
            "cost_usd": r.cost_usd,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        } for r in rows]}


# --------------------------------------------------------------------------
# manual ingestion
#
# Rebuilt onto the SAME two-phase runner automation uses. The previous version
# failed on serverless for three independent reasons, all invisible: it called
# the blocking `ingest()` (Actor runtime 10.8-47.1s against a 60s ceiling), it
# backgrounded the work with `asyncio.create_task` (which does not survive the
# response), and it streamed progress from an in-memory registry (so the SSE
# request landed on a lambda that had never heard of the job).
#
# Automation worked only because it had already been rebuilt this way.
# --------------------------------------------------------------------------

def title_filter_term(req: IngestRequest) -> str | None:
    """The keyword the title filter should enforce, or None to skip it.

    A named function rather than an expression inline, because the inline
    version was wrong and the test that should have caught it matched the
    comment explaining the wrongness instead of the behaviour.

    A keyword given ALONGSIDE a category is still a keyword. The earlier
    version read `req.query if req.title_match and not req.category else None`,
    so selecting a category silently switched the filter off while the
    checkbox stayed ticked -- which is how a search for "guitar" inside
    Musical instruments returned a townhouse and two cars.
    """
    if req.category:
        # A category run searches for the category's own name; the keyword
        # never reaches Facebook, so enforcing it locally would drop every
        # genuine result. The endpoint refuses the combination anyway -- this
        # is belt and braces on an expression that has been wrong twice.
        return None
    if not req.title_match:
        return None
    return (req.query or "").strip() or None


# --------------------------------------------------------------------------
# Bulk sweeps
# --------------------------------------------------------------------------

SWEEP_TARGETS = (250, 500, 1000)

# How long one cell may take before we stop waiting. The Actor measured
# 10.8s-47.1s for a single page and a cell is up to 3, so this is generous on
# purpose: giving up early is what threw away ~$1 of paid-for scraping.
CELL_TIMEOUT_S = 300
CELL_POLL_S = 3.0

# Fallback yields when no supply baseline has been measured yet. Deliberately
# FLAT: an even split is the honest thing to do when nothing is known, and the
# numbers being identical makes it obvious on screen that they are assumed
# rather than measured.
ASSUMED_YIELD_PER_PAGE = 12.0


def measured_yields() -> tuple[dict[str, float], bool]:
    """(yields, measured) -- net-new rows per page, per category.

    Reads `docs/supply-baseline.md` if `scripts/measure_supply.py` has been
    run. Returns `measured=False` when falling back to assumptions, so the UI
    can say which it is: an allocator running on guesses spends real money on
    them.
    """
    import re

    from vingo_scraper.config import sweep_cells

    # The cell set comes from here, NOT from the allocator: `plan_cells`
    # iterates whatever `yields` contains, so this dict decides what a sweep
    # walks. Keyed on `sweep_cells()` -- keying it on CATEGORIES would send
    # every sweep across Facebook's 17 top-level categories again, buying
    # inventory nobody asked for while the Vingo cells sat unused.
    cells = sweep_cells()

    path = Path(__file__).resolve().parents[3] / "docs" / "supply-baseline.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ({k: ASSUMED_YIELD_PER_PAGE for k in cells}, False)

    found: dict[str, float] = {}
    for key in cells:
        # "| `vehicles` | 28 | **26** | 4% | 41 | $0.0015 |"
        m = re.search(rf"\|\s*`{re.escape(key)}`\s*\|\s*\d+\s*\|\s*\*\*(\d+)\*\*", text)
        if m:
            found[key] = float(m.group(1))
    if not found:
        return ({k: ASSUMED_YIELD_PER_PAGE for k in cells}, False)

    # A PARTIAL baseline is the dangerous case, not a missing one. `plan_cells`
    # allocates only to cells present in this dict, so a file measured against
    # the old category set matches one key and silently shrinks the whole sweep
    # to that single cell -- a 1000-target run buying one page and reporting
    # "exhausted". Unmeasured cells get the assumption and `measured` stays
    # False until every cell has a real number behind it.
    complete = len(found) == len(cells)
    return ({k: found.get(k, ASSUMED_YIELD_PER_PAGE) for k in cells}, complete)


class SweepRequest(BaseModel):
    target: int = 250
    location: str = "bangalore"


async def _run_one_cell(category: str, pages: int, location: str):
    """One cell, through the same path a manual run takes.

    Returns what the cell ACTUALLY produced. The loop never assumes a cell
    delivered what was planned -- that assumption is what the rebalancing
    exists to correct.
    """
    from vingo_scraper.apify.client import LISTINGS_PER_PAGE, collect_run, start_run
    from vingo_scraper.apify.runner import ingest_collected
    from vingo_scraper.config import VINGO_CELLS
    from vingo_scraper.pipeline.sweep import CellOutcome

    # Vingo's targets are Facebook SUBcategories with no verified numeric id,
    # so they run as keyword searches with no category at all. Sending both
    # would be worse than either: `category_id` loses to a conflicting `query`
    # and is discarded in silence.
    keyword = VINGO_CELLS.get(category)

    try:
        started = await start_run(
            keyword, location, pages * LISTINGS_PER_PAGE,
            category=None if keyword else category, include_details=True,
            filters={"daysSinceListed": "7", "sortBy": "creation_time_descend"},
        )

        # POLL. `collect_run` is a single-shot check that returns None while
        # the run is still RUNNING -- it exists for the serverless pattern
        # where the browser polls. Calling it once here treated every live run
        # as a failure: 17 cells each started an Actor, were charged, and had
        # their results discarded a second later. ~$1 bought nothing.
        #
        # A cell is 1-3 pages and the Actor has measured 10.8s-47.1s for one
        # page, so the ceiling is generous and the interval short enough to
        # not add minutes across a 14-cell sweep.
        run = None
        deadline = time.monotonic() + CELL_TIMEOUT_S
        while time.monotonic() < deadline:
            run = await collect_run(started.run_id, run_input=started.run_input)
            if run is not None:
                break
            await asyncio.sleep(CELL_POLL_S)

        if run is None:
            # Timed out, not failed. The run may still finish and bill, so the
            # id goes in the message -- it can be collected later rather than
            # paid for again.
            return CellOutcome(
                category, pages,
                error=f"timed out after {CELL_TIMEOUT_S}s; run {started.run_id} "
                      f"may still be charging -- collect it rather than re-run",
            )
        # The keyword goes through so `pipeline.relevance` can filter on the
        # TITLE. Facebook matches description text, so a `watch` search returns
        # listings that merely mention one -- the Actor-side term reduces cost,
        # never guarantees correctness.
        result = await ingest_collected(
            run, location=location, trigger="bulk", search_term=keyword,
        )
        return CellOutcome(
            category, pages,
            new_rows=result.created,
            cost_usd=float(result.cost_usd or 0.0),
            error=result.error,
        )
    except Exception as exc:                 # noqa: BLE001
        # One bad cell must not lose the cells already paid for.
        return CellOutcome(category, pages, error=f"{type(exc).__name__}: {exc}"[:160])


async def _drive_sweep(target: int, location: str, resume) -> None:
    """Run the sweep to completion, saving state after every cell."""
    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.db import config_store
    from vingo_scraper.pipeline.sweep import run_quota_sweep

    yields, _ = measured_yields()
    budget = await get_budget()
    headroom = max(0.0, float(budget.total_cap_usd) - float(budget.monthly_used_usd))

    async def on_progress(state) -> None:
        await config_store.save_sweep(state.to_dict())

    async with config_store.ingestion_lock("bulk"):
        await run_quota_sweep(
            target=target, location=location, yields=yields,
            budget_usd=headroom, run_cell=_run_one_cell,
            resume=resume, on_progress=on_progress,
        )


@app.post("/api/sweep/start")
async def sweep_start(req: SweepRequest) -> dict:
    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.db import config_store
    from vingo_scraper.pipeline.sweep import SweepState

    if req.target not in SWEEP_TARGETS:
        raise HTTPException(400, f"target must be one of {SWEEP_TARGETS}")

    existing = SweepState.from_dict(await config_store.load_sweep())
    if existing and not existing.stopped_reason:
        # Never silently restart: every completed cell is already paid for.
        raise HTTPException(
            409,
            f"a sweep is already in flight ({existing.obtained} of "
            f"{existing.target}). Resume it or abandon it.",
        )

    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        raise HTTPException(402, blocked)

    # Stamped BEFORE the first cell starts, so no run this sweep creates can
    # fall outside its own window. A second later would be a race the Bulk tab
    # would show as a missing row.
    state = SweepState(
        target=req.target, location=req.location,
        started_at=datetime.now(UTC).isoformat(),
    )
    await config_store.save_sweep(state.to_dict())
    asyncio.create_task(_drive_sweep(req.target, req.location, None))
    return {"started": True, "target": req.target}


@app.post("/api/sweep/resume")
async def sweep_resume() -> dict:
    """Continue a sweep that died. Explicit, because the alternative --
    starting fresh -- re-buys every cell already paid for."""
    from vingo_scraper.db import config_store
    from vingo_scraper.pipeline.sweep import SweepState

    state = SweepState.from_dict(await config_store.load_sweep())
    if state is None:
        raise HTTPException(404, "no sweep to resume")

    state.stopped_reason = None
    state.stopped_kind = None
    # A resumed sweep is the SAME sweep, so its window must not move -- resetting
    # it here would hide the cells already paid for from the Bulk tab. Only a
    # state written before `started_at` existed needs one.
    if not state.started_at:
        state.started_at = datetime.now(UTC).isoformat()
    asyncio.create_task(_drive_sweep(state.target, state.location, state))
    return {"resumed": True, "from_cells": len(state.completed)}


@app.post("/api/sweep/abandon")
async def sweep_abandon() -> dict:
    from vingo_scraper.db import config_store
    await config_store.clear_sweep()
    return {"ok": True}


@app.get("/api/sweep/state")
async def sweep_state() -> dict:
    from vingo_scraper.db import config_store
    from vingo_scraper.pipeline.sweep import SweepState

    raw = await config_store.load_sweep()
    state = SweepState.from_dict(raw)
    yields, measured = measured_yields()
    if state is None:
        return {"running": False, "yields_measured": measured,
                "targets": list(SWEEP_TARGETS)}
    return {
        "running": state.stopped_reason is None,
        "target": state.target,
        "obtained": state.obtained,
        "spent_usd": round(state.spent_usd, 4),
        "cells": [
            {"category": c.category, "pages": c.pages, "new_rows": c.new_rows,
             "cost_usd": round(c.cost_usd, 4), "error": c.error}
            for c in state.completed
        ],
        "stopped_reason": state.stopped_reason,
        # The UI branches on this rather than on the sentence. "Target met" and
        # "Marketplace is empty" want opposite treatment, and telling them
        # apart by substring breaks the first time a sentence is reworded.
        "stopped_kind": state.stopped_kind,
        "location": state.location,
        "started_at": state.started_at,
        "yields_measured": measured,
        "targets": list(SWEEP_TARGETS),
    }


@app.post("/api/ingest")
async def start_ingest(
    req: IngestRequest,
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """Start one manual run. Returns immediately; poll /api/manual."""
    _require_secret(authorization, x_demo_token)

    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.apify.client import (
        MAX_ITEMS_CEILING,
        EmptySearchError,
        start_run,
    )
    from vingo_scraper.db import config_store

    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        raise HTTPException(402, blocked)

    # Volume is decided in ONE place -- `client.build_run_input`, which turns
    # items into PAGES because that is the unit Marketplace sells and Apify
    # bills. This ceiling is the 3-page equivalent; clamping to 25 here pinned
    # every option in the Volume selector to a single page, so the count came
    # back ~24 no matter what was asked for.
    # Facebook cannot search a keyword INSIDE a category: whichever query is
    # sent wins and `category_id` is discarded. Measured -- Musical instruments
    # + "iphone" returned iPhones. Refusing costs nothing; accepting and
    # quietly altering the request costs a page of scraping and an afternoon,
    # which is the fourth time this project has paid that price.
    if req.category and (req.query or "").strip():
        raise HTTPException(
            400,
            "category and keyword cannot be combined -- Facebook ignores the "
            "category when a keyword is given. Send one or the other.",
        )

    req.max_items = max(1, min(req.max_items, MAX_ITEMS_CEILING))
    # Both, when both are given: a keyword scopes a category browse.
    query = req.query or None
    label = f"{req.location} / {' / '.join(x for x in (req.category, req.query) if x)}"

    try:
        async with config_store.ingestion_lock("manual"):
            if await config_store.get_manual_pending():
                raise HTTPException(409, "a manual run is already in flight")
            run = await start_run(
                query, req.location, req.max_items,
                category=req.category,
                include_details=req.include_details,
                filters=req.to_filters(),
            )
            # Stored alongside the run input, never inside it: the Actor
            # rejects unknown keys and fails the entire run, which costs a
            # full page charge.
            await config_store.set_manual_pending(
                run.run_id,
                {**run.run_input, "_max_age_hours": req.max_age_hours,
                 "_search_term": title_filter_term(req),
                 # Enforced after the fetch because the Actor discards its own
                 # price fields whenever we drive it with `urls`.
                 "_min_price": req.min_price,
                 "_max_price": req.max_price},
                label,
            )
    except EmptySearchError as exc:
        # Refused before any spend: an unnarrowed browse is unbounded.
        raise HTTPException(400, str(exc)) from exc
    except config_store.IngestionBusy as busy:
        # One Actor at a time. With automation gone this guards against a
        # double-click rather than two runners, but the money is the same.
        raise HTTPException(409, "A run is already in progress") from busy

    return {"started": True, "run_id": run.run_id, "cell": label,
            "reason": "actor started; poll /api/manual to collect it"}


@app.post("/api/ingest/collect")
async def collect_ingest(
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """Second phase: pick up a finished manual run and ingest it."""
    _require_secret(authorization, x_demo_token)

    from vingo_scraper.apify.budget import invalidate_cache
    from vingo_scraper.apify.client import collect_run
    from vingo_scraper.apify.runner import ingest_collected
    from vingo_scraper.db import config_store

    pending = await config_store.get_manual_pending()
    if pending is None:
        return {"ran": False, "reason": "no manual run in flight"}

    run_id, run_input = pending
    city = run_input.get("location") or "bangalore"
    try:
        run = await collect_run(run_id, run_input=run_input)
    except Exception as exc:
        await config_store.clear_manual(f"failed: {type(exc).__name__}")
        log.warning("manual.collect_failed", run_id=run_id, error=str(exc)[:200])
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}

    if run is None:
        return {"ran": False, "waiting": True, "run_id": run_id,
                "reason": "actor still running"}

    try:
        result = await ingest_collected(
            run, location=city, trigger="manual",
            max_age_hours=(run_input or {}).get("_max_age_hours"),
            search_term=(run_input or {}).get("_search_term"),
            min_price=(run_input or {}).get("_min_price"),
            max_price=(run_input or {}).get("_max_price"),
        )
        await config_store.clear_manual(
            "failed: " + result.error if result.error else "ok"
        )
        invalidate_cache()
        return {
            "ran": True, "run_id": run_id,
            "listings": len(result.listings),
            "created": result.created,
            "updated": result.updated,
            "images_stored": result.images_stored,
            "skipped_duplicates": result.skipped_duplicates,
            "rejected": result.rejected,
            "rejected_reasons": result.rejected_reasons,
            "stale_dropped": result.stale_dropped,
            "overpriced": result.overpriced,
            # What Marketplace handed back, versus what we paid to receive.
            # Without both, "14 listings" cannot be told apart from
            # "we asked for 72 and Facebook only had 14".
            "items_returned": result.items_returned,
            "requested": result.requested,
            "off_topic": result.off_topic,
            "off_topic_examples": result.off_topic_examples,
            "cost_usd": result.cost_usd,
            "all_cached": result.all_cached,
            "error": result.error,
        }
    except Exception as exc:
        await config_store.clear_manual(f"error: {type(exc).__name__}")
        log.exception("manual.ingest_failed", run_id=run_id)
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/manual")
async def manual_status() -> dict:
    """Manual run state plus its own history. Open: read-only."""
    from vingo_scraper.db import config_store

    try:
        state = await config_store.manual_state()
        state["history"] = await config_store.recent_ticks(trigger="manual")
        state["lock"] = await config_store.lock_status()
        return state
    except Exception as exc:
        log.warning("manual.status_failed", error=str(exc)[:200])
        return {"running": False, "history": [], "error": type(exc).__name__}


# --------------------------------------------------------------------------
# review queue -- demonstrates that nothing auto-publishes
# --------------------------------------------------------------------------

class OutreachRequest(BaseModel):
    """Unlike review, this takes a BODY and moves in both directions.

    Outreach genuinely goes backwards -- a "replied" that turns out to be a
    wrong number is "new" again, and forcing a one-way transition would make
    the operator lie to the tool to keep working.
    """

    status: str
    note: str | None = None


OUTREACH_STATUSES = ("new", "contacted", "replied", "converted", "dead")


@app.post("/api/listings/{listing_id}/outreach")
async def set_outreach(listing_id: str, req: OutreachRequest) -> dict:
    if req.status not in OUTREACH_STATUSES:
        raise HTTPException(400, f"status must be one of {OUTREACH_STATUSES}")
    async with session_scope() as s:
        row = await s.get(models.Listing, uuid.UUID(listing_id))
        if row is None:
            raise HTTPException(404, "no such listing")
        row.outreach_status = req.status
        if req.note is not None:
            # Truncate rather than let Postgres raise. The column is Text so
            # there is no hard limit, but an accidental paste of a whole chat
            # log is not a note.
            row.outreach_note = req.note[:2000]
        # Stamped on the first move away from `new`, and kept thereafter --
        # "when did we first reach out" is the question, not "when was this
        # row last touched".
        if req.status != "new" and row.contacted_at is None:
            row.contacted_at = datetime.now(UTC)
        if req.status == "new":
            row.contacted_at = None
        return {
            "ok": True, "status": row.outreach_status,
            "contacted_at": row.contacted_at.isoformat() if row.contacted_at else None,
        }


class ContactRequest(BaseModel):
    """What a seller replied with, typed in by whoever read the reply.

    MANUAL on purpose. We cannot read a Messenger inbox: detecting replies
    automatically needs a logged-in Facebook account driven by code, which is
    the same automation that gets accounts banned -- and an `outreach_status`
    that updated itself without anyone checking is a number nobody can trust.
    """

    phone: str | None = None
    email: str | None = None
    note: str | None = None


@app.post("/api/listings/{listing_id}/contact")
async def capture_contact(listing_id: str, req: ContactRequest) -> dict:
    """Record a reply: contact on the SELLER, the note on the listing.

    Contact goes on the seller row because one person has several listings.
    Capture a number once and every other listing by them shows it, so nobody
    messages the same seller twice asking for details they already gave.
    """
    if not (req.phone or req.email):
        raise HTTPException(400, "a phone or an email is required")
    async with session_scope() as s:
        row = await s.get(models.Listing, uuid.UUID(listing_id))
        if row is None:
            raise HTTPException(404, "no such listing")
        if row.seller_id is None:
            # The ~9% with no seller identity. There is no row to hang a
            # number on, and inventing one would key it to a listing instead
            # of a person -- which is the thing this endpoint exists to avoid.
            raise HTTPException(
                409, "this listing has no seller identity, so a contact "
                     "cannot be attached to a person"
            )
        seller = await s.get(models.Seller, row.seller_id)
        if req.phone:
            seller.phone = req.phone.strip()[:32]
        if req.email:
            seller.email = req.email.strip()[:255]
        if req.note is not None:
            row.outreach_note = req.note[:2000]
        row.outreach_status = "replied"
        if row.contacted_at is None:
            row.contacted_at = datetime.now(UTC)
        return {
            "ok": True, "status": row.outreach_status,
            "seller_phone": seller.phone, "seller_email": seller.email,
        }


@app.get("/api/export.csv")
async def export_csv(
    search: str | None = None, outreach: str | None = None,
    has_contact: bool = False, since_ts: str | None = None,
    order: str = "lead", limit: int = 1000,
) -> Response:
    """The lead queue as a file, for whoever works it outside this console.

    Same filters as /api/listings so the export is what you are looking at,
    not a different question answered separately.
    """
    data = await _listings(limit, None, search=search, outreach=outreach,
                           has_contact=has_contact, since_ts=since_ts,
                           order=order)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "lead_score", "band", "signals", "title", "price_inr", "location",
        "category", "posted_at", "photos", "quality_score",
        "outreach_status", "contacted_at", "note",
        "seller_name", "seller_phone", "seller_email",
        "messenger_url", "listing_url",
    ])
    for row in data["listings"]:
        lead = row.get("lead") or {}
        w.writerow([
            lead.get("score", 0), lead.get("band", ""),
            " | ".join(s["label"] for s in lead.get("signals", [])),
            row["title"], row["price_inr"] or "", row["location"] or "",
            row.get("category") or "", row["posted_at"] or "",
            row["image_count"], row.get("quality_score") or 0,
            row.get("outreach_status") or "new", row.get("contacted_at") or "",
            row.get("outreach_note") or "",
            # Whatever the seller volunteered in reply. Facebook publishes
            # neither, so an empty phone column means nobody has answered yet
            # -- not that the scrape missed it.
            row.get("seller_name") or "", row.get("seller_phone") or "",
            row.get("seller_email") or "",
            # The two contact routes, longest last. Messenger addresses the
            # PERSON; the listing URL only opens Marketplace's composer under
            # that one item, and is all there is for the ~9% with no id.
            messenger_url(row.get("fb_seller_id")) or "",
            row["listing_url"],
        ])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vingo-leads.csv"'},
    )


@app.post("/api/listings/{listing_id}/review")
async def review(listing_id: str, action: str = "approve") -> dict:
    target = {
        "approve": ReviewStatus.APPROVED,
        "reject": ReviewStatus.REJECTED,
        "takedown": ReviewStatus.TAKEDOWN,
    }.get(action)
    if target is None:
        raise HTTPException(400, f"unknown action {action!r}")

    async with session_scope() as s:
        row = await s.get(models.Listing, listing_id)
        if row is None:
            raise HTTPException(404, "unknown listing")
        row.review_status = target
    return {"id": listing_id, "review_status": target.value}


# --------------------------------------------------------------------------
# automation -- the grid walk, state in Postgres
#
# This used to be an in-process scheduler with a module-level state object and
# an asyncio loop. Neither works on Vercel: every lambda instance gets its own
# copy of the object (so polling alternated between two self-consistent views),
# and a background task does not survive the response that created it.
#
# Both symptoms are silent -- no error, just a UI that flickers and a sequence
# that stalls after one run. State now lives in `system_config`; ticks are
# driven by HTTP, either from the open console or an external pinger.
# --------------------------------------------------------------------------

@app.get("/api/budget")
async def budget() -> dict:
    from vingo_scraper.apify.budget import get_budget

    return (await get_budget()).to_dict()


# --------------------------------------------------------------------------
# demo control -- the kill switch, and the bounded 3-run sequence
#
# Vercel functions are stateless, so the on/off state lives in `system_config`
# rather than in memory. Every tick reads it BEFORE calling Apify: a switch
# that is checked after the spend is not a kill switch.
# --------------------------------------------------------------------------

def _require_secret(authorization: str | None, x_demo_token: str | None) -> None:
    """No-op. Kept so call sites read the same if a gate is ever re-added.

    Authentication now lives at the edge: Caddy holds a password and the app
    listens on loopback only, so nothing reaches these endpoints unauthenticated.
    A second secret inside added friction without adding a barrier -- the
    operator had to paste a token into a page they had already authenticated to.

    **This makes the edge gate load-bearing.** If Caddy is ever bypassed or its
    basic_auth block removed, /api/ingest becomes an open endpoint that spends
    money. The budget cap and the kill switch are the remaining guards.
    """
    return


def _unused_require_secret(authorization: str | None, x_demo_token: str | None) -> None:
    """Shared-secret gate.

    These endpoints start and stop real spending, so they are not left open.
    Vercel Cron sends `Authorization: Bearer <secret>`; the toggle UI sends
    `X-Demo-Token`.
    """
    expected = settings.demo_secret
    if not expected:
        raise HTTPException(503, "VINGO_DEMO_SECRET is not configured")

    supplied = x_demo_token or ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = supplied or authorization[7:]

    # compare_digest avoids leaking the secret through timing.
    import hmac

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "bad or missing demo token")


@app.get("/api/health")
async def health() -> dict:
    try:
        async with session_scope() as s:
            await s.scalar(select(func.count(models.Listing.id)))
        db_ok = True
    except Exception as exc:
        db_ok = False
        log.warning("health.db_down", error=str(exc)[:200])
    return {
        "ok": db_ok,
        "apify_configured": bool(settings.apify_api_token),
        "supabase_configured": bool(settings.supabase_service_key),
    }
