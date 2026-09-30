"""Command-line entry points."""

from __future__ import annotations

import argparse
import logging
import sys

import structlog

from vingo_scraper.config import settings


def _setup_logging() -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=settings.log_level
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )


def scrape() -> None:
    """Trigger an Apify run, map results, rehost images, persist."""
    parser = argparse.ArgumentParser(
        description="Scrape Facebook Marketplace via Apify and ingest to Supabase."
    )
    parser.add_argument("--query", required=True, help="search term")
    parser.add_argument("--location", required=True, help="Marketplace location slug")
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="skip per-listing detail fetch (cheaper, but no description)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and map, but do not write to the database or storage",
    )
    args = parser.parse_args()

    _setup_logging()

    # Imported lazily so --help works without credentials configured.
    from vingo_scraper.apify.runner import run_ingest

    raise SystemExit(
        run_ingest(
            search_query=args.query,
            location=args.location,
            max_items=args.max_items,
            include_details=not args.no_details,
            dry_run=args.dry_run,
        )
    )


def console() -> None:
    """Serve the one-page ingestion console."""
    parser = argparse.ArgumentParser(description="Vingo ingestion console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    _setup_logging()

    import asyncio

    import uvicorn

    from vingo_scraper.api.main import app
    from vingo_scraper.db.session import ensure_compatible_event_loop

    # psycopg3 async refuses to run on Windows' ProactorEventLoop. Setting the
    # policy is not enough on its own: uvicorn.run() builds its own loop
    # afterwards and wins. So we construct the loop ourselves via asyncio.run()
    # (which honours the policy) and drive the server inside it.
    ensure_compatible_event_loop()

    config = uvicorn.Config(
        app, host=args.host, port=args.port,
        log_level=settings.log_level.lower(), loop="asyncio",
    )
    server = uvicorn.Server(config)

    print(f"\n  Vingo console -> http://{args.host}:{args.port}\n")
    asyncio.run(server.serve())
