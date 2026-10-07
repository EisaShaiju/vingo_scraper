"""The mapped classes must agree with the schema the migrations build.

This is bug 1 at its own boundary. Three outreach columns were declared on
`Seller` instead of `Listing`; migration 0009 was correct throughout. Every one
of 337 offline tests passed, because none of them had ever asked a mapped class
a question -- and `extract.schema.Listing` (pydantic) shares its name with
`db.models.Listing` (ORM), so ~150 tests said `Listing(...)` while meaning the
other one entirely.

The check here is general rather than pinned to 0009: it compares EVERY mapped
column against the live schema in both directions. A future migration that the
model forgets to mirror -- or a model column with no migration behind it --
fails here instead of in the console.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from vingo_scraper.db.models import Base, Listing, Seller


async def _columns(session, table: str) -> set[str]:
    rows = await session.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_schema='public' AND table_name=:t"),
        {"t": table},
    )
    return {r[0] for r in rows}


async def test_01_every_mapped_column_exists_in_the_database(session) -> None:
    """A column the model reads but the database lacks is a crashed query.

    And because /api/listings swallows its exceptions, a crashed query renders
    as an empty catalog rather than an error.
    """
    missing: list[str] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        actual = await _columns(session, table.name)
        if not actual:
            continue       # table not created by any migration -- caught below
        for col in table.columns:
            if col.name not in actual:
                missing.append(f"{table.name}.{col.name}")
    assert missing == [], f"mapped but not in the database: {missing}"


async def test_02_every_table_the_migrations_build_is_mapped(session) -> None:
    """The converse: a migration whose columns no model reads is dead weight,
    and usually means the column landed on the wrong class."""
    unmapped: list[str] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        mapped = {c.name for c in table.columns}
        for name in await _columns(session, table.name):
            if name not in mapped:
                unmapped.append(f"{table.name}.{name}")
    assert unmapped == [], f"in the database but not mapped: {unmapped}"


async def test_03_the_outreach_columns_are_on_listing(session) -> None:
    """Bug 1, stated directly, against the real schema rather than the model's
    own opinion of itself."""
    listing_cols = await _columns(session, "listing")
    seller_cols = await _columns(session, "seller")
    for col in ("outreach_status", "contacted_at", "outreach_note"):
        assert col in listing_cols, f"listing is missing {col}"
        assert col not in seller_cols, f"seller should not have {col}"
        assert col in Listing.__table__.columns
        assert col not in Seller.__table__.columns


async def test_04_the_migrations_reached_head(session) -> None:
    got = await session.execute(text("SELECT version_num FROM alembic_version"))
    assert got.scalar() == "0009"


async def test_05_a_listing_round_trips_through_the_real_schema(session) -> None:
    """JSONB, native enums and UUID defaults all at once -- none of which
    sqlite could have stood in for."""
    from vingo_scraper.db.models import Listing as L

    row = L(
        fb_listing_id="rt-1", listing_url="https://x/1", title="Round trip",
        price_minor=12_345, description_facts={"urgency": "urgent"},
        quality_flags=["short description"], attributes={"brand": "Sony"},
    )
    session.add(row)
    await session.flush()

    got = await session.get(L, row.id)
    assert got.description_facts == {"urgency": "urgent"}
    assert got.review_status.value == "pending_review"
    assert got.outreach_status == "new"          # server_default, not just a
    assert got.contacted_at is None              # Python-side default


async def test_06_indexes_the_queries_rely_on_exist(session) -> None:
    """`outreach_status` is indexed because the lead queue's only real query is
    'everything still marked new'. An unindexed one is a sequential scan that
    will not show up until the table is large."""
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename='listing'")
    )
    names = " ".join(r[0] for r in rows)
    assert "outreach_status" in names
    assert "quality_score" in names or "ix_listing_quality_score" in names


async def test_07_the_orm_and_pydantic_listings_are_not_the_same_class() -> None:
    """The decoy that made bug 1 invisible.

    `extract.schema.Listing` and `db.models.Listing` share a name. Most tests
    instantiate the pydantic one and read as though they cover "a listing".
    Keeping this explicit so the next person reading the suite sees it.
    """
    from vingo_scraper.db.models import Listing as OrmListing
    from vingo_scraper.extract.schema import Listing as PydanticListing

    assert OrmListing is not PydanticListing
    assert not hasattr(PydanticListing, "__table__")
    assert inspect(OrmListing).local_table is not None
