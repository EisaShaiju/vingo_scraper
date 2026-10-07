"""`_listings` executed for real, against rows that are actually in Postgres.

This is bug 2 at its own boundary. `lead_score` read `listing.images`, a lazy
relationship, which raises MissingGreenlet under the async engine. Every
offline test passed: the scorer's tests use a dataclass whose `images` is a
plain list, and every TestClient test monkeypatches `_listings` away entirely,
so the 82-line function containing the real query had never once run.

The offline regression test for this asserts on `inspect.getsource(_listings)`
-- it would pass if someone computed `photo_count` into a variable first, and
fail on a refactor that changed nothing. These replace it with execution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vingo_scraper.db.models import Listing, ListingImage, ListingPriceHistory, Seller
from vingo_scraper.extract.schema import ImageStatus

NOW = datetime.now(UTC)


async def _seed(session, **over) -> Listing:
    """One fully-populated listing: images, price history, stated intent.

    Everything the lead scorer reads, so a lazy load on ANY of it surfaces.
    """
    row = Listing(
        fb_listing_id=over.get("fid", "seed-1"),
        listing_url="https://www.facebook.com/marketplace/item/1",
        title=over.get("title", "Royal Enfield Classic 350"),
        price_minor=over.get("price", 145_000_00),
        description_facts=over.get(
            "facts", {"sale_reason": "relocating", "urgency": "urgent"}
        ),
        quality_score=88,
        posted_at=NOW - timedelta(days=over.get("age_days", 30)),
        outreach_status=over.get("outreach", "new"),
    )
    session.add(row)
    await session.flush()

    for i in range(over.get("photos", 6)):
        session.add(ListingImage(
            listing_id=row.id, source_url=f"https://fbcdn/{i}.jpg",
            source_key=f"/p/{row.fb_listing_id}/{i}.jpg",
            storage_url=f"https://storage.googleapis.com/b/{row.id}/{i}.jpg",
            status=ImageStatus.STORED, is_primary=(i == 0),
        ))
    # Newest LAST here; the relationship serves them DESC, which is the trap
    # `lead_score._price_drop` sorts around.
    for days, price in ((40, 165_000_00), (5, 145_000_00)):
        session.add(ListingPriceHistory(
            listing_id=row.id, price_minor=price, seen_at=NOW - timedelta(days=days)
        ))
    await session.flush()
    return row


# --------------------------------------------------------------------------
# the bug
# --------------------------------------------------------------------------

async def test_01_listings_returns_rows_without_a_lazy_load(session, wire_app) -> None:
    """The exact failure: MissingGreenlet mid-request, swallowed, rendered as
    an empty catalog while the database held 237 listings."""
    await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import _listings
    out = await _listings(60, None)

    assert out.get("db_available") is not False, out.get("error")
    assert out["count"] == 1
    assert out["listings"][0]["title"] == "Royal Enfield Classic 350"


async def test_02_the_lead_score_is_computed_from_real_rows(session, wire_app) -> None:
    await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import _listings
    row = (await _listings(60, None, order="lead"))["listings"][0]

    lead = row["lead"]
    assert lead["score"] > 0 and lead["band"] in {"hot", "warm", "cold"}
    labels = " ".join(s["label"] for s in lead["signals"])
    assert "relocating" in labels          # description_facts survived JSONB
    assert "photos" in labels              # images counted without lazy-loading
    assert "Price cut" in labels           # history read in the right order


async def test_03_the_price_cut_is_not_read_backwards(session, wire_app) -> None:
    """`price_history` is declared order_by seen_at DESC. Reading it in arrival
    order turns a cut into a rise and ranks motivated sellers lowest."""
    await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import _listings
    labels = " ".join(
        s["label"]
        for s in (await _listings(60, None))["listings"][0]["lead"]["signals"]
    )
    assert "Price cut" in labels
    assert "20,000" in labels              # 165,000 -> 145,000, not the reverse


async def test_04_is_servable_does_not_lazy_load_in_a_request(session) -> None:
    """`Listing.is_servable` reads `self.images` -- the same unguarded lazy
    relationship that caused bug 2, still present in the model.

    It is safe only while nothing calls it inside a request without eager
    loading. This asserts the hazard is real rather than theoretical, so the
    next person to use the property knows what it needs.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    row = await _seed(session)
    await session.commit()
    session.expunge_all()

    bare = await session.scalar(select(Listing).where(Listing.id == row.id))
    with pytest.raises(Exception, match="(?i)greenlet|lazy"):
        _ = bare.is_servable

    session.expunge_all()
    eager = await session.scalar(
        select(Listing).where(Listing.id == row.id)
        .options(selectinload(Listing.images))
    )
    assert eager.is_servable is False      # pending_review, so not servable


