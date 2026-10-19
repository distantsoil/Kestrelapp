"""The LAN shared key never travels in a backup, and a restore never changes this machine's key
(drift review Group 10, D6). The key lives in the `setting` table under
`server.shared_secret`, and a backup is a copy of the database, so it is stripped from the staged
copy before zipping. A restore puts back whatever key the machine had before it (or none), even
when an older backup still carries a different one. Cloud-safe: real migrated SQLite, no network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kestrel import paths
from kestrel.cli import app
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.services.backup import (
    DB_NAME,
    create_backup,
    create_backup_to_temp,
    discard_temp_backup,
    restore_backup,
    snapshot_database,
)
from kestrel.web.auth import SETTING_KEY

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

runner = CliRunner()


def _fresh_db() -> None:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _set_key(db: Path, value: str | None) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM setting WHERE key = ?", (SETTING_KEY,))
        if value is not None:
            con.execute(
                "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
                (SETTING_KEY, json.dumps(value), "2026-01-01T00:00:00+00:00"),
            )
        con.commit()
    finally:
        con.close()


def _get_key(db: Path) -> str | None:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,)).fetchone()
    finally:
        con.close()
    return None if row is None else json.loads(row[0])


def _zipped_key_rows(archive: Path, tmp: Path) -> list[tuple]:
    out = tmp / "unzipped"
    out.mkdir()
    with zipfile.ZipFile(archive) as zf:
        zf.extract(DB_NAME, out)
    con = sqlite3.connect(str(out / DB_NAME))
    try:
        rows = con.execute("SELECT * FROM setting WHERE key = ?", (SETTING_KEY,)).fetchall()
        others = con.execute("SELECT COUNT(*) FROM setting WHERE key = 'ui.scheme'").fetchone()
    finally:
        con.close()
    assert others[0] == 1  # the strip is surgical: other settings travel
    return rows


def _old_style_backup(tmp: Path, key: str | None) -> Path:
    """A backup as Kestrel made it before this fix: the database copied whole, key included."""
    db = paths.db_path()
    _set_key(db, key)
    snap = tmp / "old-snap.db"
    snapshot_database(db, snap)
    archive = tmp / "kestrel-backup-old.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, DB_NAME)
    return archive


def _seed_scheme(db: Path) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT OR REPLACE INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
            ("ui.scheme", json.dumps("paper"), "2026-01-01T00:00:00+00:00"),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------------- backup
def test_backup_strips_the_lan_key_and_leaves_the_live_db_alone(tmp_path):
    _fresh_db()
    db = paths.db_path()
    _seed_scheme(db)
    _set_key(db, "live-secret-123")

    archive = create_backup(tmp_path)

    assert _zipped_key_rows(archive, tmp_path) == []
    with zipfile.ZipFile(archive) as zf:
        for n in zf.namelist():
            assert b"live-secret-123" not in zf.read(n)
    assert _get_key(db) == "live-secret-123"  # the live database keeps its key


def test_backup_holds_no_bytes_of_an_old_or_current_key(tmp_path):
    """secure_delete on the staged copy zeroes only what THAT delete frees. The live database
    runs without it, so a rotated-out key can sit in free space or a freed cell, and the backup
    API copies pages verbatim. The staged copy is vacuumed, so neither key's bytes survive."""
    _fresh_db()
    db = paths.db_path()
    _seed_scheme(db)
    old = "OLD-lan-key-" + "a1b2c3d4" * 8
    new = "NEW-lan-key-" + "e5f6a7b8" * 8
    _set_key(db, old)
    con = sqlite3.connect(str(db))
    try:
        # Churn: filler rows beside the key, then gone, so pages and cells are freed unzeroed.
        for i in range(200):
            con.execute(
                "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
                (f"filler.{i}", json.dumps("x" * 200), "2026-01-01T00:00:00+00:00"),
            )
        con.commit()
        con.execute("DELETE FROM setting WHERE key LIKE 'filler.%'")
        # Rotate the key in place, the way the app changes it.
        con.execute("UPDATE setting SET value = ? WHERE key = ?", (json.dumps(new), SETTING_KEY))
        con.commit()
    finally:
        con.close()
    assert _get_key(db) == new

    archive = create_backup(tmp_path)

    with zipfile.ZipFile(archive) as zf:
        raw = zf.read(DB_NAME)
    assert old.encode() not in raw
    assert new.encode() not in raw
    assert _get_key(db) == new  # the live database keeps its key


def test_temp_backup_for_the_download_route_strips_the_key_too(tmp_path):
    _fresh_db()
    db = paths.db_path()
    _seed_scheme(db)
    _set_key(db, "live-secret-456")
    archive = create_backup_to_temp()
    try:
        assert _zipped_key_rows(archive, tmp_path) == []
    finally:
        discard_temp_backup(archive)
    assert _get_key(db) == "live-secret-456"


def test_cli_backup_strips_the_key(tmp_path):
    _fresh_db()
    db = paths.db_path()
    _seed_scheme(db)
    _set_key(db, "live-secret-789")
    result = runner.invoke(app, ["backup", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    (archive,) = tmp_path.glob("kestrel-backup-*.zip")
    assert _zipped_key_rows(archive, tmp_path) == []
    assert _get_key(db) == "live-secret-789"


# --------------------------------------------------------------------------------- restore
def test_restore_of_an_old_backup_keeps_this_machines_key(tmp_path):
    _fresh_db()
    db = paths.db_path()
    archive = _old_style_backup(tmp_path, "key-from-another-machine")
    _set_key(db, "this-machines-key")

    restore_backup(archive)

    assert _get_key(db) == "this-machines-key"


def test_restore_when_the_machine_has_no_key_leaves_none(tmp_path):
    _fresh_db()
    db = paths.db_path()
    archive = _old_style_backup(tmp_path, "key-from-another-machine")
    _set_key(db, None)

    restore_backup(archive)

    assert _get_key(db) is None


def test_cli_restore_keeps_this_machines_key(tmp_path, monkeypatch):
    from kestrel import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: False)
    _fresh_db()
    db = paths.db_path()
    archive = _old_style_backup(tmp_path, "key-from-another-machine")
    _set_key(db, "this-machines-key")

    result = runner.invoke(app, ["restore", str(archive)])

    assert result.exit_code == 0, result.output
    assert _get_key(db) == "this-machines-key"


def test_web_restore_keeps_this_machines_key(tmp_path):
    from kestrel.web import auth
    from kestrel.web.app import build_app

    _fresh_db()
    db = paths.db_path()
    archive = _old_style_backup(tmp_path, "key-from-another-machine")
    _set_key(db, "this-machines-key")

    with TestClient(build_app(worker=False)) as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("b.zip", archive.read_bytes(), "application/zip")},
        )
        assert r.status_code == 200
        assert "database restored: True" in r.text
        assert asyncio.run(auth.current_key()) == "this-machines-key"
    forget_engine()
    assert _get_key(db) == "this-machines-key"
