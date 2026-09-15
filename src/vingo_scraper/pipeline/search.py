"""Drive a Marketplace search and harvest listings from intercepted traffic.

Flow per search:
  1. attach the GraphQL capture to a fresh page
  2. navigate to the search URL (raises if we hit a login wall)
  3. scroll to trigger FB's own pagination requests
  4. parse everything the capture collected
  5. fall back to DOM parsing if GraphQL yielded nothing

Step 5 is what keeps the scraper alive through a GraphQL shape change: we
degrade to worse data instead of to no data, and the extraction_method on each
row tells us it happened.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

import structlog
from playwright.async_api import BrowserContext

from vingo_scraper.browser.interceptor import GraphQLCapture
from vingo_scraper.browser.session import goto_checked, human_delay
from vingo_scraper.config import CATEGORIES, MARKETPLACE_SEARCH
from vingo_scraper.extract import graphql as gql
from vingo_scraper.extract.schema import Listing

log = structlog.get_logger(__name__)


@dataclass
class SearchResult:
    city: str
    category: str
    listings: list[Listing] = field(default_factory=list)
    payload_count: int = 0
    scrolls: int = 0
    used_fallback: bool = False
    error: str | None = None
    duration_s: float = 0.0


def build_search_url(
    city: str,
    query: str,
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    days_since_listed: int | None = None,
    sort_by_new: bool = True,
) -> str:
    """Compose a Marketplace search URL.

    Isolated here because query-parameter names are one of the three things
    that break when FB ships a change (the others being the GraphQL shape and
    the DOM). One place to fix.
    """
    params: dict[str, str] = {"query": query}
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if days_since_listed is not None:
        params["daysSinceListed"] = str(days_since_listed)
    if sort_by_new:
        params["sortBy"] = "creation_time_descend"
    return f"{MARKETPLACE_SEARCH.format(city=city)}?{urlencode(params)}"


async def _human_scroll(page, times: int) -> int:
    """Scroll in irregular increments to trigger pagination fetches.

    Irregular by design: a fixed scroll step at a fixed interval is a machine
    signature, and this is the most-observed part of the session.
    """
    done = 0
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(600, 1400))
        await asyncio.sleep(random.uniform(1.2, 2.8))
        done += 1
    return done


async def search_city_category(
    context: BrowserContext,
    city: str,
    category: str,
    *,
    scrolls: int = 4,
    days_since_listed: int | None = None,
) -> SearchResult:
    """Run one city x category search and return everything harvested."""
    started = datetime.now(UTC)
    result = SearchResult(city=city, category=category)

    spec = CATEGORIES.get(category)
    query = spec["query"] if spec else category

    page = await context.new_page()
    capture = GraphQLCapture()
    capture.attach(page)

    try:
        url = build_search_url(city, query, days_since_listed=days_since_listed)
        log.info("search.start", city=city, category=category, url=url)

        await goto_checked(page, url)
        # Let the first batch of GraphQL requests settle before scrolling.
        await human_delay(2.0, 4.0)

        result.scrolls = await _human_scroll(page, scrolls)
        await human_delay(1.5, 3.0)

        result.payload_count = len(capture.payloads)
        result.listings = gql.parse_payloads(
            capture.payloads, city=city, category=category
        )

        if not result.listings:
            # GraphQL gave us nothing. Either the search genuinely had no
            # results, or the payload shape moved. Try the DOM before
            # concluding the former -- and flag it either way.
            log.warning(
                "search.graphql_empty",
                city=city,
                category=category,
                payloads=result.payload_count,
            )
            from vingo_scraper.extract import dom

            html = await page.content()
            result.listings = dom.parse_html(html, city=city, category=category)
            result.used_fallback = True

        log.info(
            "search.done",
            city=city,
            category=category,
            listings=len(result.listings),
            payloads=result.payload_count,
            fallback=result.used_fallback,
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("search.failed", city=city, category=category, error=result.error)
    finally:
        capture.detach(page)
        await page.close()
        result.duration_s = (datetime.now(UTC) - started).total_seconds()

    return result
