"""opportunity.comp_currency_known: whether the pay currency was stated (D-SCORE-28, issue #208)

A posting whose pay carries no currency ("80k-100k", a board that sends none) was stored as USD,
and the Tier 2 blocker check then compared it with a USD floor. One nullable boolean column,
plain ALTER TABLE. Rows from before this migration have NULL, which the check reads as unknown
(the safe side: a pay blocker on them needs confirming until the posting is seen again).
Downgrade drops the column.

Numbered 0037 because other open branches may take 0035 and 0036; it follows 0034, the head of
this branch, and is re-chained at merge if those land first.

Revision ID: 0037
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunity", sa.Column("comp_currency_known", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunity", "comp_currency_known")
