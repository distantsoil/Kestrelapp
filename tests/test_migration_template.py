"""The conftest serves "migrate an empty database" from a copy of one database the real Alembic
upgrade built. This proves the copy is the same database a real upgrade produces, and that the
real upgrade still runs. Cloud-safe: sqlite files only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kestrel.db import migrate


def _dump(path: Path) -> dict:
    """Structure and seed data, compared order-insensitively: the real upgrade itself emits a
    table's constraints in a different order from one run to the next, so raw table `sql` text
    cannot be compared. Triggers and views have no such ordering, so their `sql` is compared."""
    con = sqlite3.connect(path)
    try:
        objects = con.execute(
            "SELECT type, name, tbl_name FROM sqlite_master ORDER BY 1, 2"
        ).fetchall()
        out: dict = {"objects": objects}
        out["trigger_view_sql"] = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'view') ORDER BY 1"
        ).fetchall()
        for kind, name, _tbl in objects:
            quoted = name.replace('"', '""')
            if kind == "table":
                for pragma in ("table_info", "foreign_key_list", "index_list"):
                    rows = con.execute(f'PRAGMA {pragma}("{quoted}")').fetchall()
                    if pragma == "index_list":  # drop the autoindex numbering
                        rows = [(r[1], r[2], r[3], r[4]) for r in rows]
                    if pragma == "foreign_key_list":  # drop the per-table sequence numbers
                        rows = [r[2:] for r in rows]
                    out[(name, pragma)] = sorted(map(repr, rows))
                out[(name, "rows")] = con.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            elif kind == "index":
                xinfo = con.execute(f'PRAGMA index_xinfo("{quoted}")').fetchall()
                out[(name, "index_xinfo")] = xinfo
        out["alembic_version"] = con.execute("SELECT * FROM alembic_version").fetchall()
        out["user_version"] = con.execute("PRAGMA user_version").fetchone()[0]
        out["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        return out
    finally:
        con.close()


def test_the_copied_template_equals_a_real_upgrade(tmp_path):
    real = migrate.run_migrations.real  # type: ignore[attr-defined]
    copied = tmp_path / "copied.db"
    upgraded = tmp_path / "upgraded.db"
    migrate.run_migrations(copied)  # empty target: served from the template
    real(upgraded)
    assert _dump(copied) == _dump(upgraded)
    assert _dump(copied)["alembic_version"] == [(migrate.head_revision(),)]


def test_a_database_with_content_still_goes_through_the_real_upgrade(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE marker (x)")
    con.commit()
    con.close()
    migrate.run_migrations(db)
    dump = _dump(db)
    assert dump["alembic_version"] == [(migrate.head_revision(),)]
    assert any(row[1] == "marker" for row in dump["objects"]), "existing content must be kept"


def test_a_gated_module_runs_the_real_upgrade(tmp_path, monkeypatch):
    """Claim a module from _REAL_MIGRATION_MODULES and call the public entry point: the real
    alembic upgrade must run even though the target is empty."""
    called = []
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_db_schema.py::x (call)")
    monkeypatch.setattr(
        "alembic.command.upgrade", lambda *a, **k: called.append(a), raising=True
    )
    migrate.run_migrations(tmp_path / "x.db")
    assert called


def test_a_test_that_patches_alembic_still_sees_its_failure(tmp_path, monkeypatch):
    """The fast path must not hide an injected migration failure."""

    def boom(*a, **k):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr("alembic.command.upgrade", boom)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate.run_migrations(tmp_path / "fresh.db")


def test_a_patched_config_takes_the_real_path(tmp_path, monkeypatch):
    def broken_config(*a, **k):
        raise RuntimeError("config patched")

    monkeypatch.setattr(migrate, "_config", broken_config)
    with pytest.raises(RuntimeError, match="config patched"):
        migrate.run_migrations(tmp_path / "fresh2.db")
