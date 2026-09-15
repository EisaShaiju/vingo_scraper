"""The extraction contract.

Both the GraphQL parser (primary) and the DOM parser (fallback) must produce
`Listing` objects. Keeping one contract is what makes the fallback useful:
downstream code never learns which path produced a row, and swapping parsers
never ripples into normalization, dedupe, or the API.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ExtractionMethod(str, enum.Enum):
    """How a row was obtained.

    Tracked per-row because a rising share of DOM_FALLBACK is our earliest
    signal that the GraphQL shape has drifted -- days before anyone notices
    missing data. The canary monitor alerts on this ratio.
    """

    GRAPHQL = "graphql"
    DOM_FALLBACK = "dom_fallback"


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
    source_url: str
    # Populated only when rehosting is enabled (config.rehost_images).
    # Until then we display thumbnails hot-linked and link back to source.
    rehosted_url: str | None = None
    is_primary: bool = False
    phash: str | None = None


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
