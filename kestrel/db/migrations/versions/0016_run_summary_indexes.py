"""Indexes for the live run summary (D-UI-58)

The run summary (kestrel.analytics.run_summary) is polled every 5 s while a scan or scoring runs
and reads the rows a run produced by time: evaluations and quarantine rows by `created_at`, scrape
runs by `started_at`, and the newest task touch by `updated_at`. Without these each poll scanned
the whole table. No table, column or row changes: the summary is derived from rows that already
exist, so it survives a restart (hard rule 5).

Additive only: four indexes.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_evaluation_created", "evaluation", ["created_at"]),
    ("ix_quarantine_created", "quarantine", ["created_at"]),
    ("ix_scrape_run_started", "scrape_run", ["started_at"]),
    ("ix_task_updated", "task", ["updated_at"]),
)


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _cols in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
