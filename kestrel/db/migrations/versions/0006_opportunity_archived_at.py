"""opportunity.archived_at: hide from the active pipeline, reversibly

Backs the Archive/Unarchive action on the opportunity detail page (SPEC-INTERFACES 2.1). `stage`
already has a fixed, exhaustive set of values that track TIER PROGRESSION ('new', 't0_pass', ...,
'quarantined') and must not gain an 'archived' member: visibility and pipeline stage are different
axes. A nullable ISO8601 UTC string mirrors every other timestamp column in this schema: None means
not archived, a value is when it was archived, and clearing it back to None is the Unarchive path.

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
    with op.batch_alter_table("opportunity", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunity", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
