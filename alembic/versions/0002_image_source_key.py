"""add listing_image.source_key for stable photo identity

Facebook serves different derivatives (thumbnail vs full-size) of the same
photo across runs, so the bytes -- and therefore sha256 -- change while the
photo does not. Keying dedupe on sha256 made every re-ingest insert duplicate
image rows. The fbcdn URL *path* is stable even though host and query rotate,
so that becomes the dedupe key.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listing_image", sa.Column("source_key", sa.Text(), nullable=True))

    # Backfill from existing urls: strip scheme+host and the query string.
    # split_part(x, '?', 1) drops the signed params; the regexp removes the
    # protocol and host, leaving the stable path.
    op.execute(
        """
        UPDATE listing_image
        SET source_key = regexp_replace(
            split_part(source_url, '?', 1),
            '^https?://[^/]+', ''
        )
        WHERE source_key IS NULL
        """
    )

    # Collapse rows that the old sha256-based key let through as duplicates:
    # keep the earliest row per (listing_id, source_key).
    op.execute(
        """
        DELETE FROM listing_image a
        USING listing_image b
        WHERE a.listing_id = b.listing_id
          AND a.source_key = b.source_key
          AND a.created_at > b.created_at
        """
    )

    op.drop_constraint("uq_listing_image_sha", "listing_image", type_="unique")
    op.create_unique_constraint(
        "uq_listing_image_src", "listing_image", ["listing_id", "source_key"]
    )
    op.create_index("ix_listing_image_source_key", "listing_image", ["source_key"])


def downgrade() -> None:
    op.drop_index("ix_listing_image_source_key", table_name="listing_image")
    op.drop_constraint("uq_listing_image_src", "listing_image", type_="unique")
    op.create_unique_constraint(
        "uq_listing_image_sha", "listing_image", ["listing_id", "sha256"]
    )
    op.drop_column("listing_image", "source_key")
