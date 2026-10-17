"""pursuit: allow re-posts of an applied role to surface

SPEC.md 5.6: a new posting that dedups into the group of a pursuit with status applied or later
is hidden on arrival as a re-post (archive_reason = applied, stage_reason names the pursuit).
`allow_reposts` turns that off for one pursuit, for aggregators that re-list the same title many
times. Default off. Existing rows get the default.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pursuit") as batch:
        batch.add_column(
            sa.Column("allow_reposts", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("pursuit") as batch:
        batch.drop_column("allow_reposts")
