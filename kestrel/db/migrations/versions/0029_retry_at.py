"""task.retry_at: a retryable failure backs off before it is claimable again (issue #153)

Before this, a failed task requeued at once, so a persistently failing backend (a model that
is down, a key out of credit) was hammered as soon as the worker cycled, while a clean quit
still spent the in-flight attempt of every task type except the model-download and build ones.

This adds the `retry_at` column (an ISO timestamp, nullable) the queue sets when a retryable
failure is requeued: 30 s after the first failed attempt, 2 min after the second, 10 min
after the third and any later one (by the attempts already spent, not by the error). A clean
quit (release_all_leased) gives the in-flight attempt back for every task type in the same
release; a crash or an expired lease still spends it, except the refund types
(`ollama_pull`, `ollama_create`). A "Try again" from the UI (score again on the waiting
row) clears the column so the operator's fresh request is claimable at once (D-QUEUE-3).

Downgrade drops the column and its index. A task that was backing off when it is dropped is
simply claimable again at once: the wait was a delay, not state, so nothing is lost.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE ADD COLUMN and CREATE INDEX, not batch mode: batch reflects the whole
    # table, and SQLAlchemy cannot reflect the expression index ux_task_live_scoring, so it
    # warned on every run (D-PERF). SQLite adds a nullable column in place.
    op.add_column("task", sa.Column("retry_at", sa.String(), nullable=True))
    op.create_index("ix_task_retry_at", "task", ["retry_at"])


def downgrade() -> None:
    # DROP COLUMN needs SQLite 3.35 (2021), which the bundled Python 3.12 builds exceed; the
    # index must go first, since SQLite refuses to drop an indexed column.
    op.drop_index("ix_task_retry_at", table_name="task")
    op.drop_column("task", "retry_at")
