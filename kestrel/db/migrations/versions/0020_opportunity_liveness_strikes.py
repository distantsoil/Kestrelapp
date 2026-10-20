"""opportunity: liveness strikes and the reason a posting closed

A posting used to close on one 404/410 or one "position filled" phrase, so a transient page change
or a CDN error closed a role the operator was pursuing (hard rule 4: uncertainty escalates, it
never rejects). It now closes on the second consecutive expired read (D-INTEL-1).
`liveness_strikes` counts the consecutive expired reads (0 or 1 while the posting is open, 2 once
closed); `closed_reason` records why it closed: `http_404`, `http_410` or `page_text`
(`ats_missing` is reserved). Existing rows backfill to 0 strikes and no reason. A row already
`closed` stays closed, with no reason recorded.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opportunity") as batch:
        batch.add_column(
            sa.Column("liveness_strikes", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("closed_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunity") as batch:
        batch.drop_column("closed_reason")
        batch.drop_column("liveness_strikes")
