"""Primary extractor: parse intercepted GraphQL payloads into Listings.

Durability strategy -- we do NOT hardcode a path like
    body["data"]["marketplace_search"]["feed_units"]["edges"][...]

Facebook restructures wrappers (renaming connections, inserting pagination
layers, moving results under a new feed key) far more often than it renames the
leaf fields on the listing object itself. So we recursively walk the payload and
pick out anything that *looks like* a listing. A wrapper rename then costs us
nothing, and only a genuine change to the listing model requires a code change.

Leaf field names are tried in preference order, so an added alias does not
break us either.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from vingo_scraper.extract.schema import (
    ExtractionMethod,
    Listing,
    ListingImage,
    Seller,
)
from vingo_scraper.pipeline.normalize import (
    clean_text,
    listing_url,
    parse_condition,
    parse_price_to_minor,
    parse_timestamp,
)

log = structlog.get_logger(__name__)

# A node is listing-shaped if it has an id plus a title plus a price container.
_TITLE_KEYS = (
    "marketplace_listing_title",
    "custom_title",
    "listing_title",
    "title",
)
_PRICE_CONTAINER_KEYS = (
    "listing_price",
    "formatted_price",
    "price",
    "current_price",
)
_ID_KEYS = ("id", "story_key", "legacy_id", "listing_id")

_TYPENAME_HINTS = ("marketplacelisting", "groupcommercelisting")

_MAX_DEPTH = 40


def parse_payloads(
    payloads: list[Any],
    *,
    city: str | None = None,
    category: str | None = None,
) -> list[Listing]:
    """Extract every listing found across a set of captured payloads."""
    listings: dict[str, Listing] = {}
    for payload in payloads:
        body = getattr(payload, "body", payload)
        for node in _walk_for_listings(body):
            listing = _node_to_listing(node, city=city, category=category)
            if listing and listing.fb_listing_id not in listings:
                listings[listing.fb_listing_id] = listing
    return list(listings.values())


def _walk_for_listings(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Depth-first search for listing-shaped dicts anywhere in the tree."""
    found: list[dict[str, Any]] = []
    if depth > _MAX_DEPTH:
        return found

    if isinstance(node, dict):
        if _is_listing_node(node):
            found.append(node)
            # Do not recurse into a matched listing: nested related-items
            # would otherwise be harvested as if they were search results.
            return found
        for value in node.values():
            found.extend(_walk_for_listings(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_for_listings(item, depth + 1))
    return found


def _is_listing_node(node: dict[str, Any]) -> bool:
    typename = str(node.get("__typename", "")).lower()
    if any(hint in typename for hint in _TYPENAME_HINTS):
        return True
    if not _first_present(node, _ID_KEYS):
        return False
    has_title = _first_present(node, _TITLE_KEYS) is not None
    has_price = any(k in node for k in _PRICE_CONTAINER_KEYS)
    # Require both, so we do not match every id-bearing node in the graph.
    return has_title and has_price


def _first_present(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _extract_price(node: dict[str, Any]) -> int | None:
    """Pull a price from whichever shape FB used.

    Seen in the wild: {"amount": "12500"}, {"amount_with_offset": "1250000"},
    {"formatted_amount": "Rs 12,500"}, or a bare string/number.
    """
    for key in _PRICE_CONTAINER_KEYS:
        raw = node.get(key)
        if raw is None:
            continue
        if isinstance(raw, dict):
            # amount_with_offset is already in minor units -- use it directly
            # rather than round-tripping through a float.
            offset = raw.get("amount_with_offset")
            if offset is not None:
                try:
                    return int(str(offset))
                except (TypeError, ValueError):
                    pass
            for sub in (
                "amount",
                "formatted_amount",
                "text",
                "formatted_amount_zeros_stripped",
            ):
                if (value := raw.get(sub)) is not None:
                    if (parsed := parse_price_to_minor(value)) is not None:
                        return parsed
        else:
            if (parsed := parse_price_to_minor(raw)) is not None:
                return parsed
    return None


def _extract_images(node: dict[str, Any]) -> list[ListingImage]:
    urls: list[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(urls) >= 12:
            return
        if isinstance(value, dict):
            uri = value.get("uri") or value.get("url") or value.get("src")
            if isinstance(uri, str) and uri.startswith("http") and uri not in urls:
                urls.append(uri)
            for sub in value.values():
                collect(sub, depth + 1)
        elif isinstance(value, list):
            for item in value:
                collect(item, depth + 1)

    for key in (
        "primary_listing_photo",
        "listing_photos",
        "photos",
        "image",
        "primary_photo",
        "thumbnail",
    ):
        if key in node:
            collect(node[key])

    return [
        ListingImage(source_url=url, is_primary=(i == 0)) for i, url in enumerate(urls)
    ]


def _extract_seller(node: dict[str, Any]) -> Seller | None:
    for key in ("marketplace_listing_seller", "seller", "owner", "actor"):
        raw = node.get(key)
        if isinstance(raw, dict):
            seller_id = raw.get("id")
            name = clean_text(raw.get("name"))
            if seller_id or name:
                return Seller(
                    fb_seller_id=str(seller_id) if seller_id else None,
                    name=name,
                    profile_url=raw.get("url")
                    or (
                        f"https://www.facebook.com/{seller_id}" if seller_id else None
                    ),
                )
    return None


def _extract_location(node: dict[str, Any]) -> str | None:
    for key in (
        "location_text",
        "location_vanity_or_city_and_state",
        "location",
        "locationText",
    ):
        raw = node.get(key)
        if isinstance(raw, str) and raw.strip():
            return clean_text(raw)
        if isinstance(raw, dict):
            for sub in ("text", "name", "reverse_geocode", "city"):
                value = raw.get(sub)
                if isinstance(value, str) and value.strip():
                    return clean_text(value)
                if isinstance(value, dict):
                    for deep in ("city", "text", "name", "city_page_name"):
                        if isinstance(value.get(deep), str):
                            return clean_text(value[deep])
    return None


def _extract_description(node: dict[str, Any]) -> str | None:
    for key in (
        "marketplace_listing_description",
        "description",
        "redacted_description",
    ):
        raw = node.get(key)
        if isinstance(raw, dict):
            raw = raw.get("text")
        if isinstance(raw, str) and raw.strip():
            return clean_text(raw, limit=4000)
    return None


def _extract_posted_at(node: dict[str, Any]) -> datetime | None:
    for key in (
        "creation_time",
        "created_time",
        "listing_creation_time",
        "story_time",
    ):
        if key in node:
            if parsed := parse_timestamp(node.get(key)):
                return parsed
    return None


def _node_to_listing(
    node: dict[str, Any], *, city: str | None, category: str | None
) -> Listing | None:
    raw_id = _first_present(node, _ID_KEYS)
    title = _first_present(node, _TITLE_KEYS)
    if not raw_id or not title:
        return None

    fb_id = str(raw_id)

    try:
        return Listing(
            fb_listing_id=fb_id,
            listing_url=node.get("url") or listing_url(fb_id),
            title=clean_text(title, limit=300) or "",
            description=_extract_description(node),
            price_minor=_extract_price(node),
            condition=parse_condition(
                node.get("condition") or node.get("item_condition")
            ),
            city=city,
            location_text=_extract_location(node),
            category=category,
            seller=_extract_seller(node),
            images=_extract_images(node),
            posted_at=_extract_posted_at(node),
            extraction_method=ExtractionMethod.GRAPHQL,
            scraped_at=datetime.now(UTC),
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("graphql.listing_rejected", fb_id=fb_id, error=str(exc))
        return None
