"""initial schema: listing, listing_image, seller, scrape_run

Revision ID: 0001
Revises:
Create Date: 2026-09-18

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# Enum values are stored lowercase (the member *values*, not Python names) so
# the data is readable in psql and the Supabase table editor.
condition = postgresql.ENUM(
    "new", "used_like_new", "used_good", "used_fair", "unknown",
    name="condition", create_type=False,
)
extraction_method = postgresql.ENUM(
    "apify", "graphql", name="extraction_method", create_type=False,
)
review_status = postgresql.ENUM(
    "pending_review", "approved", "rejected", "takedown",
    name="review_status", create_type=False,
)
image_status = postgresql.ENUM(
    "pending", "stored", "failed", "rejected",
    name="image_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (condition, extraction_method, review_status, image_status):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "seller",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fb_seller_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("profile_url", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_seller_fb_seller_id", "seller", ["fb_seller_id"], unique=True)

    op.create_table(
        "scrape_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(64)),
        sa.Column("run_input", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("items_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("images_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("images_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scrape_run_run_id", "scrape_run", ["run_id"])

    op.create_table(
        "listing",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fb_listing_id", sa.String(64), nullable=False),
        sa.Column("listing_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        # Paise. NULL = unreadable, 0 = genuinely free. Not the same thing.
        sa.Column("price_minor", sa.BigInteger()),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("condition", condition, nullable=False, server_default="unknown"),
        sa.Column("city", sa.String(64)),
        sa.Column("location_text", sa.String(255)),
        sa.Column("category", sa.String(64)),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True)),
        sa.Column("scrape_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "extraction_method", extraction_method,
            nullable=False, server_default="apify",
        ),
        # Nothing is buyer-visible until a human approves it.
        sa.Column(
            "review_status", review_status,
            nullable=False, server_default="pending_review",
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["seller_id"], ["seller.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["scrape_run_id"], ["scrape_run.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_listing_fb_listing_id", "listing", ["fb_listing_id"], unique=True)
    op.create_index("ix_listing_city_category", "listing", ["city", "category"])
    op.create_index("ix_listing_review_status", "listing", ["review_status"])
    op.create_index("ix_listing_last_seen", "listing", ["last_seen_at"])

    op.create_table(
        "listing_image",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        # fbcdn url: provenance and retry only. Expires -- never served.
        sa.Column("source_url", sa.Text(), nullable=False),
        # Ours. The only renderable url.
        sa.Column("storage_url", sa.Text()),
        sa.Column("status", image_status, nullable=False, server_default="pending"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("content_type", sa.String(64)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("listing_id", "sha256", name="uq_listing_image_sha"),
    )
    op.create_index("ix_listing_image_listing_id", "listing_image", ["listing_id"])
    op.create_index("ix_listing_image_status", "listing_image", ["status"])
    op.create_index("ix_listing_image_sha", "listing_image", ["sha256"])


def downgrade() -> None:
    op.drop_table("listing_image")
    op.drop_table("listing")
    op.drop_table("scrape_run")
    op.drop_table("seller")
    bind = op.get_bind()
    for enum in (image_status, review_status, extraction_method, condition):
        enum.drop(bind, checkfirst=True)
