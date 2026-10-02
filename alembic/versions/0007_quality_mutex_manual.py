"""Quality score, structured description facts, ingestion mutex, manual runs

Four things that arrive together because they are one change in behaviour:
manual ingestion stops being a second, worse pipeline and becomes the same
two-phase runner as automation, guarded by one lock.

* `listing.quality_score` / `quality_flags` -- the gate scores before images
  are fetched, so a rejected listing costs no bandwidth.
* `listing.description_facts` -- structured specs/condition/sale-reason pulled
  out of freeform text.
* `system_config.lock_*` -- who holds ingestion, and since when. The real
  mutual exclusion is a Postgres advisory lock (it dies with the session, so
  it cannot deadlock); these columns exist so the UI can say *which* runner is
  busy, and so an orphaned lock is visible rather than merely absent.
* `system_config.manual_*` -- manual run state, in the table for the same
  reason automation's is: a pending Apify run has to survive the request that
  started it.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- quality gate -----------------------------------------------------
    op.add_column(
        "listing",
        sa.Column("quality_score", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "listing",
        sa.Column("quality_flags", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index("ix_listing_quality_score", "listing", ["quality_score"])

    # --- structured description facts -------------------------------------
    op.add_column(
        "listing",
        sa.Column("description_facts", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )

    # --- ingestion mutex --------------------------------------------------
    # 'automation' | 'manual' | NULL. Advisory lock does the enforcing; this
    # records who, so a 409 can name the holder instead of saying "busy".
    op.add_column("system_config", sa.Column("lock_holder", sa.String(16), nullable=True))
    op.add_column(
        "system_config",
        sa.Column("lock_acquired_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- manual run state -------------------------------------------------
    for col in ("manual_run_id", "manual_status"):
        op.add_column("system_config", sa.Column(col, sa.String(64), nullable=True))
    op.add_column(
        "system_config",
        sa.Column("manual_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("manual_input", postgresql.JSONB(), nullable=True),
    )

    # --- filters, shared by both runners ----------------------------------
    # Stored rather than passed per call so an automated sequence keeps the
    # filters it was armed with across ticks, restarts and redeploys.
    op.add_column(
        "system_config",
        sa.Column("filters", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    for col in ("filters", "manual_input", "manual_started_at", "manual_status",
                "manual_run_id", "lock_acquired_at", "lock_holder"):
        op.drop_column("system_config", col)
    op.drop_column("listing", "description_facts")
    op.drop_index("ix_listing_quality_score", table_name="listing")
    op.drop_column("listing", "quality_flags")
    op.drop_column("listing", "quality_score")
