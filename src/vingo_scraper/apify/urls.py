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
    query: str | None = None,
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    days_since_listed: int | str | None = None,
    sort_by: str | None = None,
    category_id: str | None = None,
    sort_by_new: bool = True,
) -> str:
    """URL for a search within one location, with every filter embedded.

    Embedded rather than passed as Actor fields, and that is the whole point.
    The Actor has TWO input modes and its schema says so: `sortBy` is
    "ignored when you paste your own URLs", `urls` is "optional if Filters are
    set", `latitude`/`longitude` are "ignored if you paste URLs above". Supply
    `urls` and every Filters-mode field is discarded.

    We were sending both. A run capped at Rs 50,000 came back with a
    Rs 9,00,000 Volvo, sorted by nothing in particular. Putting the filters in
    the URL hands them to Facebook itself, which is the one party that
    certainly applies them.
    """
    params: dict[str, str] = {}
    if query:
        params["query"] = query
    if category_id:
        params["category_id"] = str(category_id)
    if min_price is not None:
        params["minPrice"] = str(min_price)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if days_since_listed not in (None, ""):
        params["daysSinceListed"] = str(days_since_listed)
    if sort_by:
        params["sortBy"] = sort_by
    elif sort_by_new:
        params["sortBy"] = "creation_time_descend"
    return f"{SEARCH.format(location=location)}?{urlencode(params)}"


def category_url(location: str, category: str) -> str:
    """URL for browsing one category within a location."""
    return CATEGORY.format(location=location, category=category)


def to_start_urls(urls: list[str]) -> list[dict[str, str]]:
    """Wrap plain URLs in the `[{"url": ...}]` shape the Actor's input expects."""
    return [{"url": u} for u in urls]
