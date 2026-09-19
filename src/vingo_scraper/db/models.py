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
    fb_seller_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    profile_url: Mapped[str | None] = mapped_column(Text)
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
