"""Phase 0 density probe.

Answers one question before any infrastructure money is spent: is there enough
fresh Facebook Marketplace inventory in Vingo's target cities and categories to
seed a catalog at all?

Marketplace India launched as a limited trial and never reached OLX/Quikr
density, so this is a real risk, not a formality. If the answer is no, the
right move is to take the matrix to the client and re-scope -- not to build a
better scraper for an empty shelf.

Output is a JSON matrix plus a readable summary table.

NOTE: the browser-driven runner was removed in the Apify pivot. The metrics
and go/no-go thresholds below are source-agnostic -- they reduce a list of
Listings to a decision -- so an Apify-backed runner drops straight in.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vingo_scraper.extract.schema import Listing

FRESH_DAYS = 30


@dataclass
class CellMetrics:
    """One city x category cell of the density matrix."""

    city: str
    category: str
    total: int = 0
    fresh_30d: int = 0
    unknown_age: int = 0
    median_price_inr: float | None = None
    pct_with_photo: float = 0.0
    pct_with_description: float = 0.0
    pct_with_price: float = 0.0
    used_fallback: bool = False
    error: str | None = None


def summarize(listings: list[Listing], *, city: str, category: str) -> CellMetrics:
    """Reduce a set of listings to the metrics the go/no-go decision needs."""
    metrics = CellMetrics(city=city, category=category, total=len(listings))
    if not listings:
        return metrics

    cutoff = datetime.now(UTC) - timedelta(days=FRESH_DAYS)
    for listing in listings:
        if listing.posted_at is None:
            metrics.unknown_age += 1
        elif listing.posted_at >= cutoff:
            metrics.fresh_30d += 1

    prices = [x.price_major for x in listings if x.price_minor not in (None, 0)]
    if prices:
        metrics.median_price_inr = round(statistics.median(prices), 2)

    n = len(listings)
    metrics.pct_with_photo = round(100 * sum(bool(x.images) for x in listings) / n, 1)
    metrics.pct_with_description = round(
        100 * sum(bool(x.description) for x in listings) / n, 1
    )
    metrics.pct_with_price = round(
        100 * sum(x.price_minor is not None for x in listings) / n, 1
    )
    return metrics


@dataclass
class ProbeReport:
    started_at: str
    cities: list[str]
    categories: list[str]
    cells: list[CellMetrics] = field(default_factory=list)
    total_listings: int = 0
    total_fresh: int = 0
    fallback_cells: int = 0
    errors: list[str] = field(default_factory=list)

    def recommendation(self, target_catalog: int = 5000) -> str:
        """A blunt go/no-go, stated in terms the client can act on."""
        if self.errors and not self.total_listings:
            return (
                "BLOCKED - no data collected. Resolve session/login errors "
                "before drawing any conclusion about density."
            )

        # Observed unique listings across one pass of each cell. Real daily
        # yield is higher (repeat passes surface new inventory), but this is
        # the honest floor to plan against.
        if self.total_listings == 0:
            return "NO-GO - zero listings found. Marketplace inventory is absent here."

        fresh_share = (
            100 * self.total_fresh / self.total_listings if self.total_listings else 0
        )
        per_cell = self.total_listings / max(len(self.cells), 1)

        if per_cell < 10:
            return (
                f"NO-GO - only ~{per_cell:.0f} listings per city/category. "
                "Too thin to seed a catalog. Recommend pivoting to "
                "price-intelligence (aggregates only), which also removes the "
                "republishing risk."
            )
        if per_cell < 30 or fresh_share < 25:
            return (
                f"MARGINAL - ~{per_cell:.0f} listings per cell, {fresh_share:.0f}% "
                f"fresh within {FRESH_DAYS}d. Viable only for a narrow launch in "
                "the strongest cells. Take the matrix to the client before "
                "committing to infrastructure."
            )
        return (
            f"GO - ~{per_cell:.0f} listings per cell, {fresh_share:.0f}% fresh "
            f"within {FRESH_DAYS}d. Density supports a seeded catalog; size the "
            f"{target_catalog:,}-listing target against the per-cell numbers below."
        )

    def to_table(self) -> str:
        header = (
            f"{'City':<12} {'Category':<16} {'Total':>6} {'Fresh':>6} "
            f"{'Median Rs':>11} {'Photo%':>7} {'Desc%':>7} {'Price%':>7}"
        )
        lines = [header, "-" * len(header)]
        for cell in self.cells:
            median = (
                f"{cell.median_price_inr:,.0f}"
                if cell.median_price_inr is not None
                else "-"
            )
            flag = " *" if cell.used_fallback else ""
            lines.append(
                f"{cell.city:<12} {cell.category:<16} {cell.total:>6} "
                f"{cell.fresh_30d:>6} {median:>11} {cell.pct_with_photo:>7} "
                f"{cell.pct_with_description:>7} {cell.pct_with_price:>7}{flag}"
            )
        if self.fallback_cells:
            lines.append("")
            lines.append(
                f"  * {self.fallback_cells} cell(s) fell back to DOM extraction "
                "-- GraphQL shape may have drifted."
            )
        return "\n".join(lines)
