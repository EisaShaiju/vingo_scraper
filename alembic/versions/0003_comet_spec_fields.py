"""Project Comet spec fields + demo kill switch

Adds what the Phase 1 spec requires and our schema did not carry:
seller contact, listing attributes, Facebook category id, sold/pending/hidden
status, and price-change history.

Also adds `system_config` — a single-row switchboard. It exists because Vercel
functions are stateless: an in-process flag cannot survive between
invocations, so the scheduled-ingestion on/off state has to live where both
the cron endpoint and the toggle endpoint can see it.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- seller: spec requires profile link and contact -------------------
    op.add_column("seller", sa.Column("phone", sa.String(32), nullable=True))
    op.add_column("seller", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("seller", sa.Column("rating", sa.Float(), nullable=True))
    op.add_column("seller", sa.Column("rating_count", sa.Integer(), nullable=True))

    # --- listing: attributes, category, status ----------------------------
    op.add_column("listing", sa.Column("fb_category_id", sa.String(64), nullable=True))
    op.add_column(
        "listing",
        sa.Column("attributes", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    for col in ("is_sold", "is_pending", "is_hidden"):
        op.add_column(
            "listing",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.create_index("ix_listing_is_sold", "listing", ["is_sold"])

    # --- price history ----------------------------------------------------
    op.create_table(
        "listing_price_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("price_minor", sa.BigInteger(), nullable=True),
        sa.Column("is_sold", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_listing_price_history_listing_id",
                    "listing_price_history", ["listing_id"])
    op.create_index("ix_price_history_listing",
                    "listing_price_history", ["listing_id", "seen_at"])

    # --- system config ----------------------------------------------------
    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("is_ingestion_active", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("runs_remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runs_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("search_query", sa.String(255), nullable=False, server_default="iphone"),
        sa.Column("location", sa.String(64), nullable=False, server_default="bengaluru"),
        sa.Column("max_pages", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sequence_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("stopped_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Seed the single row, switched OFF. Nothing should ingest until a human
    # turns it on.
    op.execute(
        """
        INSERT INTO system_config
            (id, is_ingestion_active, runs_remaining, runs_completed,
             interval_seconds, search_query, location, max_pages, updated_at)
        VALUES (1, false, 0, 0, 180, 'iphone', 'bengaluru', 1, now())
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("system_config")
    op.drop_index("ix_price_history_listing", table_name="listing_price_history")
    op.drop_index("ix_listing_price_history_listing_id",
                  table_name="listing_price_history")
    op.drop_table("listing_price_history")

    op.drop_index("ix_listing_is_sold", table_name="listing")
    for col in ("is_hidden", "is_pending", "is_sold", "attributes", "fb_category_id"):
        op.drop_column("listing", col)
    for col in ("rating_count", "rating", "email", "phone"):
        op.drop_column("seller", col)
