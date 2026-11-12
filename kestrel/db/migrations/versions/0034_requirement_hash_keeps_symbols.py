"""requirement_correction.requirement_hash keeps C++, C# and every script (D-LEARN-11)

The identity form of a requirement now keeps letters and digits of any script and `+` and `#`,
so "C++", "C#" and "C" and two different Cyrillic requirements no longer share one correction.
This re-derives the stored hash of every row from its own `requirement_text`. The new form is
finer than the old one (two texts equal under it were equal under the old), so no two rows merge
and the two partial unique indexes still hold. The normalization is written out here, not
imported, so this step never changes with the application code.

A stored text that is not text (a BLOB, which Kestrel never writes but an older backup can carry)
is read as UTF-8 with bad bytes replaced and written back as that text before it is hashed, so a
restore never fails here on the value's type (D-DB-3).

Downgrade gives back the old form wherever it can. Rows the old form cannot tell apart ("C++" and
"C#" on one track, both "c" in the old form) cannot all hold it under the unique indexes, so in
each such group exactly one row gets it: the row whose text already has the old form ("C"), else
the oldest (lowest id). The others keep today's form: nothing is merged or deleted, the old build
simply finds no requirement with that hash, and upgrading again re-derives every row (D-DB-3).

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import hashlib
import logging
import re

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_NEW = re.compile(r"[^\w+#]+|_+")
_OLD = re.compile(r"[^a-z0-9]+")


def _as_text(value: object) -> str:
    """The stored value as text, whatever its storage class (D-DB-3)."""
    if value is None:
        return ""
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("utf-8", "replace")
    return value if isinstance(value, str) else str(value)


def _new(text: str) -> str:
    return " ".join(_NEW.sub(" ", text.lower()).split())


def _old(text: str) -> str:
    return _OLD.sub(" ", text.lower()).strip()


def _digest(form: str) -> str:
    return hashlib.sha1(form.encode("utf-8"), usedforsecurity=False).hexdigest()


def _rows(conn) -> list[tuple[int, object, str, str, object, str]]:
    return conn.execute(sa.text(
        "SELECT id, requirement_text, typeof(requirement_text), track, opportunity_id, scope "
        "FROM requirement_correction ORDER BY id"
    )).all()


def _clear(conn) -> None:
    """Give every row a placeholder unique to it first, so no step in between can meet a hash
    another row still holds (a hand-made older database may hold any value there)."""
    conn.execute(sa.text("UPDATE requirement_correction SET requirement_hash = 'pending:' || id"))


def upgrade() -> None:
    conn = op.get_bind()
    _clear(conn)
    for rid, raw, kind, _track, _opp, _scope in _rows(conn):
        text = _as_text(raw)
        params = {"h": _digest(_new(text)), "i": rid}
        if kind not in ("text", "null"):  # a BLOB or a number: stored back as its text
            conn.execute(sa.text(
                "UPDATE requirement_correction SET requirement_text = :t, requirement_hash = :h "
                "WHERE id = :i"), {**params, "t": text})
        else:
            conn.execute(sa.text(
                "UPDATE requirement_correction SET requirement_hash = :h WHERE id = :i"), params)


def downgrade() -> None:
    conn = op.get_bind()
    rows = _rows(conn)
    _clear(conn)
    # The unique groups of migration 0032: one `person` row per (track, hash) and one `posting`
    # row per (opportunity_id, hash). A NULL opportunity is never equal to another in SQLite.
    groups: dict[tuple, list[tuple[int, str]]] = {}
    for rid, raw, _kind, track, opp, scope in rows:
        text = _as_text(raw)
        old = _old(text)
        if scope == "person":
            key: tuple = ("person", track, old)
        elif scope == "posting" and opp is not None:
            key = ("posting", opp, old)
        else:
            key = ("row", rid)
        groups.setdefault(key, []).append((rid, text))
    kept_new = 0
    for members in groups.values():
        # Ordered by id. The row whose text already has the old form takes it (its hash is the
        # same in both forms); else the oldest. Only one row of a group can have it (the index
        # holds today), and a row whose today's form differs from its old form has a character
        # the old form never contains, so the rows left in today's form collide with nothing.
        winner = next((rid for rid, text in members if _new(text) == _old(text)), members[0][0])
        for rid, text in members:
            form = _old(text) if rid == winner else _new(text)
            kept_new += rid != winner
            conn.execute(
                sa.text("UPDATE requirement_correction SET requirement_hash = :h WHERE id = :i"),
                {"h": _digest(form), "i": rid},
            )
    if kept_new:
        log.info("0034 downgrade: %d requirement corrections kept today's form (the old form "
                 "could not tell them apart from another row)", kept_new)
