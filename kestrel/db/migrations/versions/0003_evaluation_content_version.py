"""evaluation.content_version — the opportunity content version an evaluation was made against

Lets the Tier 2 re-evaluation path (a materially changed re-scrape enqueues a `changed` task) be
idempotent: if a Tier 2 evaluation already exists for the opportunity's current version, a re-leased
or duplicate task skips instead of writing a second evaluation (hard rule 5).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evaluation",
        sa.Column("content_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("evaluation", "content_version")
