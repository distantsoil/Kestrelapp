"""selection_override outlives its render (D-DOC-7)

Deleting a resume keeps the choices Kestrel learned from it unless the person asks to forget them.
A kept row must not point at a deleted render, so `doc_render_id` becomes nullable with
ON DELETE SET NULL. The key columns (`model_choice`, `operator_choice`) name variants that go with
the resume, so the wording behind each is copied onto the row (`model_choice_text`,
`operator_choice_text`), and `opportunity_id` is filled from the render where it was empty. Both
are backfilled here for the rows that exist; new rows carry them from the moment they are written.

SQLite cannot change nullability in place, so the table is rebuilt (batch mode). Nothing references
selection_override, but the table itself holds a foreign key, and a real database can hold an
orphan (a doc_render_id whose render is gone, written while foreign keys were off). Copying that
row into the rebuilt table fails under foreign_keys=ON and leaves `_alembic_tmp_selection_
override` behind, so every later start would fail too. SQLite's own rebuild procedure turns
foreign key enforcement off, but `PRAGMA foreign_keys` is a no-op inside a transaction, and Alembic
has usually opened one by now (earlier migrations wrote rows), as does the deferred-check pragma
outside one. So no pragma is relied on: the orphans are set aside in a temporary table before the
rebuild and put back afterwards, kept, with `doc_render_id` NULL (the column is nullable by then).
`foreign_key_check` confirms none is left. A leftover `_alembic_tmp_selection_override` from an
interrupted earlier run is dropped first, and columns that already exist are not added twice, so
running this again is safe.

The downgrade deletes rows whose render is gone (NOT NULL cannot hold them) and drops the two text
columns.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


_TMP = "_alembic_tmp_selection_override"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_TMP}"))
    have = {c["name"] for c in sa.inspect(conn).get_columns("selection_override")}
    orphan = ("doc_render_id IS NOT NULL AND NOT EXISTS"
              " (SELECT 1 FROM doc_render r WHERE r.id = selection_override.doc_render_id)")
    conn.execute(sa.text("DROP TABLE IF EXISTS temp._so_orphans"))
    conn.execute(sa.text(
        f"CREATE TEMP TABLE _so_orphans AS SELECT * FROM selection_override WHERE {orphan}"))
    conn.execute(sa.text(f"DELETE FROM selection_override WHERE {orphan}"))
    # A row can also point at an opportunity that is gone (same foreign-keys-off history). Copying
    # it into the rebuilt table would fail the same way, so its link is cleared, the row kept.
    conn.execute(sa.text(
        "UPDATE selection_override SET opportunity_id = NULL WHERE opportunity_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM opportunity p"
        " WHERE p.id = selection_override.opportunity_id)"))
    with op.batch_alter_table("selection_override", naming_convention=_NAMING) as batch:
        if "model_choice_text" not in have:
            batch.add_column(sa.Column("model_choice_text", sa.Text(), nullable=True))
        if "operator_choice_text" not in have:
            batch.add_column(sa.Column("operator_choice_text", sa.Text(), nullable=True))
        batch.alter_column("doc_render_id", existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint("fk_selection_override_doc_render_id_doc_render", type_="foreignkey")
        batch.create_foreign_key(
            "fk_selection_override_doc_render_id_doc_render",
            "doc_render", ["doc_render_id"], ["id"], ondelete="SET NULL",
        )
    # Put the orphans back, kept, with no render. The two wording columns are new (NULL).
    conn.execute(sa.text(
        "INSERT INTO selection_override (id, doc_render_id, opportunity_id, slot_key,"
        " model_choice, operator_choice, created_at)"
        " SELECT id, NULL, CASE WHEN EXISTS (SELECT 1 FROM opportunity p"
        " WHERE p.id = _so_orphans.opportunity_id) THEN opportunity_id END,"
        " slot_key, model_choice, operator_choice, created_at"
        " FROM temp._so_orphans"))
    conn.execute(sa.text("DROP TABLE temp._so_orphans"))
    left = conn.execute(sa.text("PRAGMA foreign_key_check(selection_override)")).all()
    if left:
        raise RuntimeError("selection_override still points at a missing row after the repair")
    _backfill(conn)


def _backfill(conn) -> None:
    conn.execute(sa.text(
        "UPDATE selection_override SET opportunity_id = (SELECT r.opportunity_id FROM doc_render r"
        " JOIN opportunity p ON p.id = r.opportunity_id"
        " WHERE r.id = selection_override.doc_render_id) WHERE opportunity_id IS NULL"
    ))
    rows = conn.execute(sa.text(
        "SELECT o.id, o.slot_key, o.model_choice, o.operator_choice, r.doc_master_id"
        " FROM selection_override o JOIN doc_render r ON r.id = o.doc_render_id"
    )).all()
    summaries: dict[int, dict[str, str]] = {}
    slots: dict[tuple[int, str], dict[str, str]] = {}
    for oid, slot_key, model_choice, operator_choice, master_id in rows:
        if slot_key == "skills" or slot_key.startswith("skills:"):
            continue
        if slot_key == "summary":
            if master_id not in summaries:
                raw = conn.execute(sa.text("SELECT summary_variants FROM doc_master WHERE id = :m"),
                                   {"m": master_id}).scalar()
                try:
                    variants = json.loads(raw) if isinstance(raw, str) else (raw or [])
                except ValueError:
                    variants = []
                if not isinstance(variants, list):  # JSON null or a bare value: no wordings
                    variants = []
                summaries[master_id] = {
                    str(v["key"]): str(v["text"]) for v in variants
                    if isinstance(v, dict) and v.get("key") is not None and v.get("text")
                }
            texts = summaries[master_id]
        else:
            if (master_id, slot_key) not in slots:
                slots[(master_id, slot_key)] = {k: t for k, t in conn.execute(sa.text(
                    "SELECT v.key, v.text FROM doc_variant v"
                    " JOIN doc_slot sl ON sl.id = v.doc_slot_id"
                    " JOIN doc_section sec ON sec.id = sl.doc_section_id"
                    " WHERE sec.doc_master_id = :m AND sl.key = :k"
                ), {"m": master_id, "k": slot_key}).all() if t}
            texts = slots[(master_id, slot_key)]
        conn.execute(
            sa.text("UPDATE selection_override SET model_choice_text = :m,"
                    " operator_choice_text = :o WHERE id = :i"),
            {"m": texts.get(model_choice) if model_choice else None,
             "o": texts.get(operator_choice) if operator_choice else None, "i": oid},
        )


def downgrade() -> None:
    op.execute("DELETE FROM selection_override WHERE doc_render_id IS NULL")
    with op.batch_alter_table("selection_override", naming_convention=_NAMING) as batch:
        batch.drop_column("operator_choice_text")
        batch.drop_column("model_choice_text")
        batch.alter_column("doc_render_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_constraint("fk_selection_override_doc_render_id_doc_render", type_="foreignkey")
        batch.create_foreign_key(
            "fk_selection_override_doc_render_id_doc_render",
            "doc_render", ["doc_render_id"], ["id"],
        )
