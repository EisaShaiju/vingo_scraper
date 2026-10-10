"""Ingest Apify runs that were paid for but never collected.

Written because the first bulk sweep started 17 Actor runs, was charged ~$1 for
them, and discarded every result: `collect_run` returns None while a run is
still RUNNING, and the sweep treated that as failure instead of polling.

The runs succeeded and their datasets are still on Apify. Collecting them now
costs **nothing** — Apify charges when the Actor scrapes, not when we read the
dataset — so this recovers the spend rather than repeating it.

    python scripts/recover_runs.py --since 2h --dry-run
    python scripts/recover_runs.py --since 2h

Safe to re-run: ingestion dedups on `fb_listing_id`, so a listing already
stored is skipped rather than duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta


def _cutoff(spec: str) -> datetime:
    unit, amount = spec[-1], float(spec[:-1])
    hours = {"h": amount, "d": amount * 24, "m": amount / 60}[unit]
    return datetime.now(UTC) - timedelta(hours=hours)


async def recover(since: str, dry_run: bool, location: str) -> int:
    from apify_client import ApifyClientAsync

    from vingo_scraper.apify.client import collect_run
    from vingo_scraper.apify.runner import ingest_collected
    from vingo_scraper.config import settings

    cutoff = _cutoff(since)
    client = ApifyClientAsync(token=settings.apify_api_token)
    page = await client.runs().list(desc=True, limit=50)

    candidates = []
    for item in page.items:
        started = item.get("startedAt")
        if isinstance(started, str):
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if started and started < cutoff:
            continue
        if item.get("status") != "SUCCEEDED":
            continue
        candidates.append(item["id"])

    print(f"{len(candidates)} succeeded run(s) since {since}")
    if dry_run:
        print("--dry-run: nothing ingested, nothing charged either way.")
        return 0

    created = skipped = failed = 0
    for i, run_id in enumerate(candidates, 1):
        try:
            run = await collect_run(run_id)
            if run is None:
                print(f"  [{i}/{len(candidates)}] {run_id} still running, skipped")
                continue
            # trigger='bulk' so these land in the Bulk tab, which is where the
            # sweep that paid for them was meant to put them.
            result = await ingest_collected(run, location=location, trigger="bulk")
            created += result.created
            skipped += result.skipped_duplicates
            print(f"  [{i}/{len(candidates)}] {run_id}: "
                  f"{result.created} new, {result.skipped_duplicates} already held")
        except Exception as exc:                    # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(candidates)}] {run_id} FAILED: "
                  f"{type(exc).__name__}: {exc}"[:140])

    print(f"\nrecovered {created} new listings · {skipped} already held "
          f"· {failed} failed · $0.00 additional spend")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2h", help="e.g. 90m, 2h, 1d")
    ap.add_argument("--location", default="bangalore")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from vingo_scraper.db.session import ensure_compatible_event_loop
    ensure_compatible_event_loop()
    return asyncio.run(recover(args.since, args.dry_run, args.location))


if __name__ == "__main__":
    raise SystemExit(main())
