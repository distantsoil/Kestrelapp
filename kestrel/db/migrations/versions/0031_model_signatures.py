"""ml_model.artifact_signature and pca_artifact_signature (D-SEC-13)

Each saved reranker file is signed (HMAC-SHA256 under a per-install key) and the signature is
stored on the row; the loader verifies it before unpickling. Two nullable text columns, plain
ALTER TABLE (not batch mode). Rows saved before this migration have NULL and are refused by the
loader, so they retrain. Downgrade drops the columns.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ml_model", sa.Column("artifact_signature", sa.String(), nullable=True))
    op.add_column("ml_model", sa.Column("pca_artifact_signature", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("ml_model", "pca_artifact_signature")
    op.drop_column("ml_model", "artifact_signature")
