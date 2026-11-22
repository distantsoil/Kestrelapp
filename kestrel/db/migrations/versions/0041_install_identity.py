"""The install ID moves out of `setting` into its own one-row table (D-INST-15)

`install_identity` holds the one record (slot 1): the ID, when it was made and by which version.
Only kestrel.services.install_id reads or writes it, so no settings reader (overrides, doctor,
diagnostics, the API, MCP, the backup preview) can reach the ID, because it is not in their
table.

Upgrade moves a well-formed `install.id` row (a lowercase version 4 UUID; the shape check is
written out here, not imported, so this step never changes with the application code) and
deletes the setting row whatever it held. A damaged row is not moved: the app writes a new ID on
its next start, as it does for a damaged row today.

Downgrade puts a record back as the `install.id` setting row and drops the table.

Revision ID: 0041
Revises: 0040 (re-chained at the #208 merge; it was written on 0034)
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"  # re-chained at the #208 merge (D-REL-40 C3)
branch_labels = None
depends_on = None

_KEY = "install.id"
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_MAX = 512


def _record(raw: object) -> dict | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or len(raw) > _MAX:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    full = value.get("id")
    if not isinstance(full, str) or not _UUID4.match(full):
        return None
    created, version = value.get("created_at"), value.get("version")
    return {"id": full, "created_at": created if isinstance(created, str) else None,
            "version": version if isinstance(version, str) else None}


def upgrade() -> None:
    op.create_table(
        "install_identity",
        sa.Column("slot", sa.Integer, primary_key=True),
        sa.Column("install_id", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=True),
        sa.Column("version", sa.String, nullable=True),
        sa.Column("updated_at", sa.String, nullable=False),
        sa.CheckConstraint("slot = 1", name="ck_install_identity_one_row"),
    )
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT CAST(value AS BLOB), updated_at FROM setting WHERE key = :k"), {"k": _KEY}).first()
    rec = _record(row[0]) if row is not None else None
    if row is not None and rec is not None:
        bind.execute(sa.text(
            "INSERT INTO install_identity (slot, install_id, created_at, version, updated_at) "
            "VALUES (1, :i, :c, :v, :u)"),
            {"i": rec["id"], "c": rec["created_at"], "v": rec["version"],
             "u": row[1] if isinstance(row[1], str) else ""})
    bind.execute(sa.text("DELETE FROM setting WHERE key = :k"), {"k": _KEY})


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT install_id, created_at, version, updated_at FROM install_identity WHERE slot = 1"
    )).first()
    if row is not None:
        bind.execute(sa.text("DELETE FROM setting WHERE key = :k"), {"k": _KEY})
        bind.execute(sa.text(
            "INSERT INTO setting (key, value, updated_at) VALUES (:k, :v, :u)"),
            {"k": _KEY, "v": json.dumps({"id": row[0], "created_at": row[1], "version": row[2]}),
             "u": row[3]})
    op.drop_table("install_identity")
