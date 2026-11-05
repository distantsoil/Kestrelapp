"""title_suggestion: add doc_master_id for cascade delete

P15 follow-up (#118). A suggestion is derived from a specific resume; deleting
that resume must remove the suggestions it produced. The doc_master_id column
links each suggestion to the document it was read from.

Revision ID: 0027
Revises: 0026
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

TABLE = "title_suggestion"


def _has_column() -> bool:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}
    return "doc_master_id" in cols


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE, recreate="never") as batch:
        batch.add_column(sa.Column("doc_master_id", sa.Integer(), nullable=True))
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_title_suggestion_doc_master_id "
        f"ON {TABLE} (doc_master_id)"
    )


def downgrade() -> None:
    if not _has_column():
        return
    op.execute("DROP INDEX IF EXISTS ix_title_suggestion_doc_master_id")
    with op.batch_alter_table(TABLE, recreate="never") as batch:
        batch.drop_column("doc_master_id")
