"""Record whether a transmission's payload reached the provider

`transmit_escalation` decides whether a failed transmission may be retried, and the only fact
that matters is whether the payload left the machine. That cannot be read from the stored error
text: `run_guarded` writes `request error: ...` for EVERY exception that is not a
`ModelDeclined`, which includes `ProviderError` (HTTP 401/402/404/429/500, where the provider
received the entire payload) and a timeout waiting for a reply.

The retry test was exactly this string-prefix check, and it was wrong: a real 500 looked
retryable, the operator re-confirmed, and the payload egressed a second time -- reopening the
double-send that migration 0017's reservation exists to prevent.

So the fact is now recorded explicitly, decided by exception type in `llm.client._never_left`.
Backfill: existing rows are conservatively marked egressed=True, because a duplicate egress
cannot be undone and an old row has no way to prove otherwise. That is the safe default and it
only means pre-existing failed transmissions need a fresh preview rather than a retry.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "frontier_transmission",
        sa.Column("egressed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # server_default=True already backfills existing rows to "it went out"; stating it explicitly
    # documents the intent rather than relying on the DDL default.
    op.execute("UPDATE frontier_transmission SET egressed = 1 WHERE egressed IS NULL")


def downgrade() -> None:
    op.drop_column("frontier_transmission", "egressed")
