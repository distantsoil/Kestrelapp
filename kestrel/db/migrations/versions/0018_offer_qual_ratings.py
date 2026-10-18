"""Operator ratings for the offer comparison's qualitative axes

The comparison matrix (kestrel/negotiation/compare.py) has seven axes: two quantitative
(first-year comp, four-year value) computed from the recorded offer, and five qualitative
(level/scope, reporting line, location/travel, organizational risk, trajectory fit) that are
the OPERATOR's judgment. The module was built for them -- OfferComp.qual and the QUAL_AXES
inversion for org_risk both exist -- but nothing ever produced a rating, so all five read None
and were dropped from the weighted total. The matrix ranked offers purely on money.

Those ratings are per offer, so they belong on the offer row: a nullable JSON column mapping an
axis name to a 0-5 rating. Nullable so an offer recorded without them is still an offer, and the
matrix renormalizes over the axes a given offer actually has (compare_offers), so a missing
rating lowers nothing unfairly.

Weights are NOT here. They are the operator's preference across the matrix rather than a
property of any one offer, so they live in the setting table (see offers.py).

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_TABLES = ("offer", "engagement_offer")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("qual_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "qual_json")
