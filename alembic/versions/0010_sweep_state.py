"""Sweep progress -- so a 40-minute bulk run can survive a restart

A 1,000-listing sweep is ~42 Actor pages run sequentially. Dedup already stops
a re-scraped listing being STORED twice, but it does not stop it being BILLED
twice: the Actor charges when it scrapes, long before any id reaches us. So a
crash at cell 9 of 14 would mean paying for cells 1-8 again.

One nullable JSONB column holding which cells are done. In the table rather
than in memory for the same reason `pending_run_id` is: the work outlives the
request that started it, and in-process state does not error under concurrency,
it quietly disagrees with itself.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("system_config", sa.Column("sweep_state", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("system_config", "sweep_state")
