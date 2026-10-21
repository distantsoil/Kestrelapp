"""interview_question_set: likely interview questions generated when a role is marked Applied

P14 (D-INTERVIEW-1). One row per generated set; the active set of a pursuit is the one with a
null `superseded_at`, and a partial unique index keeps it to one per pursuit. Regenerating writes
a new row and stamps the old one superseded. The prep pack, the mock interview and the debrief
prefer the active set over the Tier 2 Block F questions.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

TABLE = "interview_question_set"
ACTIVE_INDEX = "ux_interview_question_set_active"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunity.id"), nullable=False),
        sa.Column("pursuit_id", sa.Integer(), sa.ForeignKey("pursuit.id"), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("superseded_at", sa.String(), nullable=True),
    )
    op.create_index("ix_interview_question_set_opportunity_id", TABLE, ["opportunity_id"])
    op.create_index("ix_interview_question_set_pursuit_id", TABLE, ["pursuit_id"])
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_INDEX} ON {TABLE} (pursuit_id) "
        "WHERE superseded_at IS NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {ACTIVE_INDEX}")
    op.drop_index("ix_interview_question_set_pursuit_id", table_name=TABLE)
    op.drop_index("ix_interview_question_set_opportunity_id", table_name=TABLE)
    op.drop_table(TABLE)
