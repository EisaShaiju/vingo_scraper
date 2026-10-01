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


def test_settled_event_totals_win_over_recomputing_them():
    """`eventUsage` is what Apify settled on; prefer it to our own arithmetic.

    It also carries events we do not price ourselves -- `apify-actor-start` is
    charged per GB of memory, so multiplying our own table would miss it.
    """
    run = _ppe_run(10, 10)
    run["eventUsage"] = {
        "listing-item": {"eventTotalUsd": 0.0115},
        "listing-item-details": {"eventTotalUsd": 0.0},
        "apify-actor-start": {"eventTotalUsd": 0.00005},
    }
    assert _run_charge_usd(run) == pytest.approx(0.01155)


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


# --------------------------------------------------------------------------
# paying for a page and accepting a handful of it
# --------------------------------------------------------------------------

def test_billed_ceiling_matches_what_a_page_costs():
    """Apify's `max_items` truncates the dataset without reducing the charge.

    Measured on a real run: asked for 8, charged for 23, returned 8. Volume on
    curious_coder is bought by the page, so the clamp must be the page
    capacity -- otherwise we discard listings already paid for.
    """
    from vingo_scraper.apify.client import CURIOUS_CODER, billed_item_ceiling

    assert billed_item_ceiling(8, CURIOUS_CODER) == 25     # one page, 8 asked
    assert billed_item_ceiling(26, CURIOUS_CODER) == 50    # spills to two
    # Never below what was asked for.
    for asked in (1, 5, 8, 20, 24, 25, 40):
        assert billed_item_ceiling(asked, CURIOUS_CODER) >= asked


def test_legacy_actor_keeps_its_native_limit():
    """The legacy actor bounds volume with `resultsLimit`, so leave it alone."""
    from vingo_scraper.apify.client import billed_item_ceiling

    assert billed_item_ceiling(8, "apify/facebook-marketplace-scraper") == 8
