"""task: at most one live scoring task per (type, opportunity)

Release round review, finding 4: `pipeline.tasks.enqueue_once` checked for a live task and then
inserted, so two worker processes (the desktop app and the CLI) could both pass the check and
both insert. A partial unique index on live t1, t2, t3 and rescore tasks per (type,
opportunity_id) makes the database the arbiter: the second insert fails, and enqueue_once (which
inserts inside a savepoint) reads that as "already queued".

Existing duplicates (from before this index) would make the CREATE fail, so the newest live
duplicates are cancelled first, keeping the oldest live row per pair. Additive otherwise: no
column changes.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

INDEX = "ux_task_live_scoring"
_OID = "json_extract(payload, '$.opportunity_id')"
_WHERE = (
    "status IN ('pending', 'leased') AND type IN ('t1', 't2', 't3', 'rescore') "
    f"AND {_OID} IS NOT NULL"
)


def upgrade() -> None:
    op.execute(
        "UPDATE task SET status = 'cancelled', worker_id = NULL, lease_until = NULL, "
        "last_error = 'duplicate of an earlier queued task' "
        f"WHERE {_WHERE} AND id NOT IN ("
        f"  SELECT MIN(id) FROM task WHERE {_WHERE} GROUP BY type, {_OID}"
        ")"
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON task (type, {_OID}) WHERE {_WHERE}")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
