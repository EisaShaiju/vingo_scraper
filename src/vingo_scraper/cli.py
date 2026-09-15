"""Command-line entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import structlog

from vingo_scraper.config import CATEGORIES, INDIA_CITIES, settings


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


def login() -> None:
    """Open a headed browser so an operator can authenticate one account."""
    parser = argparse.ArgumentParser(description="Authenticate a scraper account.")
    parser.add_argument("--account", default="default", help="account profile name")
    args = parser.parse_args()

    _setup_logging()
    from vingo_scraper.browser.session import interactive_login

    ok = asyncio.run(interactive_login(args.account))
    sys.exit(0 if ok else 1)


def probe() -> None:
    """Run the Phase 0 density probe."""
    parser = argparse.ArgumentParser(
        description="Phase 0 density probe -- is there enough inventory to seed?"
    )
    parser.add_argument("--account", default="default")
    parser.add_argument(
        "--cities",
        nargs="*",
        default=None,
        help=f"default: {', '.join(INDIA_CITIES.values())}",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help=f"default: {', '.join(CATEGORIES)}",
    )
    parser.add_argument("--scrolls", type=int, default=4)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/density_probe.json"),
    )
    args = parser.parse_args()

    _setup_logging()
    from vingo_scraper.browser.session import LoginRequiredError
    from vingo_scraper.pipeline.probe import run_probe

    try:
        report = asyncio.run(
            run_probe(
                account_id=args.account,
                cities=args.cities,
                categories=args.categories,
                scrolls=args.scrolls,
                output=args.out,
            )
        )
    except LoginRequiredError as exc:
        print(f"\n  {exc}\n")
        sys.exit(2)

    print()
    print(report.to_table())
    print()
    print(f"  Total listings: {report.total_listings}")
    print(f"  Fresh (<=30d):  {report.total_fresh}")
    if report.errors:
        print(f"  Errors:         {len(report.errors)}")
        for err in report.errors[:5]:
            print(f"    - {err}")
    recommendation = report.recommendation()
    print()
    print(f"  {recommendation}")
    print()
    print(f"  Full matrix: {args.out}")
    print()

    # Exit non-zero when the probe could not produce a usable answer, so a
    # scheduled run surfaces as a failure instead of quietly reporting
    # "no inventory" that was really a dead session.
    if recommendation.startswith("BLOCKED"):
        sys.exit(2)
    if recommendation.startswith("NO-GO"):
        sys.exit(3)


def scrape() -> None:
    """Placeholder for the production scrape loop (Phase 5+)."""
    print("Not implemented yet -- run the Phase 0 probe first: vingo-probe")
    sys.exit(1)
