"""opportunity (source_id, external_id) index for the re-seen lookup

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_opportunity_source_external", "opportunity", ["source_id", "external_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_source_external", table_name="opportunity")
