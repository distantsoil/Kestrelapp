"""One transmission per confirmed payload (Tier 3 double egress)

`transmit_escalation` (kestrel/pipeline/tier3.py) checks for a prior transmission, sends the
payload to the hosted frontier, and only then records the audit row. A read cannot close the
window between those steps, so two concurrent confirmations of one previewed payload (a
double-click, or an htmx retry racing the first response) both saw no prior row and both sent:
the operator's data left the machine twice and the provider billed twice, against a comment that
promises exactly one egress.

The fix reserves the row first (insert + commit with status "pending"), which only holds if the
database rejects the second one. This adds the constraint that makes that true, so the guarantee
does not depend on the application check alone.

Existing rows are rewritten, NOT deleted (D-RR1-3). The earliest row per
(opportunity_id, payload_sha256) keeps its hash; later ones have theirs suffixed "-dup2",
"-dup3", ... so the unique index can still be created and every row survives.

Deleting them was the first version of this migration, on the reasoning that "a duplicate can
only exist from the race this migration closes". That is true, and it does not address that the
race DID happen in the field. Those rows are the audit record of the incident: each carries its
own cost_usd and evaluation_id, so deleting them destroys the only evidence that the operator's
data left the machine twice. This is why the decision was made before shipping rather than after:
once a user's database has run the delete, those rows cannot be recovered.

The EARLIEST row keeps the unsuffixed hash deliberately. `transmit_escalation`'s replay guard
looks up the prior transmission by exact payload_sha256, so suffixing the canonical row would stop
the guard matching the very send it exists to protect against.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_INDEX = "ux_frontier_transmission_payload"
_TABLE = "frontier_transmission"
# Scratch table holding the ORIGINAL hashes plus each row's rank within its duplicate group. It
# must be a snapshot: a correlated subquery reading frontier_transmission directly would see rows
# this statement has already rewritten, and the ranks would be wrong from the second duplicate on.
_RANK = "_kestrel_ft_dupe_rank"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TEMP TABLE {_RANK} AS
        SELECT
            id,
            payload_sha256,
            ROW_NUMBER() OVER (
                PARTITION BY opportunity_id, payload_sha256 ORDER BY id
            ) AS rn
        FROM {_TABLE}
        """
    )
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET payload_sha256 = (
            SELECT r.payload_sha256 || '-dup' || r.rn
            FROM {_RANK} r
            WHERE r.id = {_TABLE}.id
        )
        WHERE id IN (SELECT id FROM {_RANK} WHERE rn > 1)
        """
    )
    op.execute(f"DROP TABLE {_RANK}")
    op.create_index(
        _INDEX, _TABLE, ["opportunity_id", "payload_sha256"], unique=True
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
