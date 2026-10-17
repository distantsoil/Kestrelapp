"""contact.former_orgs — prior employers, for warm-path tiers 2 and 4

The LinkedIn CSV export gives only a connection's CURRENT company; the operator or P6 discovery can
add prior employers here so "formerly at the org" and "shares a former employer with a decision
maker" (SPEC-MODULES 2.3) are computable graph queries.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contact", sa.Column("former_orgs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("contact", "former_orgs")
