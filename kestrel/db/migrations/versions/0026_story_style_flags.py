"""story.style_flags: marks from the plain-writing and grounding checks (issue #93)

A proposed story keeps the problems the checks found (a stock word that survived the one re-ask,
a number or name no sketch states, a sentence the model said has no source) as a JSON list, so the
Stories page can show a small mark where the text is displayed. Kestrel never rewrites the story
for these. Existing rows get an empty list.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("story")}
    return "style_flags" in cols


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table("story", recreate="never") as batch:
        batch.add_column(sa.Column("style_flags", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE story SET style_flags = '[]' WHERE style_flags IS NULL"))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("story", recreate="never") as batch:
        batch.drop_column("style_flags")
