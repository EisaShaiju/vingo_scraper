"""Fallback extractor: parse listings out of rendered HTML.

This exists purely so a GraphQL shape change degrades us to *worse data*
rather than *no data*. It is deliberately heuristic and structural -- we never
match on Facebook's hashed CSS class names, because those regenerate on every
deploy and would make this fallback as fragile as the thing it is backing up.

Instead we anchor on the one structural invariant that has to keep working for
the site to function at all: a listing is an <a> whose href points at
/marketplace/item/<id>/. Everything else is read out of that anchor's own text.

Two sources are tried, best first:
  1. JSON embedded in the page's own <script> tags (still structured)
  2. anchor-and-text heuristics (last resort)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from bs4 import BeautifulSoup

from vingo_scraper.extract.schema import (
    ExtractionMethod,
    Listing,
    ListingImage,
)
from vingo_scraper.pipeline.normalize import (
    clean_text,
    listing_url,
    parse_price_to_minor,
)

log = structlog.get_logger(__name__)

ITEM_HREF_RE = re.compile(r"/marketplace/item/(\d+)")
# Any of the ways a rupee amount shows up in rendered text.
PRICE_TEXT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s?[\d,]+(?:\.\d{1,2})?|\bFree\b", re.IGNORECASE
)


def parse_html(
    html: str, *, city: str | None = None, category: str | None = None
) -> list[Listing]:
    """Extract listings from rendered search HTML."""
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    listings = _from_embedded_json(soup, city=city, category=category)
    if listings:
        log.info("dom.embedded_json_hit", count=len(listings))
        return listings

    listings = _from_anchors(soup, city=city, category=category)
    log.info("dom.anchor_heuristic", count=len(listings))
    return listings


def _from_embedded_json(
    soup: BeautifulSoup, *, city: str | None, category: str | None
) -> list[Listing]:
    """Reuse the GraphQL parser against JSON blobs inside <script> tags.

    Facebook bootstraps much of the page from JSON it inlines into the
    document. When that is present it is far better data than anything we can
    scrape out of rendered text, and the GraphQL tree-walker already knows how
    to read it.
    """
    from vingo_scraper.extract.graphql import parse_payloads

    blobs: list[dict[str, Any]] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "marketplace_listing_title" not in text and "listing_price" not in text:
            continue
        for candidate in _json_objects_in(text):
            blobs.append(candidate)

    if not blobs:
        return []

    listings = parse_payloads(blobs, city=city, category=category)
    # Re-stamp: these came from the DOM path even though a GraphQL-shaped
    # parser read them, and the monitoring depends on that distinction.
    for listing in listings:
        listing.extraction_method = ExtractionMethod.DOM_FALLBACK
    return listings


def _json_objects_in(text: str) -> list[dict[str, Any]]:
    """Pull balanced top-level JSON objects out of a script body."""
    objects: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : i + 1]
                    # Skip trivial objects; they are never listing payloads.
                    if len(chunk) > 200:
                        try:
                            parsed = json.loads(chunk)
                        except json.JSONDecodeError:
                            pass
                        else:
                            if isinstance(parsed, dict):
                                objects.append(parsed)
                    start = -1
        if len(objects) >= 50:
            break
    return objects


def _from_anchors(
    soup: BeautifulSoup, *, city: str | None, category: str | None
) -> list[Listing]:
    """Last-resort heuristic over /marketplace/item/ anchors."""
    by_id: dict[str, Listing] = {}

    for anchor in soup.find_all("a", href=True):
        match = ITEM_HREF_RE.search(anchor["href"])
        if not match:
            continue
        fb_id = match.group(1)
        if fb_id in by_id:
            continue

        # Each visible text run inside the tile, in DOM order.
        lines = [
            clean_text(s)
            for s in anchor.stripped_strings
        ]
        lines = [line for line in lines if line]
        if not lines:
            continue

        price_minor = None
        price_idx = -1
        for i, line in enumerate(lines):
            if PRICE_TEXT_RE.search(line):
                price_minor = parse_price_to_minor(line)
                price_idx = i
                break

        # FB renders tiles as price, then title, then location. Take the first
        # non-price line after the price as the title; if no price was found,
        # fall back to the longest line, which is almost always the title.
        title = None
        if price_idx >= 0:
            for line in lines[price_idx + 1 :]:
                if not PRICE_TEXT_RE.search(line):
                    title = line
                    break
        if not title:
            non_price = [x for x in lines if not PRICE_TEXT_RE.search(x)]
            title = max(non_price, key=len) if non_price else None
        if not title:
            continue

        location = None
        if title in lines:
            after = lines[lines.index(title) + 1 :]
            if after:
                location = after[-1]

        img = anchor.find("img")
        images = []
        if img and img.get("src", "").startswith("http"):
            images.append(ListingImage(source_url=img["src"], is_primary=True))

        try:
            by_id[fb_id] = Listing(
                fb_listing_id=fb_id,
                listing_url=listing_url(fb_id),
                title=title[:300],
                price_minor=price_minor,
                city=city,
                location_text=location,
                category=category,
                images=images,
                extraction_method=ExtractionMethod.DOM_FALLBACK,
                scraped_at=datetime.now(UTC),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("dom.listing_rejected", fb_id=fb_id, error=str(exc))

    return list(by_id.values())
