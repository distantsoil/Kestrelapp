"""opportunity_embedding.text_hash: re-embed when the embedded text changes

Without this the embed handler could only tell that a vector EXISTED, not whether it still described
the opportunity. Re-seen postings enqueue a fresh embed task on both `reseen_changed` and
`reseen_minor` (`ingest/normalize.py::_handle_reseen`), and the handler discarded every one of them,
so a rewritten job description kept its original vector forever while the task completed `done`.
That is worse than the empty index it replaced: few-shot retrieval, the reranker gate, and semantic
search would all be ranking against text that no longer exists, and the queue would look clean.

Hashes the exact text that was embedded rather than `opportunity.content_hash` or `version`:
`reseen_minor` deliberately does not bump `version`, and `content_hash` covers fields the embedding
does not use. Keying on the input to the model is the only value that cannot drift from it.

Nullable: an existing row predates the column and re-embeds once, which is correct rather than
merely convenient, since nothing recorded what those vectors were computed from.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opportunity_embedding", schema=None) as batch_op:
        batch_op.add_column(sa.Column("text_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opportunity_embedding", schema=None) as batch_op:
        batch_op.drop_column("text_hash")
