"""source: origin (shipped | generated | custom)

D-SRC-2: job searches are generated from the operator's profile and rebuilt silently whenever
the profile changes. A rebuild must touch only the rows it made, never one the operator added
or edited, so every source records where it came from. Existing rows are the shipped examples
or the operator's own, and both read as `shipped` until edited (the Sources page marks an edited
row `custom`).

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(
            sa.Column("origin", sa.String(), nullable=False, server_default="shipped")
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.drop_column("origin")
