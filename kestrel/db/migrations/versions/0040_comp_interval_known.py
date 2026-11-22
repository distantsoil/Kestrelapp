"""opportunity.comp_interval_known: whether the pay interval was stated (D-SCORE-28, issue #208)

A posting whose pay names no interval, or whose board sends none and the adapter assumed one,
cannot be compared with an annual floor without guessing. One nullable boolean column, plain
ALTER TABLE. Rows from before this migration have NULL, which the blocker check reads as unknown.
Downgrade drops the column.

Numbered 0040 so 0037 stays as round 3 wrote it; other open branches may take 0038 and 0039,
and the number is re-chained at merge if those land first.

Revision ID: 0040
Revises: 0037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity", sa.Column("comp_interval_known", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity", "comp_interval_known")
