"""Does asking for a second and third page actually buy anything?

`quota.MAX_PAGES_PER_CELL = 3` has never been measured. Its only empirical
anchor is that it happens to equal `settings.apify_max_pages`, the plan ceiling
-- which IS measured, but is a different quantity. One is "how many pages will
Apify deliver", the other is "how many pages are worth buying". Nobody has
checked the second.

This reads runs already paid for, so it costs **nothing**. Every
`scrape_run` row records what it asked for (`run_input.resultsLimit`) and what
came back (`items_returned`, `items_ingested`), which is exactly the pair
needed.

    python scripts/analyze_page_yield.py
    python scripts/analyze_page_yield.py --trigger bulk --days 7

Three things it answers:

  1. **Delivery.** Does a 3-page request return ~72 items, or does the plan cap
     it at ~24 regardless? This project has already shipped a bug where every
     volume option bought one page and nobody noticed for weeks.
  2. **Marginal gross.** Items per page at 1, 2 and 3 pages. If page 3 returns
     as many as page 1, Marketplace still had depth there.
  3. **Marginal NET.** Net-new per page, after de-duplication. This is the only
     number a quota can spend against, and it is the one that decides whether 3
     is too high, too low, or accidentally right.

What it CANNOT answer: whether 4 or 5 pages would keep paying, because nothing
has ever requested them. That needs a deliberate probe -- see `--suggest`.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

LISTINGS_PER_PAGE = 24


def _bucket(results_limit: int | None) -> int | None:
    """Pages this run asked for, from the limit it sent."""
    if not results_limit:
        return None
    return max(1, -(-int(results_limit) // LISTINGS_PER_PAGE))   # ceil


async def analyze(trigger: str | None, days: int) -> int:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from vingo_scraper.db import models
    from vingo_scraper.db.session import session_scope

    cutoff = datetime.now(UTC) - timedelta(days=days)

    async with session_scope() as s:
        q = select(models.ScrapeRun).where(models.ScrapeRun.started_at >= cutoff)
        if trigger:
            q = q.where(models.ScrapeRun.trigger == trigger)
        runs = list(await s.scalars(q))

    # (pages asked) -> list of (gross returned, net ingested, cost)
    by_pages: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    unusable = 0
    for r in runs:
        pages = _bucket((r.run_input or {}).get("resultsLimit"))
        if pages is None:
            unusable += 1
            continue
        by_pages[pages].append(
            (r.items_returned or 0, r.items_ingested or 0, float(r.cost_usd or 0.0))
        )

    print(f"{len(runs)} runs in the last {days}d"
          + (f" (trigger={trigger})" if trigger else "")
          + f", {unusable} without a recorded resultsLimit\n")

    if not by_pages:
        print("No runs carry `resultsLimit`. Nothing to measure.")
        return 1

    print(f"{'asked':>6} {'runs':>5} {'gross':>7} {'gross/pg':>9} "
          f"{'net':>6} {'net/pg':>7} {'dup%':>6} {'$/net':>8}")
    print("-" * 62)

    rows = []
    for pages in sorted(by_pages):
        samples = by_pages[pages]
        n = len(samples)
        gross = sum(x[0] for x in samples) / n
        net = sum(x[1] for x in samples) / n
        cost = sum(x[2] for x in samples) / n
        dup = 0.0 if not gross else 100.0 * (gross - net) / gross
        per_net = cost / net if net else 0.0
        rows.append((pages, n, gross, net))
        print(f"{pages:>6} {n:>5} {gross:>7.1f} {gross / pages:>9.1f} "
              f"{net:>6.1f} {net / pages:>7.1f} {dup:>5.0f}% {per_net:>8.4f}")

    print()

    # The verdict, stated rather than left for the reader to infer.
    one = next((r for r in rows if r[0] == 1), None)
    if one is None:
        print("No single-page runs to compare against; verdict needs a baseline.")
        return 0

    base_gross, base_net = one[2], one[3]
    print(f"Baseline: a 1-page run returns {base_gross:.1f} gross, "
          f"{base_net:.1f} net.\n")

    for pages, n, gross, net in rows:
        if pages == 1:
            continue
        if n < 3:
            print(f"  {pages} pages: only {n} run(s) -- too few to conclude from.")
            continue
        delivered = gross / base_gross
        if delivered < 1.3:
            print(f"  {pages} pages returned {gross:.1f} gross vs {base_gross:.1f} "
                  f"for one -- the plan is NOT delivering the extra pages. "
                  f"Paying for them is waste.")
        elif net / pages < base_net * 0.5:
            print(f"  {pages} pages: net/page fell to {net / pages:.1f} from "
                  f"{base_net:.1f}. Depth is exhausted before page {pages}; "
                  f"MAX_PAGES_PER_CELL is too HIGH.")
        else:
            print(f"  {pages} pages: net/page held at {net / pages:.1f} vs "
                  f"{base_net:.1f}. Still productive at page {pages} -- "
                  f"MAX_PAGES_PER_CELL may be too LOW.")

    top = max(rows, key=lambda r: r[0])[0]
    print(f"\nNothing has ever requested more than {top} page(s), so this says "
          f"nothing about {top + 1}+.")
    print("To settle that, raise VINGO_APIFY_MAX_PAGES and probe one deep cell:")
    print("  cost per extra page ~ $0.015; a 1/3/5/7 ladder on one keyword "
          "is ~16 pages, ~$0.25")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trigger", default=None,
                    help="bulk | manual | automation | probe (default: all)")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    from vingo_scraper.db.session import ensure_compatible_event_loop
    ensure_compatible_event_loop()
    return asyncio.run(analyze(args.trigger, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
