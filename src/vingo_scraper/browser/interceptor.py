"""Passive capture of Facebook's GraphQL responses.

This is the heart of the design. Rather than parsing obfuscated markup (the
approach that got the well-known open-source scrapers archived), we let the
real page make its own authenticated requests and listen to the structured
JSON it receives.

Why passive capture rather than calling /api/graphql/ ourselves:
  - each query needs a `doc_id` that rotates every few weeks
  - each request needs a session-bound `fb_dtsg` CSRF token
  - both are derived from page bootstrap payloads that also change

Letting the page do the work means we inherit all of that for free, and a
doc_id rotation costs us nothing. FB's internal *field names* track their data
model and move far more slowly than CSS class names do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from playwright.async_api import Page, Response

log = structlog.get_logger(__name__)

GRAPHQL_PATH = "/api/graphql/"


@dataclass
class CapturedPayload:
    """One decoded GraphQL response body."""

    url: str
    friendly_name: str | None
    body: dict[str, Any]


@dataclass
class GraphQLCapture:
    """Collects GraphQL payloads seen while a page is driven.

    Attach before navigating, then read `.payloads` afterwards.
    """

    payloads: list[CapturedPayload] = field(default_factory=list)
    _seen: set[int] = field(default_factory=set)

    async def _on_response(self, response: Response) -> None:
        if GRAPHQL_PATH not in response.url:
            return
        try:
            text = await response.text()
        except Exception:
            # Body already consumed or the request was aborted -- not fatal.
            return

        for chunk in _split_json_stream(text):
            marker = id(chunk)
            if marker in self._seen:
                continue
            self._seen.add(marker)
            self.payloads.append(
                CapturedPayload(
                    url=response.url,
                    friendly_name=_friendly_name(chunk),
                    body=chunk,
                )
            )

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def detach(self, page: Page) -> None:
        try:
            page.remove_listener("response", self._on_response)
        except Exception:
            pass

    def clear(self) -> None:
        self.payloads.clear()
        self._seen.clear()

    def matching(self, *needles: str) -> list[CapturedPayload]:
        """Payloads whose friendly name or raw body mentions any needle.

        Matching on substrings rather than an exact operation name is
        intentional: FB renames operations more often than it restructures
        the data underneath them.
        """
        lowered = [n.lower() for n in needles]
        out = []
        for p in self.payloads:
            name = (p.friendly_name or "").lower()
            if any(n in name for n in lowered):
                out.append(p)
                continue
            try:
                raw = json.dumps(p.body)[:20_000].lower()
            except (TypeError, ValueError):
                continue
            if any(n in raw for n in lowered):
                out.append(p)
        return out


def _split_json_stream(text: str) -> list[dict[str, Any]]:
    """Decode a GraphQL response body.

    Facebook streams multi-part responses as newline-delimited JSON objects
    (one per deferred fragment), so a plain json.loads() on the whole body
    fails and would silently lose every deferred chunk. Parse the whole body
    first, then fall back to line-by-line.
    """
    text = text.strip()
    if not text:
        return []

    # FB prefixes some responses with an anti-JSON-hijacking guard.
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass

    chunks: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            chunks.append(obj)
        elif isinstance(obj, list):
            chunks.extend(o for o in obj if isinstance(o, dict))
    return chunks


def _friendly_name(body: dict[str, Any]) -> str | None:
    """Best-effort operation name; FB includes it inconsistently."""
    for key in ("label", "friendly_name", "operationName"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    extensions = body.get("extensions")
    if isinstance(extensions, dict):
        for key in ("operationName", "friendly_name"):
            value = extensions.get(key)
            if isinstance(value, str) and value:
                return value
    return None
