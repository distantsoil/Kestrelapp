"""frontier_transmission — Tier 3 escalation audit trail

Every operator-confirmed transmission to the hosted frontier (SPEC 6.4, hard rule 1) is recorded
here: what was sent, where, when, whether it was redacted, and the confirmation hash the operator
approved. Tier 3 is the ONLY path that transmits operator data off the machine.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "frontier_transmission",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunity.id"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("confidential", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("payload_sha256", sa.String(), nullable=False),
        sa.Column("prompt_tokens_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evaluation_id", sa.Integer(), sa.ForeignKey("evaluation.id"), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_frontier_transmission_opportunity", "frontier_transmission", ["opportunity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_frontier_transmission_opportunity", table_name="frontier_transmission")
    op.drop_table("frontier_transmission")
