"""notification.dismissed_at: when the person dismissed a bell item (issue #255, D-UI-906)

SPEC-INTERFACES "The bell and its panel": Dismiss moves an item to the Dismissed tab and Restore
brings it back; a dismissal never deletes the row. One nullable text column (an ISO stamp, like
read_at), plain ALTER TABLE. Rows from before this migration have NULL: not dismissed.
Downgrade drops the column.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification", sa.Column("dismissed_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("notification", "dismissed_at")
