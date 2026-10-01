"""Record what started a scrape run

The console's automation feed reads `scrape_run` directly, so a manual
"Today's update" appeared among the automation ticks -- a `bangalore / iphone`
row sitting between the category cells, which reads as the automation having
scraped something it never did.

Existing rows backfill to 'manual': every run before this column existed was
either a manual console run or an early scheduler experiment, and attributing
an unknown run to automation would misrepresent the feed in the direction that
matters.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scrape_run",
        sa.Column("trigger", sa.String(16), nullable=False,
                  server_default="manual"),
    )
    op.create_index("ix_scrape_run_trigger", "scrape_run", ["trigger"])


def downgrade() -> None:
    op.drop_index("ix_scrape_run_trigger", table_name="scrape_run")
    op.drop_column("scrape_run", "trigger")
