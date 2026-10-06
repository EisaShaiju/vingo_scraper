"""SQLAlchemy models. Mirrors docs/data-model.md -- keep them in step."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from vingo_scraper.extract.schema import (
    Condition,
    ExtractionMethod,
    ImageStatus,
    ReviewStatus,
)


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


# Store enums by value, not by Python member name, so the DB holds 'pending'
# rather than 'PENDING' and stays readable in psql / the Supabase UI.
def _enum(py_enum, name: str) -> Enum:
    return Enum(
        py_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=True,
    )


class Seller(Base):
    """Deliberately minimal.

    This is personal data under India's DPDP Act, so we keep only what is
    needed to attribute a listing and link back. No phone numbers, no emails.
    Do not widen without a decision on record.
    """

    __tablename__ = "seller"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # 255, not 64: a `pfbid` is ~73 characters and overflowed the original
    # column, killing the first run that ever returned seller identity.
    # Facebook documents no upper bound, so this is headroom, not a known fit.
    fb_seller_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    profile_url: Mapped[str | None] = mapped_column(Text)

    # Contact details, when the seller published them in their own listing
    # text. Personal data under the DPDP Act: store the minimum, never log in
    # plaintext, never expose client-side.
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))

    rating: Mapped[float | None] = mapped_column(Float)
    rating_count: Mapped[int | None] = mapped_column(Integer)

    # Outreach state. On the LISTING rather than the seller because seller
    # identity is absent on ~94% of rows -- see migration 0009.
    outreach_status: Mapped[str] = mapped_column(
        String(16), default="new", server_default="new", index=True
    )
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outreach_note: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    listings: Mapped[list[Listing]] = relationship(back_populates="seller")


class ScrapeRun(Base):
    """One Apify run. Audit trail and cost evidence.

    Written for failures too -- a run that produced nothing is exactly the
    thing you want a record of later.
    """

    __tablename__ = "scrape_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    actor_id: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # What started it: manual | automation | cron. The console's automation
    # feed filters on this -- without it a manual run shows up among the
    # category ticks and reads as something automation scraped.
    trigger: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    run_input: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    items_returned: Mapped[int] = mapped_column(Integer, default=0)
    items_ingested: Mapped[int] = mapped_column(Integer, default=0)
    images_stored: Mapped[int] = mapped_column(Integer, default=0)
    images_failed: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    listings: Mapped[list[Listing]] = relationship(back_populates="scrape_run")


class Listing(Base):
    __tablename__ = "listing"
    __table_args__ = (
        Index("ix_listing_city_category", "city", "category"),
        Index("ix_listing_review_status", "review_status"),
        Index("ix_listing_last_seen", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

    # Natural key from Facebook. Upserts target this.
    fb_listing_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    listing_url: Mapped[str] = mapped_column(Text)

    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    # Paise. NULL means "could not read a price"; 0 means genuinely free.
    # These are different and must never be conflated.
    price_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    condition: Mapped[Condition] = mapped_column(
        _enum(Condition, "condition"), default=Condition.UNKNOWN
    )

    city: Mapped[str | None] = mapped_column(String(64))
    location_text: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64))

    # Facebook's own category id, plus the free-form attributes shown on the
    # listing (brand, size, model...). Spec: "any other details shown".
    fb_category_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Scored by `pipeline.quality` BEFORE images are fetched, so a low-effort
    # listing costs no bandwidth. Flagged, never auto-discarded -- the reject
    # rate is what tells us whether the thresholds are set right.
    quality_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    quality_flags: Mapped[list] = mapped_column(JSONB, default=list)

    # Specs / condition nuances / sale reason pulled out of freeform text by
    # `extract.description`. Sparse by nature: most sellers write very little.
    description_facts: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Spec: daily runs must track sold status.
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)

    seller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seller.id", ondelete="SET NULL")
    )
    scrape_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scrape_run.id", ondelete="SET NULL")
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        _enum(ExtractionMethod, "extraction_method"), default=ExtractionMethod.APIFY
    )

    # Nothing is buyer-visible until a human approves it.
    review_status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus, "review_status"), default=ReviewStatus.PENDING_REVIEW
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    seller: Mapped[Seller | None] = relationship(back_populates="listings")
    scrape_run: Mapped[ScrapeRun | None] = relationship(back_populates="listings")
    images: Mapped[list[ListingImage]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    price_history: Mapped[list[ListingPriceHistory]] = relationship(
        back_populates="listing", cascade="all, delete-orphan",
        order_by="ListingPriceHistory.seen_at.desc()",
    )

    @property
    def is_servable(self) -> bool:
        """Approved, and has at least one image we host ourselves."""
        return self.review_status is ReviewStatus.APPROVED and any(
            img.status is ImageStatus.STORED for img in self.images
        )


class ListingImage(Base):
    __tablename__ = "listing_image"
    __table_args__ = (
        # One row per (listing, image content). Re-ingesting the same listing
        # updates in place instead of accumulating duplicates.
        UniqueConstraint("listing_id", "source_key", name="uq_listing_image_src"),
        Index("ix_listing_image_status", "status"),
        Index("ix_listing_image_sha", "sha256"),
        Index("ix_listing_image_source_key", "source_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("listing.id", ondelete="CASCADE"), index=True
    )

    # The original fbcdn url. Provenance and retry ONLY -- it expires, so it
    # must never be handed to a client.
    source_url: Mapped[str] = mapped_column(Text)

    # Stable per-photo identity (fbcdn URL path). Dedupe key for re-ingest --
    # sha256 cannot serve that role because FB returns different derivatives.
    source_key: Mapped[str | None] = mapped_column(Text)

    # Ours. The only url anything downstream may render.
    storage_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ImageStatus] = mapped_column(
        _enum(ImageStatus, "image_status"), default=ImageStatus.PENDING
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    sha256: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    listing: Mapped[Listing] = relationship(back_populates="images")


class ListingPriceHistory(Base):
    """One row per observed price change.

    Spec: "runs every day, adds new listings, and updates old ones (price
    changes, sold status)". An upsert alone loses the previous value, so a
    change is recorded here before the listing row is overwritten.
    """

    __tablename__ = "listing_price_history"
    __table_args__ = (Index("ix_price_history_listing", "listing_id", "seen_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("listing.id", ondelete="CASCADE"), index=True
    )
    price_minor: Mapped[int | None] = mapped_column(BigInteger)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    listing: Mapped[Listing] = relationship(back_populates="price_history")


class SystemConfig(Base):
    """Single-row runtime switchboard.

    Exists because Vercel functions are stateless: an in-process flag would
    not survive between invocations, so the on/off state for scheduled
    ingestion has to live somewhere both the cron endpoint and the toggle
    endpoint can see. The database is that somewhere.
    """

    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # The kill switch. Every scheduled tick checks this BEFORE calling Apify.
    is_ingestion_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Bounded demo sequence: a run decrements this, and ingestion switches
    # itself off at zero. Prevents a forgotten demo draining the budget.
    runs_remaining: Mapped[int] = mapped_column(Integer, default=0)
    runs_completed: Mapped[int] = mapped_column(Integer, default=0)

    interval_seconds: Mapped[int] = mapped_column(Integer, default=180)
    search_query: Mapped[str] = mapped_column(String(255), default="iphone")
    location: Mapped[str] = mapped_column(String(64), default="bengaluru")
    max_pages: Mapped[int] = mapped_column(Integer, default=1)

    # Automation grid state. These five lived in a module-level object until
    # Vercel made it obvious that per-instance memory is not shared state:
    # the console alternated between two self-consistent views depending on
    # which lambda answered the poll. In the table there is only one view.
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    per_cell: Mapped[int] = mapped_column(Integer, default=10)
    # Defaults FALSE: the old dataclass defaulted True, so a cold instance
    # quoted twice the per-run cost of a warm one.
    include_details: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    city: Mapped[str] = mapped_column(String(64), default="bangalore")

    # An Apify run started by one tick and collected by a later one. The
    # Actor's runtime swings 10.8s-47.1s for identical work, which overruns a
    # 60s function ceiling when the wait happens inside the request.
    pending_run_id: Mapped[str | None] = mapped_column(String(64))
    pending_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    pending_cell: Mapped[str | None] = mapped_column(String(128))
    pending_input: Mapped[dict | None] = mapped_column(JSONB)

    # Who currently holds ingestion: 'automation' | 'manual' | None. The real
    # mutual exclusion is a Postgres advisory lock, which dies with its
    # session and therefore cannot deadlock. These columns exist so a 409 can
    # name the holder, and so an orphaned lock is visible rather than silent.
    lock_holder: Mapped[str | None] = mapped_column(String(16))
    lock_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Manual run state. In the table for the same reason automation's is: an
    # Apify run has to outlive the request that started it.
    manual_run_id: Mapped[str | None] = mapped_column(String(64))
    manual_status: Mapped[str | None] = mapped_column(String(64))
    manual_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_input: Mapped[dict | None] = mapped_column(JSONB)

    # Marketplace filters, shared by both runners. Stored rather than passed
    # per call so a sequence keeps the filters it was armed with across ticks
    # and restarts.
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)

    sequence_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    stopped_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
