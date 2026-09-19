"""The extraction contract.

Both the GraphQL parser (primary) and the DOM parser (fallback) must produce
`Listing` objects. Keeping one contract is what makes the fallback useful:
downstream code never learns which path produced a row, and swapping parsers
never ripples into normalization, dedupe, or the API.
"""

from __future__ import annotations

import enum
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def stable_source_key(url: str) -> str:
    """Photo identity that survives Facebook's CDN churn.

    Same photo, two runs:
      scontent-mia5-1.xx.fbcdn.net/v/t39../817651258_..._n.jpg?stp=c0.124.261...
      scontent.fboi1-1.fna.fbcdn.net/v/t39../817651258_..._n.jpg?stp=dst-jpg_s960x960...

    Host and query differ, and the bytes differ (different resolution), so
    neither the URL nor a content hash identifies the photo. The path does.
    """
    try:
        return urlparse(url).path or url
    except Exception:
        return url


class ExtractionMethod(str, enum.Enum):
    """Where a row came from.

    Kept per-row for provenance: when ingest quality changes we need to know
    whether the source changed with it. GRAPHQL is retained so historical rows
    from the pre-Apify pipeline stay readable.
    """

    APIFY = "apify"
    GRAPHQL = "graphql"  # legacy: self-hosted browser era


class ImageStatus(str, enum.Enum):
    """Rehosting state for one image.

    Facebook CDN urls are signed and expire within hours-to-days, so an image
    is only servable once it is in our own storage. A per-image status means a
    single failed download degrades one photo instead of losing the listing.
    """

    PENDING = "pending"
    STORED = "stored"
    FAILED = "failed"
    REJECTED = "rejected"  # downloaded, but not actually an image


class Condition(str, enum.Enum):
    NEW = "new"
    USED_LIKE_NEW = "used_like_new"
    USED_GOOD = "used_good"
    USED_FAIR = "used_fair"
    UNKNOWN = "unknown"


class ReviewStatus(str, enum.Enum):
    """Nothing reaches buyers without a human approving it.

    Scraped rows land in PENDING_REVIEW. This is both catalog quality control
    and the process we point at if a seller or Meta ever objects.
    """

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    TAKEDOWN = "takedown"


class Seller(BaseModel):
    """Minimal seller record.

    Deliberately thin: this is personal data under India's DPDP Act, so we
    capture only what is needed to attribute and link back, never contact
    details. Do not widen this without a decision on record.
    """

    fb_seller_id: str | None = None
    name: str | None = None
    profile_url: str | None = None


class ListingImage(BaseModel):
    """One listing photo, tracked from Facebook CDN to our own storage."""

    # The original fbcdn url. Kept for provenance and for re-fetching after a
    # failed upload -- it is NEVER served to a client, because it expires.
    source_url: str

    # Stable identity for the *photo*, independent of which derivative
    # Facebook happened to serve. The fbcdn host and query string rotate
    # between runs (and the same photo comes back at different resolutions,
    # so its bytes and sha256 change), but the URL path does not. Without
    # this, every re-ingest would insert duplicate image rows forever.
    source_key: str | None = None

    # Our permanent storage url. This is the only url anything downstream may
    # render. Populated once status is STORED.
    storage_url: str | None = None

    status: ImageStatus = ImageStatus.PENDING
    is_primary: bool = False

    # sha256 of the image bytes. Doubles as dedupe key (the same photo reused
    # across listings is stored once) and as the storage path component, which
    # is what makes re-ingesting a listing idempotent.
    sha256: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    error: str | None = None

    def model_post_init(self, _ctx) -> None:
        if not self.source_key:
            self.source_key = stable_source_key(self.source_url)

    @property
    def is_servable(self) -> bool:
        """True only when we can render this without depending on Facebook."""
        return self.status is ImageStatus.STORED and bool(self.storage_url)


class Listing(BaseModel):
    """A single Marketplace listing, normalized."""

    # --- identity ------------------------------------------------------
    fb_listing_id: str
    listing_url: str

    # --- content -------------------------------------------------------
    title: str
    description: str | None = None
    price_minor: int | None = Field(
        default=None,
        description="Price in paise (INR minor units). Integer to avoid float drift.",
    )
    currency: str = "INR"
    condition: Condition = Condition.UNKNOWN

    # --- placement -----------------------------------------------------
    city: str | None = None
    location_text: str | None = None
    category: str | None = None

    # --- provenance ----------------------------------------------------
    seller: Seller | None = None
    images: list[ListingImage] = Field(default_factory=list)
    posted_at: datetime | None = None

    # --- pipeline metadata ---------------------------------------------
    extraction_method: ExtractionMethod
    scraped_at: datetime | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING_REVIEW

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("listing title is empty")
        return v

    @property
    def price_major(self) -> float | None:
        """Price in rupees, for display only."""
        return None if self.price_minor is None else self.price_minor / 100

    def completeness(self) -> dict[str, bool]:
        """Field presence, used by the canary monitor to build a baseline.

        When the presence rate of a field drops sharply between runs, the
        parser has drifted even though nothing threw an exception. That
        silent-degradation case is the one that burns clients.
        """
        return {
            "title": bool(self.title),
            "price": self.price_minor is not None,
            "description": bool(self.description),
            "image": len(self.images) > 0,
            "location": bool(self.location_text or self.city),
            "seller": self.seller is not None and bool(self.seller.fb_seller_id),
            "posted_at": self.posted_at is not None,
        }
