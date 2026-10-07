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
from vingo_scraper.config import CATEGORIES, settings

log = structlog.get_logger(__name__)

# Apify reports these as terminal states.
_SUCCESS = "SUCCEEDED"


class EmptySearchError(ValueError):
    """Neither a category nor a keyword was given.

    Refused rather than defaulted: browsing a whole city with no narrowing is
    an unbounded request on a per-result-billed API, and "I forgot to type
    anything" should not cost a page.
    """


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


# Each Actor has its own input vocabulary. Callers never see this -- they pass
# (search_query, location, max_items) and the mapping happens here, so swapping
# Actors does not move a single call site.
CURIOUS_CODER = "curious_coder/facebook-marketplace"

# Marketplace filters, verified against the LIVE build's input schema
# (`GET /v2/actor-builds/{id}` -> inputSchema), not the Actor's prose docs --
# those have been wrong before on this very Actor.
#
# Note what is NOT here: there is no `categoryIds` parameter. Category is
# expressed through `urls` or `searchKeyword`, so asking for one by id is not
# a thing the Actor can do.
FILTER_KEYS: dict[str, type | tuple] = {
    "sortBy": str,          # '' | creation_time_descend | price_ascend
                            # | price_descend | distance_ascend
    "minPrice": int,
    "maxPrice": int,
    "daysSinceListed": str,  # '' | '1' | '7' | '30'
    "condition": list,
    "availability": str,     # '' | in_stock | out_of_stock
    "deliveryMethod": str,   # '' | local_pickup | shipping
    "radiusKm": int,
    "strictFiltering": bool,
}

# Volume, in the only unit Marketplace actually sells: the PAGE.
#
# A page is ~24 listings and costs ~$0.012. Everything about volume is derived
# from these two constants, because the last time items and pages were computed
# in two places they disagreed -- the API clamped items to 25 before this file
# turned them into pages, so all three options in the Volume selector produced
# one page and every search returned ~24 listings regardless.
LISTINGS_PER_PAGE = 24


def max_pages() -> int:
    """The page ceiling this plan can actually deliver.

    A function, not a constant, because it is a property of the ACCOUNT rather
    than of the code -- and because the constant version made the UI promise
    "3 pages, ~72 listings" on a plan that returns one page whatever it is
    asked for. Measured: 1 page -> 25 items, 3 pages -> 26 items.
    """
    return max(1, settings.apify_max_pages)


def max_items_ceiling() -> int:
    return LISTINGS_PER_PAGE * max_pages()


# Kept for callers that import them directly. These read the configured plan at
# import time, so anything that must follow a runtime change calls the
# functions above instead.
MAX_PAGES = max(1, settings.apify_max_pages)
MAX_ITEMS_CEILING = LISTINGS_PER_PAGE * MAX_PAGES


