"""D-DOC-7 / migration 0025: a selection_override row outlives its render. Up and down on a
database that already holds rows, plus the schema the model describes."""

from __future__ import annotations

import sqlite3

from alembic import command

from kestrel import paths
from kestrel.db import migrate


def _go(db, direction: str, rev: str) -> None:
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.upgrade if direction == "up" else command.downgrade)(cfg, rev)


def _ins(con, table: str, **vals) -> None:
    """Insert one row, filling any other NOT NULL column that has no default."""
    info = con.execute(f"PRAGMA table_info({table})").fetchall()
    for _cid, name, typ, notnull, default, pk in info:
        if name in vals or not notnull or default is not None or pk:
            continue
        vals[name] = 0 if "INT" in typ.upper() or "BOOL" in typ.upper() else (
            "[]" if "JSON" in typ.upper() else "x")
    cols = ", ".join(vals)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(vals))})",
                list(vals.values()))


def _seed(db) -> None:
    con = sqlite3.connect(db)
    _ins(con, "opportunity", id=1, track="employment", opp_type="job_posting", title="CIO",
         org_name="Acme", url="https://e.test/1", stage="t2_done")
    _ins(con, "doc_master", id=1, track="employment", name="t", doc_kind="resume",
         header="{}", summary_variants='[{"key":"s1","text":"Summary one"}]', skill_groups="[]")
    _ins(con, "doc_section", id=1, doc_master_id=1, section_kind="role", sort_order=0)
    _ins(con, "doc_slot", id=1, doc_section_id=1, key="r1_a", sort_order=0, optional=0,
         default_variant_key="original")
    _ins(con, "doc_variant", id=1, doc_slot_id=1, key="original", text="Original wording",
         tags="[]", derived=0)
    _ins(con, "doc_variant", id=2, doc_slot_id=1, key="r1", text="Rewritten wording",
         tags="[]", derived=0)
    _ins(con, "doc_render", id=1, opportunity_id=1, doc_master_id=1, config="{}")
    for i, (opp, slot, m, o) in enumerate((
        (None, "r1_a", "original", "r1"), (1, "summary", None, "s1"), (1, "skills", "show", "hide"),
    ), start=1):
        _ins(con, "selection_override", id=i, doc_render_id=1, opportunity_id=opp, slot_key=slot,
             model_choice=m, operator_choice=o)
    con.commit()
    con.close()


def _upgraded(tmp_path):
    db = tmp_path / "m.db"
    _go(db, "up", "0024")
    _seed(db)
    _go(db, "up", "0025")
    return db


def test_upgrade_keeps_rows_and_backfills_the_opportunity_and_the_wordings(tmp_path):
    db = _upgraded(tmp_path)
    con = sqlite3.connect(db)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT id, doc_render_id, opportunity_id, model_choice_text, operator_choice_text"
        " FROM selection_override")}
    assert rows[1] == (1, 1, "Original wording", "Rewritten wording")
    assert rows[2] == (1, 1, None, "Summary one")
    assert rows[3] == (1, 1, None, None)


def test_doc_render_id_is_nullable_and_set_null_on_render_delete(tmp_path):
    db = _upgraded(tmp_path)
    con = sqlite3.connect(db)
    info = {r[1]: r for r in con.execute("PRAGMA table_info(selection_override)")}
    assert info["doc_render_id"][3] == 0  # notnull off
    fks = {r[3]: r[6] for r in con.execute("PRAGMA foreign_key_list(selection_override)")}
    assert fks["doc_render_id"] == "SET NULL"
    assert ("ix_selection_override_doc_render_id",) in [
        (r[1],) for r in con.execute("PRAGMA index_list(selection_override)")]
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("DELETE FROM doc_render WHERE id=1")
    con.commit()
    assert con.execute("SELECT count(*), count(doc_render_id) FROM selection_override"
                       ).fetchone() == (3, 0)


