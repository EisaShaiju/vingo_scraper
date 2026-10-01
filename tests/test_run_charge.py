"""What a run COST us, as opposed to what it used.

`usageTotalUsd` is platform usage -- compute, storage, transfer. On a
pay-per-event actor it is close to nothing: it read $0.0001 for a run that
charged about $0.015. Recording that as the cost made `scrape_run.cost_usd`
report roughly 1% of real spend, and the daily cap is built on those rows.
"""

from __future__ import annotations

import pytest

from vingo_scraper.apify.client import _run_charge_usd


class _Run(dict):
    """Apify has shipped both plain dicts and typed objects; the reader
    handles either, so the fixtures are dicts with attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _ppe_run(listing_count: int, detail_count: int = 0, usage: float = 0.0001):
    return _Run(
        usageTotalUsd=usage,
        chargedEventCounts={"listing": listing_count, "listing-details": detail_count},
        pricingInfo={
            "pricingPerEvent": {
                "actorChargeEvents": {
                    "listing": {"eventPriceUsd": 0.0005},
                    "listing-details": {"eventPriceUsd": 0.0010},
                }
            }
        },
    )


def test_event_charge_beats_usage():
    """The regression: a 10-listing run charged ~$0.015, not $0.0001."""
    charge = _run_charge_usd(_ppe_run(10, 10))
    assert charge == pytest.approx(0.015)
    assert charge > 100 * 0.0001  # emphatically not the usage figure


def test_details_cost_more_than_listings_alone():
    assert _run_charge_usd(_ppe_run(10, 10)) > _run_charge_usd(_ppe_run(10, 0))


def test_pretotalled_field_wins_when_present():
    run = _ppe_run(10, 10)
    run["chargedTotalUsd"] = 0.0199
    assert _run_charge_usd(run) == pytest.approx(0.0199)


def test_falls_back_to_usage_for_compute_priced_actors():
    """Rental actors genuinely bill on usage -- there it is the right answer."""
    assert _run_charge_usd(_Run(usageTotalUsd=0.42)) == pytest.approx(0.42)


def test_missing_everything_is_none_not_zero():
    """Unknown cost must not read as free -- `None` and `0.0` differ."""
    assert _run_charge_usd(_Run()) is None


def test_zero_charged_events_falls_through_rather_than_reporting_free():
    """A run that charged nothing yet should report usage, not a bare 0."""
    run = _ppe_run(0, 0, usage=0.0003)
    assert _run_charge_usd(run) == pytest.approx(0.0003)
