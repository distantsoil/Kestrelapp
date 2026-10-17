"""doc_master: optional sections (education, certifications, licenses, projects) and look

D-DOC-3: a resume carries four optional sections the import never produced: education,
certifications, licenses and projects. Each is a JSON list of the operator's own typed entries on
the master, the pattern skill_groups already uses; they are always printed when present and never
model-selected. D-DOC-4: `look` holds the master's saved presentation choices (the accent color),
so every render of that document, including the pipeline's, uses the color the operator picked.

Additive only: nullable columns, no data rewritten.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_COLUMNS = ("education", "certifications", "licenses", "projects", "look")


def upgrade() -> None:
    with op.batch_alter_table("doc_master") as batch:
        for name in _COLUMNS:
            batch.add_column(sa.Column(name, sa.JSON(), nullable=True))


def downgrade() -> None:
    # Plain ALTER TABLE ... DROP COLUMN (SQLite 3.35+), never a table copy: doc_master is a
    # parent table, and with foreign_keys=ON the copy's DROP TABLE fails on any doc_section row.
    with op.batch_alter_table("doc_master", recreate="never") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
