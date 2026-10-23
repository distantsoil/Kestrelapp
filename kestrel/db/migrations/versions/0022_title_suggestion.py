"""title_suggestion: job titles the model proposes for the operator's target titles

P15 (D-TTL-3, #31). A suggestion is stored `suggested`; only an explicit Accept adds it to
employment_terms.target_titles, and a dismissed or accepted title is never suggested again. The
title is unique case-insensitively across every status, so the table itself remembers each title
it has shown.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLE = "title_suggestion"
TITLE_INDEX = "ux_title_suggestion_title"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="suggested"),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("decided_at", sa.String(), nullable=True),
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {TITLE_INDEX} ON {TABLE} (lower(title))")
    op.create_index("ix_title_suggestion_status", TABLE, ["status"])


def downgrade() -> None:
    op.drop_index("ix_title_suggestion_status", table_name=TABLE)
    op.execute(f"DROP INDEX IF EXISTS {TITLE_INDEX}")
    op.drop_table(TABLE)
