"""evaluation.correction_ids: the corrections a read was given (D-SCORE-4)

The ids of the requirement corrections whose lines a Tier 2 or Tier 3 read's OPERATOR_CORRECTIONS
block stated, so a score reads as older when one of them was removed or changed since, as well as
when a correction added since bears on the role. One nullable JSON column, plain ALTER TABLE (not
batch mode). Rows stored before this migration have NULL and keep the "added or changed since"
rule only. Downgrade drops the column.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evaluation", sa.Column("correction_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluation", "correction_ids")
