"""Outreach state -- who has been contacted, and what came of it

The scraper produced a catalog; the business needs a queue. This adds the three
columns that turn one into the other.

They go on **listing**, not on seller, and that is a deliberate concession to a
measured limit rather than a modelling preference. Seller identity is present
on ~6.4% of listings and phone/email on 0% (`docs/seller-data-problem.md`) --
Facebook does not expose it. A `seller.contacted_at` would therefore be null
for 19 of every 20 leads and would silently merge unrelated people whenever the
rotating `pfbid` collided. The listing is the thing we can always identify, and
its URL is the contact route, so the listing is what carries the state.

`outreach_status` is indexed because the only query that matters is "show me
everything still marked new".

All three are nullable with a server default, so this migration is additive:
`docs/ROLLBACK.md`'s checkout-and-restart still works without downgrading.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Deliberately NOT a Postgres enum. The review_status enum has already needed a
# migration to change, and these labels will move as soon as someone works the
# queue for a week -- "replied" may want splitting into interested/declined.
# A varchar with a CHECK-free application-level vocabulary costs nothing here
# and makes that change a one-line edit instead of a migration.
OUTREACH_STATUSES = ("new", "contacted", "replied", "converted", "dead")


def upgrade() -> None:
    op.add_column(
        "listing",
        sa.Column("outreach_status", sa.String(16), nullable=False,
                  server_default="new"),
    )
    op.add_column(
        "listing", sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Free text: what the seller actually said. The single most valuable field
    # here and the one no amount of scraping can produce.
    op.add_column("listing", sa.Column("outreach_note", sa.Text(), nullable=True))

    op.create_index("ix_listing_outreach_status", "listing", ["outreach_status"])


def downgrade() -> None:
    op.drop_index("ix_listing_outreach_status", table_name="listing")
    op.drop_column("listing", "outreach_note")
    op.drop_column("listing", "contacted_at")
    op.drop_column("listing", "outreach_status")
