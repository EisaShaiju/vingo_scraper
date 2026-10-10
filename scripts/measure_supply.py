"""How many listings can each category actually give us, per day?

Nobody has ever measured this. Every volume number in the project so far has
been an estimate that later turned out wrong -- "Items: 8" bought 24, "3 pages
~72 listings" returned 26 -- so a 1,000-a-day target written against an
unchecked figure would be the same mistake at a larger scale.

One page per category, through the SAME path the console uses
(`start_run` -> `collect_run` -> `ingest_collected`), so the numbers include
the quality gate and the duplicate check rather than raw Actor output.
`runner.ingest()` is deliberately not used: it has its own inline pipeline that
skips those filters, so it would measure something the product never sees.

    python scripts/measure_supply.py --dry-run     # plan + cost, spends nothing
    python scripts/measure_supply.py               # the real probe

The column that decides everything is **dup%**. A category returning 24
listings of which 20 are already held yields 4 new, and a quota that does not
know that will keep paying for the same rows.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from dataclasses import dataclass, field

from vingo_scraper.apify.client import LISTINGS_PER_PAGE
from vingo_scraper.config import CATEGORIES, settings


@dataclass
class Probe:
    """What one category gave back."""

    key: str
    returned: int = 0
    kept: int = 0
    duplicates: int = 0
    rejected: int = 0
    cost_usd: float = 0.0
    lead_scores: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def new_rows(self) -> int:
        """The only number a quota can spend against."""
        return self.kept

    @property
    def dup_pct(self) -> float:
        seen = self.kept + self.duplicates
        return 0.0 if not seen else 100.0 * self.duplicates / seen

    @property
    def cost_per_kept(self) -> float:
        return self.cost_usd / self.kept if self.kept else 0.0

    @property
    def median_lead(self) -> int:
        return int(statistics.median(self.lead_scores)) if self.lead_scores else 0


async def probe_one(key: str, location: str, days: str) -> Probe:
    from vingo_scraper.apify.client import collect_run, start_run
    from vingo_scraper.apify.runner import ingest_collected
    from vingo_scraper.pipeline import lead_score

    out = Probe(key=key)
    try:
        started = await start_run(
            None, location, LISTINGS_PER_PAGE,
            category=key,
            include_details=True,
            # Newest first, bounded window: a supply measurement wants what
            # arrived recently, not whatever has sat on Marketplace longest.
            filters={"daysSinceListed": days, "sortBy": "creation_time_descend"},
        )
        run = await collect_run(started.run_id, run_input=started.run_input)
        if run is None:
            out.error = "run did not finish"
            return out

        result = await ingest_collected(run, location=location, trigger="probe")
        out.returned = result.items_returned
        out.kept = len(result.listings)
        out.duplicates = result.skipped_duplicates
        out.rejected = result.rejected
        out.cost_usd = float(result.cost_usd or 0.0)
        for listing in result.listings:
            out.lead_scores.append(
                lead_score.score_listing(
                    listing, price_history=[],
                    photo_count=len(getattr(listing, "images", []) or []),
                ).score
            )
    except Exception as exc:                    # noqa: BLE001 -- report, never abort
        # One category failing must not lose the measurements already paid for.
        out.error = f"{type(exc).__name__}: {exc}"[:160]
    return out


def render(probes: list[Probe], days: str, location: str) -> str:
    rows = sorted(probes, key=lambda p: p.new_rows, reverse=True)
    total_new = sum(p.new_rows for p in rows)
    total_cost = sum(p.cost_usd for p in rows)

    lines = [
        "<!-- UNTRACKED — internal working doc. Gitignored, not for production. -->",
        "",
        "# Supply baseline — what each category can actually give us",
        "",
        f"**{location}**, one page per category, listed within **{days or 'any'}** days.",
        "Measured through the console's own path, so the numbers are after the",
        "quality gate and the duplicate check — not raw Actor output.",
        "",
        "| category | returned | kept | dup% | median lead | $/kept |",
        "|---|---|---|---|---|---|",
    ]
    for p in rows:
        if p.error:
            lines.append(f"| `{p.key}` | — | — | — | — | **{p.error}** |")
            continue
        lines.append(
            f"| `{p.key}` | {p.returned} | **{p.kept}** | {p.dup_pct:.0f}% "
            f"| {p.median_lead} | ${p.cost_per_kept:.4f} |"
        )

    usable = [p for p in rows if not p.error and p.new_rows >= 10]
    thin = [p for p in rows if not p.error and p.new_rows < 10]

    lines += [
        "",
        f"**{total_new} new listings** across {len(rows)} categories for "
        f"**${total_cost:.4f}**.",
        "",
        "## What this means for a quota",
        "",
        f"- **Can carry a quota ({len(usable)}):** "
        + (", ".join(f"`{p.key}`" for p in usable) or "none"),
        f"- **Too thin to carry one ({len(thin)}):** "
        + (", ".join(f"`{p.key}`" for p in thin) or "none"),
        "",
        "One page is one sample, not a daily rate. A category's real daily yield",
        "needs the same probe repeated across days — this says which categories",
        "are worth that, and which are already exhausted at a single page.",
    ]
    if total_new:
        lines += [
            "",
            f"At this rate 250 new listings needs ~{250 / total_new:.1f}x this sweep",
            f"(~${total_cost * 250 / total_new:.2f}), and 1,000 needs "
            f"~{1000 / total_new:.1f}x (~${total_cost * 1000 / total_new:.2f}).",
            "**Those multiples assume supply that repeats; it may not.**",
        ]
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    import pathlib

    keys = list(CATEGORIES)
    est = len(keys) * LISTINGS_PER_PAGE * 0.0014      # measured $/item

    print(f"{len(keys)} categories x 1 page ({LISTINGS_PER_PAGE} listings)")
    print(f"location={args.location}  daysSinceListed={args.days or 'any'}")
    print(f"estimated cost ~${est:.2f}  (cap ${settings.total_spend_cap_usd})")

    if args.dry_run:
        print("\n--dry-run: nothing was sent to Apify.")
        for k in keys:
            print(f"  {k:<22} {CATEGORIES[k]['query']}")
        return 0

    probes = []
    for i, key in enumerate(keys, 1):
        print(f"  [{i}/{len(keys)}] {key} ...", end="", flush=True)
        p = await probe_one(key, args.location, args.days)
        probes.append(p)
        print(f" {p.kept} kept / {p.returned} returned"
              if not p.error else f" FAILED: {p.error}")

    out = pathlib.Path(args.out)
    out.write_text(render(probes, args.days, args.location), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--location", default="bangalore")
    ap.add_argument("--days", default="7", help="'' | 1 | 7 | 30")
    ap.add_argument("--out", default="docs/supply-baseline.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from vingo_scraper.db.session import ensure_compatible_event_loop
    ensure_compatible_event_loop()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
