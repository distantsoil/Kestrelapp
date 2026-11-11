"""requirement_correction: the operator's corrections to what a posting asks for (issue 170)

One new table (SPEC-MODULES 5.1, D-LEARN-3) and its indexes, created with plain op calls: no
existing table is rebuilt, so no batch mode. Two partial unique indexes hold the scope rules: one
`person` row per (track, requirement_hash) and one `posting` row per (opportunity_id,
requirement_hash). `story_id` is ON DELETE SET NULL, and `story_deleted_at` records that the story
went (D-LEARN-5). Downgrade drops the table and its indexes; nothing else refers to it.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

TABLE = "requirement_correction"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
        sa.Column("evaluation_id", sa.Integer(), nullable=True),
        sa.Column("requirement_text", sa.Text(), nullable=False),
        sa.Column("requirement_hash", sa.String(), nullable=False),
        sa.Column("kestrel_state", sa.String(), nullable=False),
        sa.Column("operator_state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("story_id", sa.Integer(), nullable=True),
        sa.Column("basis", sa.String(), nullable=False),
        sa.Column("agrees_now", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.String(), nullable=True),
        sa.Column("story_deleted_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunity.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["evaluation_id"], ["evaluation.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["story_id"], ["story.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_requirement_correction_opportunity_id", TABLE, ["opportunity_id"])
    op.create_index("ix_requirement_correction_track_hash", TABLE, ["track", "requirement_hash"])
    op.create_index(
        "ux_requirement_correction_person",
        TABLE,
        ["track", "requirement_hash"],
        unique=True,
        sqlite_where=sa.text("scope = 'person'"),
    )
    op.create_index(
        "ux_requirement_correction_posting",
        TABLE,
        ["opportunity_id", "requirement_hash"],
        unique=True,
        sqlite_where=sa.text("scope = 'posting'"),
    )


def downgrade() -> None:
    op.drop_index("ux_requirement_correction_posting", table_name=TABLE)
    op.drop_index("ux_requirement_correction_person", table_name=TABLE)
    op.drop_index("ix_requirement_correction_track_hash", table_name=TABLE)
    op.drop_index("ix_requirement_correction_opportunity_id", table_name=TABLE)
    op.drop_table(TABLE)
