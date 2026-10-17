"""doc_section.hide_names and doc_master.updated_at (confidential mode, D-PRIV-1)

D-PRIV-1: a role marked as the operator's current employer carries an explicit, editable list of
names to hide (the label as written, its suffix-stripped form, the ALL-CAPS form of each, and
whatever the operator adds, such as a product name or "acme.com"). Matching uses exactly these
entries, so the operator, not a heuristic, decides what is hidden.

doc_master.updated_at records when a document was last saved, so the combined map for list views
follows a job change: an employer is hidden there only while it is current in the newest
document that names it.

Additive only: two nullable columns, no data rewritten.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("doc_section") as batch:
        batch.add_column(sa.Column("hide_names", sa.JSON(), nullable=True))
    with op.batch_alter_table("doc_master") as batch:
        batch.add_column(sa.Column("updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("doc_master", recreate="never") as batch:
        batch.drop_column("updated_at")
    with op.batch_alter_table("doc_section", recreate="never") as batch:
        batch.drop_column("hide_names")
