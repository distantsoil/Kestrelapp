"""opportunity: why the operator hid it

Hide (the operator-facing name for archiving, SPEC-INTERFACES 2.1 opportunity actions) now records
a reason: `applied` (applied elsewhere, outside Kestrel), `not_interested`, `later`, or `other`.
The Hidden chip shows it per row, and the learning loop can tell "not wanted" from "already
done" when it reads hidden rows. Nullable: rows hidden before this migration, and rows hidden
without choosing, carry no reason. `archived_at` keeps its meaning; this only annotates it.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opportunity") as batch:
        batch.add_column(sa.Column("archive_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunity") as batch:
        batch.drop_column("archive_reason")