# --------------------------------------------------------------------------
# the filters the Leads tab depends on
# --------------------------------------------------------------------------

async def test_05_search_narrows_server_side(session, wire_app) -> None:
    await _seed(session, fid="a", title="iPhone 14 Pro")
    await _seed(session, fid="b", title="Royal Enfield")
    await session.commit()

    from vingo_scraper.api.main import _listings
    assert (await _listings(60, None, search="iphone"))["count"] == 1
    assert (await _listings(60, None))["count"] == 2


async def test_06_outreach_filter_narrows(session, wire_app) -> None:
    await _seed(session, fid="a", outreach="new")
    await _seed(session, fid="b", outreach="contacted")
    await session.commit()

    from vingo_scraper.api.main import _listings
    assert (await _listings(60, None, outreach="new"))["count"] == 1
    assert (await _listings(60, None, outreach="contacted"))["count"] == 1


async def test_07_lead_order_puts_the_motivated_seller_first(session, wire_app) -> None:
    await _seed(session, fid="cold", facts={}, age_days=1, photos=2)
    await _seed(session, fid="hot",
                facts={"sale_reason": "needs funds", "urgency": "urgent"},
                age_days=40, photos=8)
    await session.commit()

    from vingo_scraper.api.main import _listings
    out = (await _listings(60, None, order="lead"))["listings"]
    assert out[0]["fb_listing_id"] == "hot"
    assert out[0]["lead"]["score"] > out[1]["lead"]["score"]


async def test_08_offset_paginates(session, wire_app) -> None:
    for i in range(3):
        await _seed(session, fid=f"p{i}")
    await session.commit()

    from vingo_scraper.api.main import _listings
    assert (await _listings(2, None))["count"] == 2
    assert (await _listings(2, None, offset=2))["count"] == 1


# --------------------------------------------------------------------------
# outreach and export, end to end
# --------------------------------------------------------------------------

async def test_09_outreach_persists_and_reads_back(session, wire_app) -> None:
    row = await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import OutreachRequest, set_outreach
    got = await set_outreach(str(row.id), OutreachRequest(
        status="contacted", note="asked for pickup Sat"))
    assert got["status"] == "contacted" and got["contacted_at"]

    from vingo_scraper.api.main import _listings
    back = (await _listings(60, None))["listings"][0]
    assert back["outreach_status"] == "contacted"
    assert back["outreach_note"] == "asked for pickup Sat"


async def test_10_outreach_moves_backwards_too(session, wire_app) -> None:
    """A "replied" that turns out to be a wrong number is "new" again. Forcing
    one-way transitions would make the operator lie to the tool."""
    row = await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import OutreachRequest, _listings, set_outreach
    await set_outreach(str(row.id), OutreachRequest(status="replied"))
    got = await set_outreach(str(row.id), OutreachRequest(status="new"))
    assert got["status"] == "new" and got["contacted_at"] is None
    assert (await _listings(60, None))["listings"][0]["outreach_status"] == "new"


async def test_11_export_streams_a_row_that_is_really_there(session, wire_app) -> None:
    await _seed(session)
    await session.commit()

    from vingo_scraper.api.main import export_csv
    body = (await export_csv()).body.decode("utf-8")
    assert body.splitlines()[0].startswith("lead_score,band,signals,title")
    assert "Royal Enfield Classic 350" in body
    assert "marketplace/item/1" in body       # the contact route must be in it


async def test_12_an_empty_database_is_empty_not_broken(session, wire_app) -> None:
    """The other half of the honesty rule: nothing stored must not look like a
    failure, just as a failure must not look like nothing stored."""
    from vingo_scraper.api.main import _listings
    out = await _listings(60, None)
    assert out["count"] == 0
    assert out.get("error") is None
    assert out.get("db_available") is not False


async def test_13_a_seller_row_does_not_break_the_join(session, wire_app) -> None:
    """`seller` is lazy too, and the endpoint fetches it per row."""
    seller = Seller(fb_seller_id="pfbid0" + "x" * 60, name="A Seller")
    session.add(seller)
    await session.flush()
    row = await _seed(session)
    row.seller_id = seller.id
    await session.commit()

    from vingo_scraper.api.main import _listings
    out = (await _listings(60, None))["listings"][0]
    assert out["seller_name"] == "A Seller"
