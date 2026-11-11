"""task.opportunity_id and the foreign-key indexes the pruning and the lists read (D-PERF-3)

Three indexes: opportunity(scrape_run_id) and quarantine(opportunity_id), which every delete of a
run and every per-role failure lookup scanned without, and a real `task.opportunity_id` column.

The column is the role a task is about, copied from `json_extract(payload, '$.opportunity_id')`
so the hot per-role lookups (is this role being scored, did it fail, was it deferred) use an index
instead of evaluating a JSON expression on every task row for every role listed. The payload stays
the source: two triggers keep the column equal to it on every INSERT and every UPDATE of the
payload, including the Core statements (INSERT ... SELECT, `json_remove`) no mapper event sees, so
a writer cannot forget it. The model also sets it on the ORM path.

Plain ALTER TABLE and CREATE INDEX, not batch mode (batch reflects the table and SQLAlchemy
cannot reflect the expression index ux_task_live_scoring, see 0029). The backfill is one UPDATE.
Downgrade drops the triggers first, then the indexes, then the column; nothing is lost because
the payload still holds every id.

`opportunity.lane_local_only` (D-PERF-3) records on the role itself that it came through the
background lane (D-SRC-13): such a role is only ever read by a local model. That fact used to be
read from the role's t1 and t2 task rows, which retention deletes after 30 days, so a Score again
on day 40 could have sent a lane role to an online model. It is backfilled from the existing
tasks and set by two triggers on every write of a lane task (the payload carries `lane` or
`lane_local_only`), so no writer has to remember. Never cleared.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_EXPR = "json_extract(NEW.payload, '$.opportunity_id')"
# CASE, not AND: SQLite does not promise to stop at the first false operand, and json_extract
# raises on text that is not JSON.
_WHEN = f"CASE WHEN json_valid(NEW.payload) THEN NEW.opportunity_id IS NOT {_EXPR} ELSE 0 END"


def upgrade() -> None:
    op.add_column("task", sa.Column("opportunity_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE task SET opportunity_id = json_extract(payload, '$.opportunity_id') "
        "WHERE json_valid(payload) AND json_extract(payload, '$.opportunity_id') IS NOT NULL"
    )
    op.create_index("ix_task_opportunity_id", "task", ["opportunity_id"])
    op.add_column("opportunity", sa.Column("lane_local_only", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE opportunity SET lane_local_only = 1 WHERE id IN (SELECT opportunity_id FROM task "
        "WHERE type IN ('t1', 't2') AND opportunity_id IS NOT NULL AND json_valid(payload) "
        "AND (json_type(payload, '$.lane') IS NOT NULL "
        "OR json_extract(payload, '$.lane_local_only') = 1))"
    )
    op.create_index("ix_opportunity_scrape_run_id", "opportunity", ["scrape_run_id"])
    op.create_index("ix_quarantine_opportunity_id", "quarantine", ["opportunity_id"])
    # The WHEN clause skips the write when the column already agrees, so the common payload
    # update (a lease, a retry stamp) costs one comparison.
    op.execute(
        "CREATE TRIGGER trg_task_opportunity_ins AFTER INSERT ON task "
        f"WHEN {_WHEN} "
        f"BEGIN UPDATE task SET opportunity_id = {_EXPR} WHERE id = NEW.id; END"
    )
    op.execute(
        "CREATE TRIGGER trg_task_opportunity_upd AFTER UPDATE OF payload ON task "
        f"WHEN {_WHEN} "
        f"BEGIN UPDATE task SET opportunity_id = {_EXPR} WHERE id = NEW.id; END"
    )


    lane = (
        "CASE WHEN json_valid(NEW.payload) AND NEW.type IN ('t1', 't2') "
        "AND json_extract(NEW.payload, '$.opportunity_id') IS NOT NULL THEN "
        "(json_type(NEW.payload, '$.lane') IS NOT NULL "
        "OR json_extract(NEW.payload, '$.lane_local_only') = 1) ELSE 0 END"
    )
    body = (
        "BEGIN UPDATE opportunity SET lane_local_only = 1 "
        "WHERE id = json_extract(NEW.payload, '$.opportunity_id') AND lane_local_only IS NOT 1; END"
    )
    op.execute(f"CREATE TRIGGER trg_task_lane_ins AFTER INSERT ON task WHEN {lane} {body}")
    op.execute(
        f"CREATE TRIGGER trg_task_lane_upd AFTER UPDATE OF payload ON task WHEN {lane} {body}"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_task_lane_upd")
    op.execute("DROP TRIGGER IF EXISTS trg_task_lane_ins")
    op.execute("DROP TRIGGER IF EXISTS trg_task_opportunity_upd")
    op.execute("DROP TRIGGER IF EXISTS trg_task_opportunity_ins")
    op.drop_index("ix_quarantine_opportunity_id", table_name="quarantine")
    op.drop_index("ix_opportunity_scrape_run_id", table_name="opportunity")
    op.drop_index("ix_task_opportunity_id", table_name="task")
    op.drop_column("opportunity", "lane_local_only")
    op.drop_column("task", "opportunity_id")
