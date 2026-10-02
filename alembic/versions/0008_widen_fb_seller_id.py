"""Widen seller.fb_seller_id -- a pfbid does not fit in 64 characters

The first run that actually returned seller identity died on:

    StringDataRightTruncation: value too long for type character varying(64)
    fb_seller_id = 'pfbid02nMjZE2MMvAAXtfBad9Hm6PLTiCcTweTfeb3xp1jzN2QMP4VWRPoXLeZ52fgnXjbSl'

That is 73 characters. The column was sized for Facebook's old numeric user
ids; `pfbid` identifiers are rotating, opaque and longer, and they are what
Marketplace returns now. 255 leaves room without pretending we know the
upper bound -- nothing documents one.

The Actor had already been charged for the page when this threw, so the whole
run was paid for and discarded.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "seller", "fb_seller_id",
        existing_type=sa.String(64), type_=sa.String(255),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Lossy by definition: any pfbid stored since the upgrade is longer than
    # 64 characters, so narrowing would truncate real rows. Fail loudly
    # instead of silently corrupting seller identity.
    op.execute(
        "DELETE FROM seller WHERE length(fb_seller_id) > 64"
    )
    op.alter_column(
        "seller", "fb_seller_id",
        existing_type=sa.String(255), type_=sa.String(64),
        existing_nullable=False,
    )
