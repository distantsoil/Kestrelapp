"""run: which tier set the run used

The operator can run Kestrel on local models or on hosted models under their own key, as a
standing setting (`llm.mode`) and per run (SPEC.md 6.4, D-HOSTED-2). Each pipeline run now
records the mode it resolved, so the Runs list can say which set scored a batch. Nullable because
runs from before this migration had no such choice; each Evaluation already names its model.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.add_column(sa.Column("mode", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.drop_column("mode")
