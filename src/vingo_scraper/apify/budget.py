"""Budget guard.

Spend is read from **Apify's own accounting**, not from our `scrape_run` rows.
That matters: our database has already been lost once, and a cap that resets
when you lose your database is not a cap. Apify's number is also the one that
actually exhausts the free tier.

Two ceilings, both enforced before any run starts:

  * `total_spend_cap_usd` -- the hard stop (e.g. $1.00 of a $5 free tier),
    leaving headroom for other experiments.
  * `daily_spend_cap_usd` -- secondary, stops one bad day eating the lot.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from vingo_scraper.config import settings

log = structlog.get_logger(__name__)

# (fetched_at, monthly_used_usd, plan_limit_usd)
_CACHE: tuple[datetime, float, float | None] | None = None
_CACHE_TTL_S = 20


@dataclass
class BudgetStatus:
    monthly_used_usd: float
    total_cap_usd: float
    daily_used_usd: float
    daily_cap_usd: float
    monthly_limit_usd: float | None = None
    error: str | None = None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.total_cap_usd - self.monthly_used_usd)

    @property
    def pct_used(self) -> float:
        if self.total_cap_usd <= 0:
            return 100.0
        return min(100.0, 100.0 * self.monthly_used_usd / self.total_cap_usd)

    @property
    def exhausted(self) -> bool:
        return self.monthly_used_usd >= self.total_cap_usd

    @property
    def daily_exhausted(self) -> bool:
        return self.daily_used_usd >= self.daily_cap_usd

    def can_afford(self, cost_usd: float) -> bool:
        return (
            self.monthly_used_usd + cost_usd <= self.total_cap_usd
            and self.daily_used_usd + cost_usd <= self.daily_cap_usd
        )

    def blocked_reason(self, cost_usd: float = 0.0) -> str | None:
        """Why a run cannot proceed, phrased for the console."""
        if self.exhausted:
            return (
                f"Budget cap reached — ${self.monthly_used_usd:.4f} of "
                f"${self.total_cap_usd:.2f} spent on Apify. Automation stopped."
            )
        if self.daily_exhausted:
            return (
                f"Daily cap reached — ${self.daily_used_usd:.4f} of "
                f"${self.daily_cap_usd:.2f} today. Resumes after UTC midnight."
            )
        if cost_usd and not self.can_afford(cost_usd):
            return (
                f"Next run costs ${cost_usd:.4f}; only "
                f"${self.remaining_usd:.4f} left under the "
                f"${self.total_cap_usd:.2f} cap. Stopping rather than exceed it."
            )
        return None

    def to_dict(self) -> dict:
        return {
            "monthly_used_usd": round(self.monthly_used_usd, 4),
            "total_cap_usd": self.total_cap_usd,
            "remaining_usd": round(self.remaining_usd, 4),
            "pct_used": round(self.pct_used, 1),
            "exhausted": self.exhausted,
            "daily_used_usd": round(self.daily_used_usd, 4),
            "daily_cap_usd": self.daily_cap_usd,
            "monthly_limit_usd": self.monthly_limit_usd,
            "blocked_reason": self.blocked_reason(),
            "error": self.error,
        }


def _fetch_apify_usage() -> tuple[float, float | None]:
    """(used this month, plan limit) straight from Apify."""
    global _CACHE
    now = datetime.now(UTC)
    if _CACHE and (now - _CACHE[0]).total_seconds() < _CACHE_TTL_S:
        # Return the cached LIMIT too. Dropping it made the console's
        # "free tier $X/mo" figure appear and vanish between polls.
        return _CACHE[1], _CACHE[2]

    req = urllib.request.Request(
        "https://api.apify.com/v2/users/me/limits",
        headers={"Authorization": f"Bearer {settings.apify_api_token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)["data"]

    used = float(data.get("current", {}).get("monthlyUsageUsd", 0.0) or 0.0)
    raw_limit = data.get("limits", {}).get("maxMonthlyUsageUsd")
    limit = float(raw_limit) if raw_limit is not None else None
    _CACHE = (now, used, limit)
    return used, limit


async def daily_spend_usd() -> float:
    """Spend recorded by us today. Secondary to Apify's monthly figure."""
    from sqlalchemy import func, select

    from vingo_scraper.db import models
    from vingo_scraper.db.session import session_scope

    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with session_scope() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(models.ScrapeRun.cost_usd), 0.0)).where(
                    models.ScrapeRun.started_at >= midnight
                )
            )
        return float(total or 0.0)
    except Exception:
        # No database is not permission to spend freely -- the monthly cap
        # from Apify still applies and is the one that matters.
        return 0.0


async def get_budget() -> BudgetStatus:
    daily = await daily_spend_usd()
    try:
        used, limit = _fetch_apify_usage()
        return BudgetStatus(
            monthly_used_usd=used,
            total_cap_usd=settings.total_spend_cap_usd,
            daily_used_usd=daily,
            daily_cap_usd=settings.daily_spend_cap_usd,
            monthly_limit_usd=limit,
        )
    except Exception as exc:
        log.error("budget.fetch_failed", error=str(exc)[:200])
        # Fail CLOSED: if we cannot read spend, we do not spend. An unreadable
        # balance is exactly when an unattended loop does the most damage.
        return BudgetStatus(
            monthly_used_usd=settings.total_spend_cap_usd,
            total_cap_usd=settings.total_spend_cap_usd,
            daily_used_usd=daily,
            daily_cap_usd=settings.daily_spend_cap_usd,
            error=f"could not read Apify usage: {type(exc).__name__}",
        )


def invalidate_cache() -> None:
    """Force the next read to hit Apify -- call right after a run spends."""
    global _CACHE
    _CACHE = None


def next_reset() -> datetime:
    """Approximate start of Apify's next monthly cycle."""
    now = datetime.now(UTC)
    return (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=32)).replace(day=1)
