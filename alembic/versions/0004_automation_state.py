"""Move automation state out of process memory and into system_config

The console's Automation panel kept its state in a module-level object
(`api/scheduler.py`). On Vercel that gives every lambda instance its own copy,
so polling returned whichever instance happened to answer and the UI alternated
between two self-consistent states — running/idle, cursor 2/cursor 1, cost
$0.0620/$0.1240 — without ever raising an error.

These four columns are what that object held. In the table, one instance's view
is every instance's view.

`include_details` defaults to FALSE deliberately: the dataclass default was
True, which is why a cold instance quoted double the price of a warm one.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_config",
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "system_config",
        sa.Column("per_cell", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "system_config",
        sa.Column(
            "include_details", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "system_config",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("city", sa.String(64), nullable=False, server_default="bangalore"),
    )


def downgrade() -> None:
    for col in ("city", "next_run_at", "include_details", "per_cell", "cursor"):
        op.drop_column("system_config", col)
