"""Trigger the Apify Marketplace Actor and collect its dataset.

This module owns the whole relationship with Apify. Callers speak in
(search_query, location, max_items); the Actor's own input shape
(`startUrls` / `resultsLimit` / `includeListingDetails`) never leaks past this
file. That indirection is deliberate: there are several competing Marketplace
Actors, and swapping one in should not move a single call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from apify_client import ApifyClientAsync

from vingo_scraper.apify.urls import category_url, search_url, to_start_urls
from vingo_scraper.config import settings

log = structlog.get_logger(__name__)

# Apify reports these as terminal states.
_SUCCESS = "SUCCEEDED"


class ApifyRunError(RuntimeError):
    """The Actor run did not succeed.

    Raised rather than returning an empty list, because "the run failed" and
    "the search genuinely had no results" lead to opposite decisions and must
    never be collapsed into the same empty response.
    """


@dataclass
class ActorRun:
    """Everything worth keeping about one Actor run.

    Persisted to `scrape_run`: this is both the audit trail and the
    cost-per-listing evidence for the client report.
    """

    run_id: str | None
    actor_id: str
    status: str
    run_input: dict[str, Any]
    items: list[dict[str, Any]] = field(default_factory=list)
    dataset_id: str | None = None
    cost_usd: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == _SUCCESS

    @property
    def cost_per_item(self) -> float | None:
        if not self.cost_usd or not self.items:
            return None
        return round(self.cost_usd / len(self.items), 5)


def build_run_input(
    search_query: str | None,
    location: str,
    max_items: int,
    *,
    category: str | None = None,
    include_details: bool = True,
) -> dict[str, Any]:
    """Translate our parameters into the Actor's input schema.

    `resultsLimit` is always set. Leaving it unset means *unlimited* on an
    Actor that bills per result, which is an unbounded invoice.
    """
    if category:
        urls = [category_url(location, category)]
    elif search_query:
        urls = [search_url(location, search_query)]
    else:
        raise ValueError("need either search_query or category")

    return {
        "startUrls": to_start_urls(urls),
        "resultsLimit": max_items,
        # Required for `description`, which is one of the fields we promise.
        # Costs more per item, hence a parameter rather than a constant.
        "includeListingDetails": include_details,
    }


def _clamp(max_items: int) -> int:
    """Apply the hard ceiling.

    Enforced here, not in the CLI, because Apify bills per result and a typo in
    `--max-items` is a billing incident rather than a usability problem.
    """
    cap = settings.apify_max_items_hard_cap
    if max_items > cap:
        log.warning("apify.max_items_clamped", requested=max_items, cap=cap)
        return cap
    return max(1, max_items)


async def run_actor(
    search_query: str | None,
    location: str,
    max_items: int = 100,
    *,
    category: str | None = None,
    include_details: bool = True,
    client: ApifyClientAsync | None = None,
) -> ActorRun:
    """Run the Actor to completion and return its dataset items."""
    if not settings.apify_api_token and client is None:
        raise ApifyRunError(
            "VINGO_APIFY_API_TOKEN is not set -- copy .env.example to .env"
        )

    max_items = _clamp(max_items)
    run_input = build_run_input(
        search_query, location, max_items,
        category=category, include_details=include_details,
    )
    actor_id = settings.apify_actor_id
    client = client or ApifyClientAsync(token=settings.apify_api_token)

    result = ActorRun(
        run_id=None, actor_id=actor_id, status="PENDING",
        run_input=run_input, started_at=datetime.now(UTC),
    )

    log.info(
        "apify.run.start", actor=actor_id, location=location,
        query=search_query or category, limit=max_items,
    )

    try:
        run = await client.actor(actor_id).call(
            run_input=run_input,
            # Belt and braces against the Actor overshooting our limit: these
            # are enforced by Apify itself, not by the Actor's own code.
            max_items=max_items,
            max_total_charge_usd=Decimal(str(settings.apify_max_charge_usd)),
            run_timeout=timedelta(seconds=settings.apify_timeout_s),
        )
    except Exception as exc:
        result.status = "CLIENT_ERROR"
        result.error = f"{type(exc).__name__}: {exc}"
        result.finished_at = datetime.now(UTC)
        log.error("apify.run.exception", error=result.error)
        raise ApifyRunError(result.error) from exc

    if run is None:
        result.status = "FAILED"
        result.error = "actor call returned None"
        result.finished_at = datetime.now(UTC)
        raise ApifyRunError("Apify run failed (client returned None)")

    result.run_id = _attr(run, "id")
    result.status = str(_attr(run, "status") or "UNKNOWN")
    result.dataset_id = _attr(run, "default_dataset_id", "defaultDatasetId")
    result.cost_usd = _attr(run, "usage_total_usd", "usageTotalUsd")
    result.finished_at = datetime.now(UTC)

    if not result.succeeded:
        result.error = f"run status={result.status}"
        log.error("apify.run.failed", run_id=result.run_id, status=result.status)
        raise ApifyRunError(
            f"Apify run {result.run_id} finished with status {result.status}"
        )

    if not result.dataset_id:
        raise ApifyRunError(f"run {result.run_id} succeeded but exposed no dataset")

    result.items = await fetch_dataset(client, result.dataset_id)

    log.info(
        "apify.run.done", run_id=result.run_id, items=len(result.items),
        cost_usd=result.cost_usd, cost_per_item=result.cost_per_item,
    )
    return result


async def fetch_dataset(
    client: ApifyClientAsync, dataset_id: str
) -> list[dict[str, Any]]:
    """Read every item from a run's default dataset."""
    items: list[dict[str, Any]] = []
    async for item in client.dataset(dataset_id).iterate_items():
        if isinstance(item, dict):
            items.append(item)
    return items


def _attr(obj: Any, *names: str) -> Any:
    """Read the first present attribute or key.

    The client returns a typed model today, but Apify has moved between typed
    objects and plain dicts across major versions. Two lines here is cheaper
    than an upgrade breaking ingestion silently.
    """
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return None
