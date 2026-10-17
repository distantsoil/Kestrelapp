"""spike init — trivial table to prove programmatic Alembic from bundled versions/.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spike_marker",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("note", sa.String(50)),
    )


def downgrade() -> None:
    op.drop_table("spike_marker")
