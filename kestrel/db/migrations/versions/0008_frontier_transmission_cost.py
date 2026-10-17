"""frontier_transmission: what the provider actually billed

With a bring-your-own-key hosted backend (SPEC.md 6.4, OpenRouter or openmodel.ai) every Tier 3
send costs the operator real money. The audit row already records what left the machine; it now
also records what came back on the bill: the provider's own token counts and, where the provider
reports it (OpenRouter with `usage.include`), the USD cost. All three are nullable because a
provider that reports nothing must show as "unknown", never as zero.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("frontier_transmission") as batch:
        batch.add_column(sa.Column("prompt_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("completion_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cost_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("frontier_transmission") as batch:
        batch.drop_column("cost_usd")
        batch.drop_column("completion_tokens")
        batch.drop_column("prompt_tokens")
