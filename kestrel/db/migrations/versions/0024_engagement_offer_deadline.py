"""engagement_offer.deadline: the decide-by date of an engagement offer (D-UI-122, D-UI-136)

The Offers screen shows a deadline pill on every offer card. An employment offer already had the
column; an engagement offer did not, so a consulting card could not say when the client wanted an
answer. The column is plain text like `offer.deadline` (an ISO date from the date input).

The downgrade drops the column, so any engagement deadlines entered are lost. Added and dropped
with plain ALTER TABLE, never a table copy (see 0013).

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("engagement_offer", recreate="never") as batch:
        batch.add_column(sa.Column("deadline", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("engagement_offer", recreate="never") as batch:
        batch.drop_column("deadline")
