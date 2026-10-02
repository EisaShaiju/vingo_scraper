"""Ingestion console and API.

Two runners, one pipeline:

  * automation -- a round-robin over the Bengaluru category grid
  * manual     -- a single operator-chosen query

Both go through the SAME two-phase Apify path (`start_run` then `collect_run`)
and the SAME Postgres mutex, because two runners starting Actors at once is
not a correctness curiosity -- it is paying twice.

Everything the page shows is read back out of the database, not from an
in-process object. That is deliberate and was learned the hard way: in-memory
state does not error under concurrency, it just disagrees with itself.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from vingo_scraper.config import settings
from vingo_scraper.db import models
from vingo_scraper.db.session import session_scope
from vingo_scraper.extract.schema import ImageStatus, ReviewStatus

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
    # Defaults off. Details double the per-listing charge, so the expensive
    # option should be the one you have to ask for.
    include_details: bool = False
    # 5 = one full sweep of the Bengaluru category grid.
    runs: int = 5
    city: str | None = None
    # Deliberately open on category 1 rather than continuing the rotation.
    restart: bool = False


class IngestRequest(BaseModel):
    query: str = "iphone"
    # Bengaluru first, per the client spec. This defaulted to Mumbai long
    # after that decision was made.
    location: str = "bangalore"
    max_items: int = 10
    dry_run: bool = False
    # Optional category; when set it supplies the search term, so the manual
    # panel and the automation grid can run the identical query.
    category: str | None = None
    include_details: bool = False
    # Marketplace filters. Validated against the Actor's real schema in
    # `client.merge_filters` -- unknown keys are dropped rather than sent,
    # because Apify fails the whole run on an unrecognised input field.
    min_price: int | None = None
    max_price: int | None = None
    sort_by: str | None = None
    days_since_listed: str | None = None

    def to_filters(self) -> dict:
        return {
            "minPrice": self.min_price,
            "maxPrice": self.max_price,
            "sortBy": self.sort_by,
            "daysSinceListed": self.days_since_listed,
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
            "supabase_project": settings.supabase_url.replace("https://", "").split(".")[0],
            "bucket": settings.storage_bucket,
        }


def _friendly_db_error(exc: Exception) -> str:
    """Turn a psycopg wall-of-text into something actionable."""
    msg = str(exc)
    if "getaddrinfo failed" in msg or "ENOTFOUND" in msg or "not found" in msg:
        return "Supabase project unreachable - check VINGO_DATABASE_URL in .env"
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
        "supabase_project": settings.supabase_url.replace("https://", "").split(".")[0],
        "bucket": settings.storage_bucket,
        "db_available": True,
        "db_error": None,
    }


@app.get("/api/listings")
async def listings(limit: int = 60, since_run: str | None = None) -> dict:
    try:
        return await _listings(limit, since_run)
    except Exception as exc:
        log.warning("listings.db_unavailable", error=str(exc)[:160])
        return {"listings": [], "count": 0, "db_available": False}


async def _listings(limit: int, since_run: str | None) -> dict:
    async with session_scope() as s:
        q = select(models.Listing).order_by(models.Listing.last_seen_at.desc())
        if since_run:
            run = await s.scalar(
                select(models.ScrapeRun).where(models.ScrapeRun.run_id == since_run)
            )
            if run:
                q = q.where(models.Listing.scrape_run_id == run.id)
        rows = (await s.scalars(q.limit(limit))).all()

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
                "images": [i.storage_url for i in stored[:6]],
            })
    return {"listings": out, "count": len(out)}


@app.get("/api/listings/{listing_id}")
async def listing_detail(listing_id: str) -> dict:
    """Every Phase 1 field for one listing, for the detailed view."""
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
            "listing_url": row.listing_url,
            "posted_at": row.posted_at.isoformat() if row.posted_at else None,
            "first_seen_at": row.first_seen_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat(),
            "review_status": row.review_status.value,
            "is_sold": row.is_sold,
            "is_pending": row.is_pending,
            # Present for ~6% of listings: Facebook withholds seller identity
            # from anonymous reads. Absence is normal, not an error.
            "seller": {
                "name": seller.name,
                "profile_url": seller.profile_url,
                "phone": seller.phone,
                "email": seller.email,
                "rating": seller.rating,
            } if seller else None,
            "images": [{
                "url": i.storage_url,
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

@app.post("/api/ingest")
async def start_ingest(
    req: IngestRequest,
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """Start one manual run. Returns immediately; poll /api/manual."""
    _require_secret(authorization, x_demo_token)

    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.apify.client import start_run
    from vingo_scraper.config import CATEGORIES
    from vingo_scraper.db import config_store

    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        raise HTTPException(402, blocked)

    # Apify bills per result; keep the manual button cheap regardless of input.
    req.max_items = max(1, min(req.max_items, 25))
    query = req.query
    if req.category:
        query = CATEGORIES.get(req.category, {}).get("query", req.category)
    label = f"{req.location} / {req.category or query}"

    try:
        async with config_store.ingestion_lock("manual"):
            if await config_store.get_manual_pending():
                raise HTTPException(409, "a manual run is already in flight")
            run = await start_run(
                query, req.location, req.max_items,
                include_details=req.include_details,
                filters=req.to_filters(),
            )
            await config_store.set_manual_pending(run.run_id, run.run_input, label)
    except config_store.IngestionBusy as busy:
        # The whole point of the lock: two runners must never start Actors at
        # the same time, because that is paying twice.
        raise HTTPException(
            409, "Ingestion currently in progress by automated runner"
            if busy.holder == "automation" else f"Ingestion in progress by {busy.holder}"
        ) from busy

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
        result = await ingest_collected(run, location=city, trigger="manual")
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
            "quality_flagged": result.quality_flagged,
            "cost_usd": result.cost_usd,
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

@app.get("/api/automation")
async def automation_status() -> dict:
    from vingo_scraper.db import config_store

    try:
        snap = await config_store.automation_snapshot()
        snap["history"] = await config_store.recent_ticks()
        return snap
    except Exception as exc:
        # A read must degrade, not 500 -- the console polls this every 5s.
        log.warning("automation.snapshot_failed", error=str(exc)[:200])
        return {"running": False, "error": f"{type(exc).__name__}", "history": []}


@app.post("/api/automation/start")
async def automation_start(
    req: AutomationRequest,
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    _require_secret(authorization, x_demo_token)

    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.db import config_store

    # Refuse to arm something that cannot afford its first tick.
    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        raise HTTPException(402, blocked)

    await config_store.start_automation(
        runs=req.runs,
        interval_seconds=req.interval_s,
        per_cell=req.per_cell,
        include_details=req.include_details,
        city=req.city,
        restart=req.restart,
    )
    return await config_store.automation_snapshot()


@app.post("/api/automation/stop")
async def automation_stop() -> dict:
    """Deliberately UNAUTHENTICATED.

    Starting spends money, so it needs the secret. Stopping only ever spends
    less, and a kill switch you can fail to authenticate to is not a kill
    switch. The worst an anonymous caller can do here is end a demo early;
    the worst a gated stop can do is leave an actor running while someone
    hunts for a token.
    """
    from vingo_scraper.db import config_store

    await config_store.stop_sequence()
    return await config_store.automation_snapshot()


@app.post("/api/automation/tick")
async def automation_tick(
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """One automation step, driven by whoever is watching.

    Same body as `/api/cron/tick` -- one ordering of the guards, not two.
    """
    _require_secret(authorization, x_demo_token)
    return await _run_tick(grid=True)


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


@app.get("/api/demo/status")
async def demo_status() -> dict:
    """Unauthenticated: read-only, and the demo screen needs to poll it."""
    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.db import config_store

    try:
        state = (await config_store.get_state()).to_dict()
    except Exception as exc:
        return {"error": f"config unavailable: {type(exc).__name__}",
                "is_ingestion_active": False}
    state["budget"] = (await get_budget()).to_dict()
    return state


@app.post("/api/demo/start")
async def demo_start(
    req: DemoStartRequest,
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    _require_secret(authorization, x_demo_token)

    from vingo_scraper.apify.budget import get_budget
    from vingo_scraper.db import config_store

    # Refuse to arm a sequence the budget cannot cover.
    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        raise HTTPException(402, blocked)

    state = await config_store.start_sequence(
        runs=req.runs,
        interval_seconds=req.interval_seconds,
        search_query=req.search_query,
        location=req.location,
        max_pages=req.max_pages,
    )
    return state.to_dict()


@app.post("/api/demo/stop")
async def demo_stop(
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """Kill switch. Effective before the next tick spends anything."""
    _require_secret(authorization, x_demo_token)
    from vingo_scraper.db import config_store

    return (await config_store.stop_sequence()).to_dict()


@app.post("/api/demo/toggle")
async def demo_toggle(
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    _require_secret(authorization, x_demo_token)
    from vingo_scraper.db import config_store

    return (await config_store.toggle()).to_dict()


async def _run_tick(*, grid: bool) -> dict:
    """One scheduled ingestion step. The only place a tick is defined.

    Order matters and is deliberate:
      1. (the caller authenticates)
      2. check the kill switch      <- no spend if off
      3. check the budget           <- no spend if capped
      4. claim a run from the sequence
      5. only then call Apify

    A switch checked after the spend is not a switch. `tests/test_demo_control`
    asserts Apify is called zero times when the flag is off.

    `grid=True` walks the automation cells (console); `grid=False` runs the
    single configured query (Vercel Cron, external pinger).
    """
    from vingo_scraper.apify.budget import (
        get_budget,
        invalidate_cache,
        reconcile_costs,
    )
    from vingo_scraper.db import config_store

    # Charges post after a run finishes, so what the previous tick recorded was
    # incomplete. Correct it before reading the budget off those rows.
    await reconcile_costs()

    # A run already in flight is collected first, and costs nothing new. This
    # runs even when ingestion has since been switched off: the money is
    # already spent, and throwing the data away would not refund it.
    if (pending := await config_store.get_pending()) is not None:
        return await _collect_pending(pending)

    state = await config_store.get_state()
    if not state.is_active:
        # Distinguish "it finished" from "someone killed it". `claim_run`
        # switches ingestion off as it hands out the LAST slot, so the next
        # tick finds the flag already down -- reporting that as "switched off"
        # reads as an outside intervention and sent us hunting for one.
        finished = (state.stopped_reason or "").startswith("sequence complete")
        return {
            "ran": False,
            "reason": "sequence complete" if finished
                      else (state.stopped_reason or "ingestion is switched off"),
            "complete": finished,
            **state.to_dict(),
        }

    budget = await get_budget()
    if (blocked := budget.blocked_reason()):
        await config_store.stop_sequence(f"budget: {blocked}")
        return {"ran": False, "reason": blocked, "budget": budget.to_dict()}

    claimed = await config_store.claim_run()
    if claimed is None:
        return {"ran": False, "reason": "sequence complete",
                **(await config_store.get_state()).to_dict()}

    from vingo_scraper.apify.client import start_run
    from vingo_scraper.config import CATEGORIES
    from vingo_scraper.pipeline.sweep import automation_cells

    if grid:
        cells = automation_cells(claimed.city)
        city, category = cells[claimed.cursor % len(cells)]
        query = CATEGORIES.get(category, {}).get("query", category)
        max_items, details = claimed.per_cell, claimed.include_details
    else:
        city, category = claimed.location, claimed.search_query
        query = claimed.search_query
        max_items, details = claimed.max_pages * 25, True

    cell = f"{city} / {category}"
    try:
        # Same lock the manual path takes. If a manual run holds it, this tick
        # defers rather than starting a second Actor alongside it -- no spend,
        # and the slot is returned by not having been claimed yet.
        async with config_store.ingestion_lock("automation"):
            run = await start_run(
                query, city, max_items, include_details=details,
                filters=await config_store.get_filters(),
            )
            await config_store.set_pending(run.run_id, cell, run.run_input)
        invalidate_cache()  # that run will spend; re-read before the next
        return {
            "ran": False, "started": True, "cell": cell,
            "run_id": run.run_id, "run": claimed.runs_completed,
            "reason": "actor started; a later tick collects it",
            "runs_remaining": claimed.runs_remaining,
        }
    except config_store.IngestionBusy as busy:
        return {"ran": False, "deferred": True, "cell": cell,
                "reason": f"ingestion in progress by {busy.holder}"}
    except Exception as exc:
        # One dead cell must not stop the sequence -- record it and let the
        # next tick try a different one.
        await config_store.record_result(f"error: {type(exc).__name__}")
        log.exception("tick.start_failed", cell=cell)
        return {
            "ran": False, "cell": cell,
            "error": f"{type(exc).__name__}: {exc}",
            "runs_remaining": claimed.runs_remaining,
        }


async def _collect_pending(pending: tuple[str, str, dict]) -> dict:
    """Second half of a tick: pick up a finished run and ingest it.

    Separated from starting it because the Actor's runtime is not ours and not
    predictable -- 10.8s to 47.1s measured on identical work. Waiting for it
    inside the request made every tick a gamble against the 60s ceiling, and
    losing meant the function died *after* Apify had charged us.
    """
    from vingo_scraper.apify.client import collect_run
    from vingo_scraper.apify.runner import ingest_collected
    from vingo_scraper.db import config_store

    run_id, cell, run_input = pending
    city = cell.split(" / ")[0]

    try:
        run = await collect_run(run_id, run_input=run_input)
    except Exception as exc:
        await config_store.clear_pending(f"failed: {type(exc).__name__}")
        log.warning("tick.collect_failed", run_id=run_id, cell=cell,
                    error=str(exc)[:200])
        return {"ran": False, "cell": cell, "error": f"{type(exc).__name__}: {exc}"}

    if run is None:
        return {"ran": False, "waiting": True, "cell": cell, "run_id": run_id,
                "reason": "actor still running"}

    try:
        result = await ingest_collected(run, location=city, trigger="automation")
        await config_store.clear_pending(
            "failed: " + result.error if result.error else "ok"
        )
        return {
            "ran": True, "cell": cell, "run_id": run_id,
            "listings": len(result.listings),
            "created": result.created,
            "updated": result.updated,
            "images_stored": result.images_stored,
            "cost_usd": result.cost_usd,
            "error": result.error,
        }
    except Exception as exc:
        await config_store.clear_pending(f"error: {type(exc).__name__}")
        log.exception("tick.ingest_failed", run_id=run_id, cell=cell)
        return {"ran": False, "cell": cell, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/cron/tick")
@app.post("/api/cron/tick")
async def cron_tick(
    authorization: str | None = Header(None),
    x_demo_token: str | None = Header(None),
) -> dict:
    """Wired to Vercel Cron, and to any external pinger."""
    _require_secret(authorization, x_demo_token)
    return await _run_tick(grid=False)


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
