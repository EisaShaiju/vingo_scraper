"""Build the Marketplace URLs we hand to the Apify Actor.

The Actor takes `startUrls`, not a search query -- so this is where our
caller-facing (search_query, location) pair becomes something it understands.

Kept in one place because URL shape is the most likely thing to move on us:
if Facebook changes its search path, or we swap to a different Marketplace
Actor, this file is the only edit.
"""

from __future__ import annotations

from urllib.parse import urlencode

BASE = "https://www.facebook.com/marketplace"

# The Actor documents three accepted patterns:
#   /marketplace/<location>/
#   /marketplace/<location>/<category>
#   /marketplace/<location>/search/?query=<term>
# Note the trailing slash before `?query=` -- the Actor's examples use it.
SEARCH = BASE + "/{location}/search/"
CATEGORY = BASE + "/{location}/{category}"
LOCATION = BASE + "/{location}/"


def search_url(
    location: str,
    query: str,
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    days_since_listed: int | None = None,
    sort_by_new: bool = True,
) -> str:
    """URL for a keyword search within one location."""
    params: dict[str, str] = {"query": query}
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if days_since_listed is not None:
        params["daysSinceListed"] = str(days_since_listed)
    if sort_by_new:
        params["sortBy"] = "creation_time_descend"
    return f"{SEARCH.format(location=location)}?{urlencode(params)}"


def category_url(location: str, category: str) -> str:
    """URL for browsing one category within a location."""
    return CATEGORY.format(location=location, category=category)


def to_start_urls(urls: list[str]) -> list[dict[str, str]]:
    """Wrap plain URLs in the `[{"url": ...}]` shape the Actor's input expects."""
    return [{"url": u} for u in urls]
