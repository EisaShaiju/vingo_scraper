"""Parse Facebook Marketplace payloads into Listings.

Input is whatever carries Facebook's own listing field names. Today that is
an Apify dataset item; it was previously an intercepted GraphQL response.
Both work unchanged, because the Actor returns Facebook's field names
verbatim (marketplace_listing_title, listing_price, primary_listing_photo,
marketplace_listing_seller, location.reverse_geocode).

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

import re
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
# NOTE: the Apify Actor returns camelCase (listingTitle, listingPrice), while
# Facebook's own payloads use snake_case (marketplace_listing_title). The
# Actor's published docs advertise the snake_case names but its dataset does
# not use them -- verified against a real run. Both dialects are listed here so
# either source parses.
_TITLE_KEYS = (
    "marketplace_listing_title",
    "listingTitle",
    "custom_title",
    "listing_title",
    "title",
)
_PRICE_CONTAINER_KEYS = (
    "listing_price",
    "listingPrice",
    "formatted_price",
    "price",
    "current_price",
)

# Fields that look like minor-unit amounts but are NOT the listing's price.
# `amount_with_offset_in_currency` is a USD-cents conversion: a Rs 40,000 item
# reports 41526. Using it would price everything at ~1/100th and look
# plausible, so it is denied explicitly rather than merely not-preferred.
_PRICE_DENY = frozenset({"amount_with_offset_in_currency"})
_ID_KEYS = ("id", "story_key", "legacy_id", "listing_id")

_TYPENAME_HINTS = ("marketplacelisting", "groupcommercelisting")

_MAX_DEPTH = 40


def parse_payloads(
    payloads: list[Any],
    *,
    city: str | None = None,
    category: str | None = None,
    method: ExtractionMethod = ExtractionMethod.APIFY,
) -> list[Listing]:
    """Extract every listing found across a set of captured payloads."""
    listings: dict[str, Listing] = {}
    for payload in payloads:
        body = getattr(payload, "body", payload)
        for node in _walk_for_listings(body):
            listing = _node_to_listing(
                node, city=city, category=category, method=method
            )
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
    """Pull a price from whichever shape the source used.

    `amount` (major units) is the ONLY field trusted outright. Every
    `amount_with_offset*` variant is treated as suspect, because the name does
    not mean the same thing across sources:

      apify/facebook-marketplace-scraper
        amount "40000.00"  amount_with_offset_in_currency "41526"   <- USD cents
      curious_coder/facebook-marketplace
        amount "78000.00"  amount_with_offset "80792"               <- ALSO USD cents

    That second one is the dangerous case: `amount_with_offset` is normally
    INR minor units, and reading it as such turned Rs 78,000 into Rs 807.92 --
    wrong by ~100x and entirely plausible on a listings page.

    So: use `amount`, and only fall back to an offset field when `amount` is
    absent AND the offset agrees with the formatted display string.
    """
    for key in _PRICE_CONTAINER_KEYS:
        raw = node.get(key)
        if raw is None:
            continue

        if not isinstance(raw, dict):
            if (parsed := parse_price_to_minor(raw)) is not None:
                return parsed
            continue

        # 1. `amount` is major units and authoritative.
        if (amount := raw.get("amount")) is not None:
            if (parsed := parse_price_to_minor(amount)) is not None:
                return parsed

        # 2. Otherwise the formatted display string -- it is what the seller
        #    sees, so it cannot be silently in another currency.
        for sub in ("formatted_amount_zeros_stripped", "formatted_amount", "text"):
            if (value := raw.get(sub)) is not None:
                if (parsed := parse_price_to_minor(value)) is not None:
                    return parsed

        # 3. Offsets last, and only if one corroborates the other. Two
        #    independent fields agreeing is weak evidence, but better than
        #    trusting a name we have twice seen lie.
        offset = raw.get("amount_with_offset")
        if offset is not None and str(offset) != str(raw.get("amount_with_offset_in_currency")):
            try:
                return int(str(offset))
            except (TypeError, ValueError):
                pass
    return None


def _extract_images(node: dict[str, Any]) -> list[ListingImage]:
    urls: list[str] = []

    def collect(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(urls) >= 12:
            return
        if isinstance(value, dict):
            uri = (
                value.get("uri")
                or value.get("url")
                or value.get("src")
                # Actor-specific: primaryListingPhoto.photo_image_url
                or value.get("photo_image_url")
            )
            if isinstance(uri, str) and uri.startswith("http") and uri not in urls:
                urls.append(uri)
            for sub in value.values():
                collect(sub, depth + 1)
        elif isinstance(value, list):
            for item in value:
                collect(item, depth + 1)

    for key in (
        "primary_listing_photo",
        "primaryListingPhoto",
        "listing_photos",
        "listingPhotos",
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


# Indian mobile numbers, and email. Applied ONLY to seller-authored text.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")


def extract_contacts(*texts: str | None) -> tuple[str | None, str | None]:
    """Find a phone/email the seller chose to publish in their own listing.

    Deliberately scoped to title and description only -- never ids or URLs.
    Facebook's 16-digit listing ids contain valid-looking 10-digit
    substrings, so scanning raw payloads invents phone numbers that were
    never there.
    """
    blob = " ".join(t for t in texts if t)
    phone = _PHONE_RE.search(blob)
    email = _EMAIL_RE.search(blob)
    return (
        phone.group(0).strip() if phone else None,
        email.group(0).strip() if email else None,
    )


def _extract_seller(node: dict[str, Any]) -> Seller | None:
    """Seller identity, where Facebook exposes it.

    Measured coverage on a real 47-listing Bengaluru capture: 3/47 (6.4%).
    Facebook nulls this for most anonymous reads, so absence is normal and
    must not be treated as a parse failure.
    """
    phone, email = extract_contacts(
        _extract_description(node),
        _first_present(node, _TITLE_KEYS),
    )

    for key in ("marketplace_listing_seller", "seller", "owner", "actor"):
        raw = node.get(key)
        if not isinstance(raw, dict):
            continue
        seller_id = raw.get("id")
        name = clean_text(raw.get("name"))
        if not (seller_id or name):
            continue
        return Seller(
            fb_seller_id=str(seller_id) if seller_id else None,
            name=name,
            # `pfbid...` ids resolve as a normal profile path.
            profile_url=raw.get("url")
            or (f"https://www.facebook.com/{seller_id}" if seller_id else None),
            phone=phone,
            email=email,
            rating=_as_float(raw.get("seller_rating") or node.get("sellerRating")),
            rating_count=_as_int(
                raw.get("rating_count") or node.get("sellerRatingCount")
            ),
        )

    # No identity, but the seller may still have published a contact.
    if phone or email:
        return Seller(phone=phone, email=email)
    return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _extract_location(node: dict[str, Any]) -> str | None:
    for key in (
        "locationText",
        "location_text",
        "location_vanity_or_city_and_state",
        "location",
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


def _first_str(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = node.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return None


def _extract_attributes(node: dict[str, Any]) -> dict[str, str]:
    """Flatten the attribute list into {name: label}.

    This is where the spec's "any other details (brand, size, model)" lives,
    and on some sources it is also the only place `condition` appears.
    """
    out: dict[str, str] = {}
    for key in ("attribute_data", "listing_attributes", "listingAttributes"):
        raw = node.get(key)
        if not isinstance(raw, list):
            continue
        for attr in raw:
            if not isinstance(attr, dict):
                continue
            name = attr.get("attribute_name") or attr.get("name")
            value = attr.get("label") or attr.get("value")
            if name and value:
                out[str(name)] = str(value)
    return out


def _extract_condition(node: dict[str, Any]) -> str | None:
    """Condition is top-level on some sources, inside attributes on others."""
    direct = node.get("condition") or node.get("item_condition")
    if isinstance(direct, str) and direct.strip():
        return direct
    attrs = _extract_attributes(node)
    for key in ("Condition", "condition"):
        if key in attrs:
            return attrs[key]
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
        "timestamp",
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
    node: dict[str, Any],
    *,
    city: str | None,
    category: str | None,
    method: ExtractionMethod = ExtractionMethod.APIFY,
) -> Listing | None:
    raw_id = _first_present(node, _ID_KEYS)
    title = _first_present(node, _TITLE_KEYS)
    if not raw_id or not title:
        return None

    fb_id = str(raw_id)

    try:
        return Listing(
            fb_listing_id=fb_id,
            listing_url=(
                node.get("itemUrl")
                or node.get("listingUrl")
                or node.get("url")
                or listing_url(fb_id)
            ),
            title=clean_text(title, limit=300) or "",
            description=_extract_description(node),
            price_minor=_extract_price(node),
            condition=parse_condition(_extract_condition(node)),
            city=city,
            location_text=_extract_location(node),
            category=category,
            fb_category_id=_first_str(
                node, ("marketplace_listing_category_id", "listingCategoryId",
                       "category_id", "categoryId")
            ),
            attributes=_extract_attributes(node),
            is_sold=bool(node.get("is_sold") or node.get("isSold")),
            is_pending=bool(node.get("is_pending") or node.get("isPending")),
            is_hidden=bool(node.get("is_hidden") or node.get("isHidden")),
            seller=_extract_seller(node),
            images=_extract_images(node),
            posted_at=_extract_posted_at(node),
            extraction_method=method,
            scraped_at=datetime.now(UTC),
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("graphql.listing_rejected", fb_id=fb_id, error=str(exc))
        return None
