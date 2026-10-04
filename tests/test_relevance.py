"""Search results must match the search.

The case that prompted this: searching **cycles** returned MacBook Pro,
MacBook Air and iPhone 16 Pro Max alongside the bicycles. Facebook matches a
keyword against the whole listing including the description, and laptop
listings say "battery cycle count: 120". Facebook considers those genuine
matches; we do not.
"""

from __future__ import annotations

from vingo_scraper.extract.schema import ExtractionMethod, Listing, ListingImage
from vingo_scraper.pipeline.relevance import (
    filter_relevant,
    title_matches,
)


def L(title: str, description: str = "", fid: str = "1") -> Listing:
    return Listing(
        fb_listing_id=fid, listing_url="https://fb.com/marketplace/item/1",
        title=title, description=description, price_minor=500_000,
        images=[ListingImage(source_url="https://x/1.jpg")],
        extraction_method=ExtractionMethod.APIFY,
    )


# --------------------------------------------------------------------------
# the real case
# --------------------------------------------------------------------------

def test_01_a_macbook_is_not_a_cycle():
    """The exact listing that appeared in a `cycles` search."""
    macbook = L("MacBook Pro 14inch 16/512 with good condition",
                "Excellent condition. Battery cycle count 120. Boxed.")
    assert title_matches(macbook.title, "cycles") is False


def test_02_a_cycle_is_a_cycle():
    assert title_matches("Kids Cycle for sale", "cycles") is True


def test_03_bicycle_matches_cycle():
    """Substring, so compounds work without a word list."""
    assert title_matches("BICYCLE WITH AIR PUMP", "cycles") is True


def test_04_the_whole_mixed_result_set_is_separated():
    """Reproduces the screenshot: four bikes, three laptops/phones."""
    items = [
        L("MacBook Pro 14inch 16/512", "battery cycle count 120", "1"),
        L("Kids Cycle for sale", "", "2"),
        L("Apple Macbook air M1 8gb 256gb SSD", "250 cycles only", "3"),
        L("Iphone 16pro max", "charge cycles low", "4"),
        L("BICYCLE WITH AIR PUMP", "", "5"),
        L("Begasso Soldier Foldable bicycle for sale", "", "6"),
        L("Urban Terrain bycycle", "", "7"),
    ]
    kept, report = filter_relevant(items, "cycles")

    assert report.dropped == 3
    assert {x.fb_listing_id for x in kept} == {"2", "5", "6", "7"}
    # The titles it dropped are reported, so an over-aggressive filter shows up
    # rather than silently eating half the results.
    assert any("MacBook" in e for e in report.examples)


# --------------------------------------------------------------------------
# matching behaviour
# --------------------------------------------------------------------------

def test_05_case_is_ignored():
    assert title_matches("XBOX SERIES S", "xbox") is True


def test_06_plurals_match_singular():
    assert title_matches("Office chair", "chairs") is True


def test_07_any_word_matches_not_all():
    """"xbox series s" should still find a plain Xbox controller."""
    assert title_matches("Xbox wireless controller", "xbox series s") is True


def test_08_stopwords_alone_do_not_filter():
    """A query of only common words must not empty the result set."""
    items = [L("Anything at all", fid="1")]
    kept, report = filter_relevant(items, "for sale")
    assert len(kept) == 1 and report.dropped == 0


def test_09_very_short_words_are_ignored():
    """`s` in "xbox series s" must not match every listing containing an s."""
    assert title_matches("Sony Bravia TV", "xbox series s") is False


def test_10_unrelated_listing_is_dropped():
    assert title_matches("Sony Bravia 55-inch 4K Smart TV", "xbox") is False


# --------------------------------------------------------------------------
# where it must NOT apply
# --------------------------------------------------------------------------

def test_11_a_category_browse_is_not_filtered():
    """No keyword, nothing to second-guess -- the URL already asked precisely."""
    items = [L("Anything", fid=str(i)) for i in range(3)]
    kept, report = filter_relevant(items, None)
    assert len(kept) == 3 and report.dropped == 0


def test_12_the_filter_runs_before_rehosting():
    """A dropped listing must cost no image bandwidth."""
    import inspect

    from vingo_scraper.apify import runner
    src = inspect.getsource(runner.ingest_collected)
    assert src.index("filter_relevant") < src.index("rehost_all")


def test_13_it_is_opt_out_from_the_api():
    """Off gives Facebook's raw relevance, for a deliberately loose search."""
    from vingo_scraper.api.main import IngestRequest
    assert IngestRequest().title_match is True
    assert IngestRequest(title_match=False).title_match is False


def test_14_facebook_is_asked_to_be_strict_too():
    """Upstream filtering costs nothing and reduces what we pay to receive."""
    from vingo_scraper.api.main import IngestRequest
    assert IngestRequest().to_filters()["strictFiltering"] is True
    assert IngestRequest(strict=False).to_filters()["strictFiltering"] is None
