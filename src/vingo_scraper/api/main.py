"""Demo console: one page showing the whole ingest flow.

    click "Run today's update"
      -> Apify actor runs
      -> listings parsed
      -> images rehosted off Facebook's CDN into Supabase
      -> rows upserted
      -> table refreshes from the database

Everything the page shows is read back out of Supabase, not from the in-memory
job. That is the point of the demo: the table proves the database actually
changed, rather than echoing what the pipeline claimed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from vingo_scraper.api.jobs import Job, registry
from vingo_scraper.config import settings
from vingo_scraper.db import models
from vingo_scraper.db.session import session_scope
from vingo_scraper.extract.schema import ImageStatus, ReviewStatus

log = structlog.get_logger(__name__)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Vingo Ingestion Console", docs_url="/api/docs")


class IngestRequest(BaseModel):
    query: str = "iphone"
    location: str = "mumbai"
    max_items: int = 10
    dry_run: bool = False


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
    }


@app.get("/api/listings")
async def listings(limit: int = 60, since_run: str | None = None) -> dict:
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
            out.append({
                "id": str(r.id),
                "fb_listing_id": r.fb_listing_id,
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


@app.get("/api/runs")
async def runs(limit: int = 10) -> dict:
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
# the run
# --------------------------------------------------------------------------

async def _run_job(job: Job, req: IngestRequest) -> None:
    from vingo_scraper.apify.runner import ingest

    job.status = "running"
    await job.emit("job:start", {
        "query": req.query, "location": req.location,
        "max_items": req.max_items, "dry_run": req.dry_run,
    })
    try:
        result = await ingest(
            search_query=req.query,
            location=req.location,
            max_items=req.max_items,
            dry_run=req.dry_run,
            on_event=lambda stage, payload: job.emit(stage, payload),
        )
        job.result = {
            "items_returned": result.items_returned,
            "listings": len(result.listings),
            "created": result.created,
            "updated": result.updated,
            "images_stored": result.images_stored,
            "images_failed": result.images_failed,
            "cost_usd": result.cost_usd,
            "run_id": result.run_id,
            "error": result.error,
        }
        job.status = "failed" if result.error else "done"
        job.error = result.error
    except Exception as exc:  # pragma: no cover - surfaced to the UI
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        await job.emit("error", {"message": job.error})
        log.exception("job.failed", job_id=job.id)
    finally:
        from datetime import UTC, datetime

        job.finished_at = datetime.now(UTC)
        await job.emit("job:end", {"status": job.status, "error": job.error})


@app.post("/api/ingest")
async def start_ingest(req: IngestRequest) -> dict:
    if registry.running:
        raise HTTPException(409, "an ingest is already running")
    # Apify bills per result; keep the demo button cheap regardless of input.
    req.max_items = max(1, min(req.max_items, 25))
    job = registry.create(
        query=req.query, location=req.location,
        max_items=req.max_items, dry_run=req.dry_run,
    )
    asyncio.create_task(_run_job(job, req))
    return {"job_id": job.id, "status": job.status}


@app.get("/api/ingest/{job_id}/stream")
async def stream(job_id: str) -> StreamingResponse:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")

    async def gen():
        q = job.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") == "job:end":
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/ingest/{job_id}")
async def job_status(job_id: str) -> dict:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {
        "id": job.id, "status": job.status, "error": job.error,
        "result": job.result, "events": list(job.events),
    }


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
