"""Carry an in-flight Apify run across requests

A tick used to start the Actor and wait for it inside one request. The Actor's
own runtime is not ours and not predictable -- measured between 10.8s and 47.1s
for identical work -- so on Vercel's 60s ceiling a slow run plus the ingest
overran, and the function was killed after Apify had already charged us.

The wait now happens between requests: one tick starts the run and records it
here, a later tick collects it. These columns are that handoff.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_config",
        sa.Column("pending_run_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("pending_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("pending_cell", sa.String(128), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("pending_input", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    for col in ("pending_input", "pending_cell",
                "pending_started_at", "pending_run_id"):
        op.drop_column("system_config", col)