def pages_for(max_items: int) -> int:
    """How many Marketplace pages are needed to cover `max_items` listings."""
    wanted = max(1, max_items)
    return max(1, min(-(-wanted // LISTINGS_PER_PAGE), max_pages()))


SORT_NEWEST = "creation_time_descend"

# Freshness defaults. The spec wants new inventory, and the default Marketplace
# ordering is relevance -- which surfaces stale listings that have been sitting
# for months. Sorting alone is not enough, so the window is bounded too.
DEFAULT_FILTERS: dict[str, object] = {
    "sortBy": SORT_NEWEST,
    "daysSinceListed": "7",
}

# The Actor accepts only these. There is NO hourly option -- the finest
# Facebook itself offers is one day, so anything tighter has to be applied
# after the fetch, which narrows what we KEEP rather than what we PAY FOR.
DAYS_SINCE_LISTED_CHOICES = ("", "1", "7", "30")

# What the unattended sweep uses. A daily cadence that asked for 7 days would
# re-see six days of listings it already holds on every single run; the Actor
# cache suppresses the charge, but asking for the right window is cheaper and
# clearer than relying on the cache to undo a bad request.
AUTOMATION_FILTERS: dict[str, object] = {
    "sortBy": SORT_NEWEST,
    "daysSinceListed": "1",
}


def merge_filters(overrides: dict | None = None) -> dict:
    """Defaults plus caller overrides, dropping anything the Actor rejects.

    Unknown keys are discarded rather than passed through: Apify fails a run
    outright on an unrecognised input field, and losing a run to a typo in a
    filter name costs the full page charge.
    """
    out = dict(DEFAULT_FILTERS)
    for key, value in (overrides or {}).items():
        if key not in FILTER_KEYS:
            log.warning("apify.filter_ignored", key=key,
                        known=sorted(FILTER_KEYS))
            continue
        if value in (None, "", []):
            # Explicitly clearing a default (e.g. daysSinceListed='') is
            # meaningful, so keep the key with its empty value.
            out[key] = value if key in DEFAULT_FILTERS else None
            continue
        out[key] = value
    return {k: v for k, v in out.items() if v is not None}


def build_run_input(
    search_query: str | None,
    location: str,
    max_items: int,
    *,
    category: str | None = None,
    include_details: bool = True,
    actor_id: str | None = None,
    filters: dict | None = None,
) -> dict[str, Any]:
    """Translate our parameters into the configured Actor's input schema.

    `filters` are Marketplace-level narrowings (sort, price band, recency).
    They go through `merge_filters`, so both the manual panel and the
    automation loop produce identical input for identical intent -- there is
    one builder, not two.
    """
    actor_id = actor_id or settings.apify_actor_id

    if not (search_query or category):
        raise EmptySearchError(
            "give a category or a keyword -- an unnarrowed browse is unbounded"
        )

    if actor_id.startswith(CURIOUS_CODER):
        # Volume is controlled by PAGES here, not items. Using Apify's
        # `max_items` instead would truncate the dataset while the Actor kept
        # scraping and billing -- measured once at 24 charged for 3 returned.
        pages = pages_for(max_items)
        payload: dict[str, Any] = {
            "location": location,
            "maxPagesPerUrl": pages,
            # Spec requires description and ALL photos.
            "getListingDetails": include_details,
            "getAllListingPhotos": True,
        }

        # ONE MODE AT A TIME. The Actor's schema describes two ways in and
        # says plainly that they do not compose: `sortBy` is "ignored when you
        # paste your own URLs", `urls` is "optional if Filters are set",
        # lat/long are "ignored if you paste URLs above".
        #
        # We used to send `urls` AND minPrice/maxPrice/sortBy/daysSinceListed,
        # which put it in URL mode and threw every filter away. A Rs 50,000 cap
        # returned a Rs 9,00,000 Volvo, unsorted.
        merged = merge_filters(filters)

        if category:
            # URL MODE. Everything goes IN the URL, because Facebook applies
            # what is in its own query string and the Actor does not.
            cat = CATEGORIES.get(category, {})
            # Without a verified `category_id` and without a keyword, a search
            # URL carries no narrowing term at all -- an unbounded browse on a
            # per-page-billed API. The category's own words are the documented
            # fallback: weaker than a real category id, and never silent.
            # Facebook's own category URLs pair the id WITH a query term:
            #   ?category_id=1078592699170502&query=Musical%20instruments
            # so we send both, exactly as its sidebar does. A keyword replaces
            # the category's name as the term and scopes the browse; without
            # one, the category's own wording is the term. Either way the URL
            # is never left with nothing to narrow on -- an unnarrowed browse
            # is unbounded on a per-result-billed API.
            term = search_query or cat.get("query") or category.replace("_", " ")
            # PLAIN STRINGS, not [{"url": ...}] -- `urls` is typed as a
            # stringList, and the object form is the legacy Actor's shape.
            payload["urls"] = [search_url(
                location,
                term,
                category_id=cat.get("category_id"),
                min_price=merged.get("minPrice"),
                max_price=merged.get("maxPrice"),
                days_since_listed=merged.get("daysSinceListed"),
                sort_by=merged.get("sortBy"),
                sort_by_new=False,
            )]
            # Deliberately NOT merged in: in URL mode the Actor discards them,
            # and sending them anyway is what made the filters look functional
            # while doing nothing. `strictFiltering` is kept because it is not
            # a search filter -- it narrows how loosely Facebook matches.
            if merged.get("strictFiltering") is not None:
                payload["strictFiltering"] = merged["strictFiltering"]
        else:
            # FILTERS MODE. No `urls` at all, so every filter field applies.
            payload["searchKeyword"] = search_query
            payload.update(merged)

        # Actor-side dedup. Without it a re-run re-scrapes and re-bills every
        # listing we already hold; our database check only avoids the image
        # work. Both keys are in the live build schema.
        if settings.apify_cache_store_id:
            payload["onlyNewListings"] = True
            payload["cacheStorageId"] = settings.apify_cache_store_id

        return payload

    # apify/facebook-marketplace-scraper and compatible.
    if category:
        urls = [category_url(location, category)]
    elif search_query:
        urls = [search_url(location, search_query)]
    else:
        raise ValueError("need either search_query or category")

    return {
        "startUrls": to_start_urls(urls),
        # Never leave unset: unlimited on a per-result-billed Actor is an
        # unbounded invoice.
        "resultsLimit": max_items,
        "includeListingDetails": include_details,
    }


def billed_item_ceiling(
    max_items: int, actor_id: str | None = None, *, accept_full_page: bool | None = None
) -> int:
    """How many items to accept back, given how many we are paying for.

    Apify's run-level `max_items` truncates the DATASET without reducing the
    CHARGE. On curious_coder volume is bought by the page (~24 listings), so
    asking for 8 bills a full page and throws 15 paid listings away. Measured:
    charged for 23, returned 8 -- $0.0115 for what we had costed at $0.004.

    Taking the whole page is therefore free data. It is off by default anyway,
    because ingesting 23 listings measured 60.5s against Vercel's 60s
    `maxDuration` and the function was killed mid-run. Paying for listings we
    drop is wasteful; timing out is broken, and broken is worse.

    Turn `apify_accept_full_page` on where no function timeout applies.
    Either way the real spend ceiling is `max_total_charge_usd`, enforced
    server-side by Apify.
    """
    actor_id = actor_id or settings.apify_actor_id
    if accept_full_page is None:
        accept_full_page = settings.apify_accept_full_page
    if accept_full_page and actor_id.startswith(CURIOUS_CODER):
        return pages_for(max_items) * LISTINGS_PER_PAGE
    return max_items


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
            # Enforced by Apify itself, not the Actor's own code. `max_items`
            # is a backstop only -- it truncates the dataset without reducing
            # the bill, so it is set to what we are paying for rather than to
            # what we asked for. `max_total_charge_usd` is the real ceiling.
            max_items=billed_item_ceiling(max_items, actor_id),
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
    # Charges post AFTER the run finishes. Re-read it, or we record only
    # the events that had settled by the time `.call()` returned.
    result.cost_usd = _run_charge_usd(await _settled_run(client, run, result.run_id))
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


async def start_run(
    search_query: str | None,
    location: str,
    max_items: int = 100,
    *,
    category: str | None = None,
    include_details: bool = True,
    client: ApifyClientAsync | None = None,
    filters: dict | None = None,
) -> ActorRun:
    """Start the Actor and return immediately, without waiting for it.

    `run_actor()` blocks until the Actor finishes, and the Actor's own runtime
    is neither ours nor predictable -- measured between 10.8s and 47.1s for
    identical work. Inside a serverless function with a 60s ceiling that makes
    every tick a coin flip: a 47s run plus ~13s of ingest overruns and the
    function is killed after Apify has already charged us.

    So the wait happens BETWEEN requests instead of inside one. This starts the
    run; `collect_run()` picks it up once it has finished.
    """
    if not settings.apify_api_token and client is None:
        raise ApifyRunError(
            "VINGO_APIFY_API_TOKEN is not set -- copy .env.example to .env"
        )

    max_items = _clamp(max_items)
    actor_id = settings.apify_actor_id
    run_input = build_run_input(
        search_query, location, max_items,
        category=category, include_details=include_details, filters=filters,
    )
    client = client or ApifyClientAsync(token=settings.apify_api_token)
    # Private bookkeeping keys never reach Apify -- one unknown field fails
    # the run outright, after it has already charged.
    run_input = {k: v for k, v in run_input.items() if not k.startswith("_")}

    log.info("apify.run.start_async", actor=actor_id, location=location,
             query=search_query or category, limit=max_items)

    try:
        run = await client.actor(actor_id).start(
            run_input=run_input,
            max_items=billed_item_ceiling(max_items, actor_id),
            max_total_charge_usd=Decimal(str(settings.apify_max_charge_usd)),
            timeout=timedelta(seconds=settings.apify_timeout_s),
        )
    except Exception as exc:
        raise ApifyRunError(f"{type(exc).__name__}: {exc}") from exc

    if run is None:
        raise ApifyRunError("Apify start returned None")

    return ActorRun(
        run_id=_attr(run, "id"),
        actor_id=actor_id,
        status=str(_attr(run, "status") or "RUNNING"),
        run_input=run_input,
        started_at=datetime.now(UTC),
    )


async def collect_run(
    run_id: str,
    *,
    run_input: dict[str, Any] | None = None,
    client: ApifyClientAsync | None = None,
) -> ActorRun | None:
    """Pick up a run started earlier. `None` means it is still going.

    Pairs with `start_run()`. Raises `ApifyRunError` if the run finished in a
    state other than success, so a dead run ends the wait rather than being
    polled forever.
    """
    client = client or ApifyClientAsync(token=settings.apify_api_token)
    run = await client.run(run_id).get()
    if run is None:
        raise ApifyRunError(f"run {run_id} not found")

    status = str(_attr(run, "status") or "UNKNOWN")
    if status in ("READY", "RUNNING"):
        return None

    result = ActorRun(
        run_id=run_id,
        actor_id=str(_attr(run, "act_id", "actId") or settings.apify_actor_id),
        status=status,
        run_input=run_input or {},
        dataset_id=_attr(run, "default_dataset_id", "defaultDatasetId"),
        # The run is already finished here, so charges have settled -- this is
        # the read `run_actor()` has to re-fetch to get right.
        cost_usd=_run_charge_usd(run),
        started_at=_attr(run, "started_at", "startedAt"),
        finished_at=datetime.now(UTC),
    )

    if not result.succeeded:
        result.error = f"run status={status}"
        raise ApifyRunError(f"Apify run {run_id} finished with status {status}")
    if not result.dataset_id:
        raise ApifyRunError(f"run {run_id} succeeded but exposed no dataset")

    result.items = await fetch_dataset(client, result.dataset_id)
    log.info("apify.run.collected", run_id=run_id, items=len(result.items),
             cost_usd=result.cost_usd)
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


def _run_charge_usd(run: Any) -> float | None:
    """What the run actually COST us.

    **The charge settles after the run finishes.** Reading the object the
    instant `.call()` returns gives only the events posted so far. Measured on
    a real run: `apify-actor-start` alone at $0.00005, on a run that went on to
    charge $0.01155. Recording that made `scrape_run.cost_usd` report 0.4% of
    real spend, and the daily cap is built on those rows. Callers must re-read
    the run first -- see `_settled_run`.

    `eventUsage` carries a settled per-event total and is the authority.
    Falling back: multiply `chargedEventCounts` by `pricingInfo`, then
    `usageTotalUsd` -- which on a pay-per-event actor converges to the same
    figure once settled, and is genuinely correct for compute-priced actors.
    """
    usage = _attr(run, "event_usage", "eventUsage")
    if isinstance(usage, dict) and usage:
        total = sum(
            float((v or {}).get("eventTotalUsd", 0.0) or 0.0)
            for v in usage.values()
            if isinstance(v, dict)
        )
        if total:
            return total

    counts = _attr(run, "charged_event_counts", "chargedEventCounts") or {}
    pricing = _attr(run, "pricing_info", "pricingInfo") or {}
    per_event = (
        pricing.get("pricingPerEvent", {}).get("actorChargeEvents", {})
        if isinstance(pricing, dict)
        else {}
    )
    if isinstance(counts, dict) and per_event:
        charged = 0.0
        for event, count in counts.items():
            price = per_event.get(event, {})
            if isinstance(price, dict):
                charged += float(price.get("eventPriceUsd", 0.0) or 0.0) * int(count)
        if charged:
            return charged

    total = _attr(run, "usage_total_usd", "usageTotalUsd")
    return float(total) if total is not None else None


async def _settled_run(
    client: ApifyClientAsync, run: Any, run_id: str | None
) -> Any:
    """Re-read the run so its charges have posted.

    One cheap API call, and the difference between recording $0.00005 and
    $0.01155 for the same run. Failing here is not worth losing the run over --
    fall back to the object already in hand.
    """
    if not run_id:
        return run
    try:
        fresh = await client.run(run_id).get()
        return fresh or run
    except Exception as exc:  # noqa: BLE001 -- cost accuracy is not worth a raise
        log.warning("apify.charge.refetch_failed", run_id=run_id,
                    error=str(exc)[:200])
        return run


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