def test_downgrade_drops_detached_rows_and_restores_the_old_shape(tmp_path):
    db = _upgraded(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE selection_override SET doc_render_id = NULL WHERE id = 3")
    con.commit()
    con.close()
    _go(db, "down", "0024")
    con = sqlite3.connect(db)
    info = {r[1]: r for r in con.execute("PRAGMA table_info(selection_override)")}
    assert info["doc_render_id"][3] == 1
    assert "model_choice_text" not in info and "operator_choice_text" not in info
    assert [r[0] for r in con.execute("SELECT id FROM selection_override ORDER BY id")] == [1, 2]
    fks = {r[3]: r[6] for r in con.execute("PRAGMA foreign_key_list(selection_override)")}
    assert fks["doc_render_id"] == "NO ACTION"


def test_choice_texts_copies_the_wording_behind_each_key(monkeypatch):
    import asyncio

    from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
    from kestrel.db.models import DocMaster
    from kestrel.learning.capture import choice_texts
    from tests.test_documents_editor import ORIGINAL, _app

    _client, mid = _app(monkeypatch)

    async def go():
        await reset_engine()
        async with get_sessionmaker()() as s:
            m = await s.get(DocMaster, mid)
            m.summary_variants = [{"key": "s1", "text": "Summary one"}]
            await s.commit()
            slot = await choice_texts(s, mid, "r1_enterprise_technology", "original", "nope")
            summ = await choice_texts(s, mid, "summary", None, "s1")
            skills = await choice_texts(s, mid, "skills:core", "show", "hide")
            return slot, summ, skills

    slot, summ, skills = asyncio.run(go())
    forget_engine()
    assert slot == {"model_choice_text": ORIGINAL, "operator_choice_text": None}
    assert summ == {"model_choice_text": None, "operator_choice_text": "Summary one"}
    assert skills == {"model_choice_text": None, "operator_choice_text": None}


# ------------------------------------------------------------------ a real database's leftovers
def _orphan_db(tmp_path):
    db = tmp_path / "o.db"
    _go(db, "up", "0024")
    _seed(db)
    con = sqlite3.connect(db)  # foreign keys are off on this connection, as when the row got in
    _ins(con, "selection_override", id=9, doc_render_id=77, opportunity_id=1, slot_key="r1_a",
         model_choice="original", operator_choice="r1")
    con.commit()
    con.close()
    return db


def test_an_orphan_row_is_kept_with_no_render_and_the_upgrade_succeeds(tmp_path):
    db = _orphan_db(tmp_path)
    _go(db, "up", "0025")
    con = sqlite3.connect(db)
    assert con.execute("SELECT doc_render_id, opportunity_id FROM selection_override WHERE id=9"
                       ).fetchone() == (None, 1)
    assert con.execute("SELECT count(*) FROM selection_override").fetchone() == (4,)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    tmp = "_alembic_tmp_selection_override"
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (tmp,)).fetchone() is None
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == ("0025",)


def test_a_leftover_temporary_table_from_a_crashed_run_does_not_block_the_upgrade(tmp_path):
    db = tmp_path / "t.db"
    _go(db, "up", "0024")
    _seed(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE _alembic_tmp_selection_override (id INTEGER PRIMARY KEY, junk TEXT)")
    con.commit()
    con.close()
    _go(db, "up", "0025")
    con = sqlite3.connect(db)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='_alembic_tmp_selection_override'"
                       ).fetchone() is None
    assert con.execute("SELECT count(*) FROM selection_override").fetchone() == (3,)


def test_there_is_one_alembic_head():
    from alembic.script import ScriptDirectory

    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        script = ScriptDirectory(str(mig_dir))
        assert script.get_heads() == [script.get_current_head()] and len(script.get_heads()) == 1


def test_a_row_pointing_at_a_deleted_opportunity_does_not_block_the_upgrade(tmp_path):
    """Review: rows whose opportunity is gone failed the rebuild under foreign_keys=ON, so the
    app could not start. The row is kept and its dead link cleared; a render pointing at a gone
    opportunity is never copied in by the backfill either."""
    db = tmp_path / "p.db"
    _go(db, "up", "0024")
    _seed(db)
    con = sqlite3.connect(db)  # foreign keys off on this connection, as when the rows got in
    _ins(con, "selection_override", id=10, doc_render_id=1, opportunity_id=555, slot_key="r1_a",
         model_choice="original", operator_choice="r1")
    _ins(con, "doc_render", id=2, opportunity_id=556, doc_master_id=1, config="{}")
    _ins(con, "selection_override", id=11, doc_render_id=2, opportunity_id=None, slot_key="r1_a",
         model_choice="original", operator_choice="r1")
    con.commit()
    con.close()
    _go(db, "up", "0025")
    con = sqlite3.connect(db)
    got = dict(con.execute(
        "SELECT id, opportunity_id FROM selection_override WHERE id IN (10, 11)"))
    # 10: the dead link is cleared, then refilled from its live render; 11: its render points at
    # a gone opportunity, so nothing is copied in.
    assert got == {10: 1, 11: None}
    assert con.execute("SELECT count(*) FROM selection_override").fetchone() == (5,)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == ("0025",)


def test_a_summary_variants_value_that_is_not_a_list_does_not_break_the_backfill(tmp_path):
    db = tmp_path / "s.db"
    _go(db, "up", "0024")
    _seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE doc_master SET summary_variants = 'null' WHERE id = 1")
    con.commit()
    con.close()
    _go(db, "up", "0025")
    con = sqlite3.connect(db)
    assert con.execute("SELECT operator_choice_text FROM selection_override WHERE id = 2"
                       ).fetchone() == (None,)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone() == ("0025",)
