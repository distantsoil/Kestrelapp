"""Backup and restore (kestrel.services.backup, shared by `kestrel backup`/`kestrel restore` and
the /settings web restore action). Cloud-safe: pure file I/O plus a real migrated SQLite db, no
Ollama/Playwright/network.

THE GATE (test_roundtrip_after_wipe_service_level and test_cli_backup_then_restore_roundtrip):
populate the db with a couple of Org rows, run backup, wipe/change the db, restore from the zip,
and assert the original rows are back. Also covers: the zip layout (db + config + documents, never
.env), refusing a non-Kestrel zip and an unsafe path entry before touching anything, and the web
upload endpoint never 500ing on a bad file."""

from __future__ import annotations

import asyncio
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

from kestrel import paths
from kestrel.cli import app
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Org
from kestrel.services.backup import InvalidBackupError, create_backup, restore_backup

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

runner = CliRunner()


# ------------------------------------------------------------------------------------- helpers
def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed_two_orgs() -> None:
    """A fresh migrated db (function-scoped _isolate_data_home gives each test its own app-db) with
    two Org rows -- the "couple of rows" the gate populates before backing up."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(name="Acme Robotics", normalized_name="acme robotics"))
        s.add(Org(name="Beacon Health", normalized_name="beacon health"))
        await s.commit()
    await engine.dispose()
    forget_engine()  # the file is about to be zipped/replaced; nothing may hold it open


async def _wipe_and_reseed_other() -> None:
    """Simulate the database changing after the backup was taken: a fresh db with DIFFERENT
    content, so a restore that merely no-ops would be caught."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(name="Someone Else", normalized_name="someone else"))
        await s.commit()
    await engine.dispose()
    forget_engine()


async def _org_names() -> set[str]:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        names = (await s.execute(select(Org.normalized_name))).scalars().all()
    await engine.dispose()
    return set(names)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


# ------------------------------------------------------------------------------------- backup
def test_backup_zip_contains_db_config_documents_never_env(tmp_path):
    asyncio.run(_seed_two_orgs())
    paths.seed_config()  # ensure at least settings.yml exists in the writable config dir

    dotenv = paths.dotenv_path()
    dotenv.write_text("KESTREL_SECRET=do-not-leak\n", encoding="utf-8")

    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake resume\n")
    prep_dir = paths.data_dir() / "prep_packs"
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "1_prep_pack.pdf").write_bytes(b"%PDF-1.4 fake prep pack\n")

    try:
        archive = create_backup(tmp_path)
        assert archive.exists()

        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "kestrel.db" in names
            assert any(n.startswith("config/") and n.endswith(".yml") for n in names)
            assert "documents/documents/resume.pdf" in names
            assert "documents/prep_packs/1_prep_pack.pdf" in names
            assert not any(".env" in n for n in names)  # secrets never leave the machine
            for n in names:
                assert b"do-not-leak" not in zf.read(n)
    finally:
        dotenv.unlink(missing_ok=True)  # never leave a stray .env for other tests


def test_backup_under_an_open_write_transaction_is_a_consistent_readable_db(tmp_path):
    """The database is zipped as a sqlite3 online-backup snapshot, not a file copy: a writer
    mid-transaction (the serve loop) neither blocks the backup nor leaks uncommitted rows into
    it, and the copy is readable on its own with no -wal/-shm beside it."""
    import sqlite3

    asyncio.run(_seed_two_orgs())
    db = paths.db_path()
    writer = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO org (name, normalized_name, first_seen_at, last_seen_at) "
            "VALUES ('Uncommitted Inc', 'uncommitted inc', '2026-10-12', '2026-10-12')"
        )
        archive = create_backup(tmp_path)  # the write transaction is still open
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "kestrel.db" in names
        assert not any(n.endswith(("-wal", "-shm")) for n in names)
        out = tmp_path / "unpacked.db"
        out.write_bytes(zf.read("kestrel.db"))
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = {r[0] for r in conn.execute("SELECT normalized_name FROM org")}
    finally:
        conn.close()
    assert rows == {"acme robotics", "beacon health"}  # committed rows only, every one of them


# ------------------------------------------------------------------------------- THE GATE (1/2)
def test_roundtrip_after_wipe_service_level(tmp_path):
    """Populate -> backup -> wipe/change the db -> restore -> the original rows are back."""
    asyncio.run(_seed_two_orgs())
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}

    archive = create_backup(tmp_path)

    asyncio.run(_wipe_and_reseed_other())
    assert asyncio.run(_org_names()) == {"someone else"}  # confirms the db really changed

    result = restore_backup(archive)
    assert result.db_restored is True
    assert result.pre_restore_backup is not None
    assert result.pre_restore_backup.exists()  # the pre-restore db (the "someone else" state)

    forget_engine()  # a restored file must not be read through a stale cached connection
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


# ------------------------------------------------------------------------------- THE GATE (2/2)
def test_cli_backup_then_restore_roundtrip(tmp_path, monkeypatch):
    """The same round trip, driven through the actual `kestrel backup` / `kestrel restore` CLI."""
    from kestrel import cli as cli_mod

    # `kestrel restore` refuses while the desktop app is running; whether one happens to be open
    # on the machine running the tests must not decide this one.
    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: False)
    asyncio.run(_seed_two_orgs())

    result = runner.invoke(app, ["backup", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "backup written to" in result.output
    zips = list(tmp_path.glob("kestrel-backup-*.zip"))
    assert len(zips) == 1

    asyncio.run(_wipe_and_reseed_other())

    restored = runner.invoke(app, ["restore", str(zips[0])])
    assert restored.exit_code == 0, restored.output
    assert "database restored: True" in restored.output
    assert "previous database preserved at" in restored.output
    assert "Restore complete" in restored.output

    forget_engine()
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


# ------------------------------------------------------------------------------------ safety
def test_restore_refuses_a_non_kestrel_zip_and_touches_nothing(tmp_path):
    bogus = tmp_path / "not-a-backup.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("readme.txt", "just some other zip, not a Kestrel backup")

    db = paths.db_path()
    existed_before = db.exists()
    pre_restore = db.with_name(db.name + ".pre-restore")
    pre_restore.unlink(missing_ok=True)

    with pytest.raises(InvalidBackupError, match="does not look like a Kestrel backup"):
        restore_backup(bogus)

    # Refused before anything real was touched: no pre-restore copy, db existence unchanged.
    assert not pre_restore.exists()
    assert db.exists() == existed_before


def test_restore_refuses_a_path_traversal_entry(tmp_path):
    bogus = tmp_path / "traversal.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("kestrel.db", "looks like a db entry")
        z.writestr("config/../../evil.yml", "pwn: true")

    with pytest.raises(InvalidBackupError, match="unsafe"):
        restore_backup(bogus)


def test_restore_refuses_a_missing_file():
    with pytest.raises(InvalidBackupError, match="The backup file was not found"):
        restore_backup("this/path/does/not/exist.zip")


def test_restore_refuses_a_corrupt_zip(tmp_path):
    not_a_zip = tmp_path / "corrupt.zip"
    not_a_zip.write_bytes(b"this is definitely not a zip file")

    with pytest.raises(InvalidBackupError, match="Not a valid zip"):
        restore_backup(not_a_zip)


def test_restore_refuses_a_db_that_is_not_sqlite(tmp_path):
    """A well-formed zip whose kestrel.db holds garbage. Structure checks pass, so without the
    content check this overwrote a working database and reported db_restored=True."""
    bogus = tmp_path / "garbage-db.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("kestrel.db", b"not a database")

    db = paths.db_path()
    migrate.run_migrations()  # so "the live db is untouched" is a real claim
    db_before = db.read_bytes()
    pre_restore = db.with_name(db.name + ".pre-restore")
    pre_restore.unlink(missing_ok=True)

    with pytest.raises(InvalidBackupError, match="not a usable SQLite database"):
        restore_backup(bogus)

    # Refused during staging: the live database is byte-identical and nothing was copied aside.
    assert db.read_bytes() == db_before
    assert not pre_restore.exists()


def test_restore_refuses_a_db_without_kestrel_tables(tmp_path):
    """A real SQLite file, but not ours. Installing it would look successful and then break
    every query, so the expected tables are checked too."""
    bogus = tmp_path / "wrong-schema.zip"
    other = tmp_path / "other.db"
    con = sqlite3.connect(str(other))
    con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with zipfile.ZipFile(bogus, "w") as z:
        z.write(other, "kestrel.db")

    with pytest.raises(InvalidBackupError, match="not a Kestrel database"):
        restore_backup(bogus)


def test_restore_refuses_an_oversized_entry(tmp_path, monkeypatch):
    """A decompression bomb is refused from the zip's central directory, before extraction."""
    import kestrel.services.backup as backup_mod

    monkeypatch.setattr(backup_mod, "MAX_MEMBER_BYTES", 16)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w") as z:
        z.writestr("kestrel.db", b"x" * 64)

    with pytest.raises(InvalidBackupError, match="oversized entry"):
        restore_backup(bomb)


def test_config_and_documents_are_restored_to_their_real_locations(tmp_path):
    asyncio.run(_seed_two_orgs())
    paths.seed_config()

    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "resume.pdf").write_bytes(b"original resume bytes")

    archive = create_backup(tmp_path)

    # Wipe the config and the document too, so restore is proven to recreate them, not merely
    # leave them alone.
    settings_yml = paths.config_dir() / "settings.yml"
    original_settings_text = settings_yml.read_text(encoding="utf-8")
    settings_yml.unlink()
    (docs_dir / "resume.pdf").unlink()

    result = restore_backup(archive)
    assert "settings.yml" in result.config_files
    assert "documents/resume.pdf" in result.document_files
    assert settings_yml.read_text(encoding="utf-8") == original_settings_text
    assert (docs_dir / "resume.pdf").read_bytes() == b"original resume bytes"


# ------------------------------------------------------------------------------------ web page
def test_web_restore_endpoint_round_trips(tmp_path):
    asyncio.run(_seed_two_orgs())
    archive = create_backup(tmp_path)
    zip_bytes = archive.read_bytes()

    asyncio.run(_wipe_and_reseed_other())
    assert asyncio.run(_org_names()) == {"someone else"}

    with _client() as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("kestrel-backup.zip", zip_bytes, "application/zip")},
        )
    assert r.status_code == 200
    assert "database restored: True" in r.text

    forget_engine()
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


def test_web_restore_endpoint_never_500s_on_a_bad_upload(tmp_path):
    with _client() as c:
        # Not a zip at all.
        r1 = c.post(
            "/settings/restore",
            files={"backup_file": ("not-a-zip.zip", b"just some bytes", "application/zip")},
        )
        assert r1.status_code == 200
        assert "warnbox" in r1.text

        # A well-formed zip that is not a Kestrel backup.
        bogus = tmp_path / "bogus.zip"
        with zipfile.ZipFile(bogus, "w") as z:
            z.writestr("readme.txt", "hello")
        r2 = c.post(
            "/settings/restore",
            files={"backup_file": ("bogus.zip", bogus.read_bytes(), "application/zip")},
        )
        assert r2.status_code == 200
        assert "does not look like a Kestrel backup" in r2.text

        # No file chosen at all.
        r3 = c.post("/settings/restore", data={})
        assert r3.status_code == 200
        assert "Choose a backup" in r3.text


def test_web_restore_escapes_the_archive_entry_name_in_its_error(tmp_path):
    """A rejected entry name is quoted into the error message, and the uploader controls it.
    Unescaped, a crafted archive executed markup in the app's own origin -- the one holding the
    LAN shared secret and every mutating endpoint."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("kestrel.db", b"x")
        # Rejected for the "..", so it reaches the message carrying live markup.
        z.writestr("config/<img src=x onerror=alert(1)>/../../evil.yml", "pwn: true")

    with _client() as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("evil.zip", evil.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200
    # The name still READS as the operator typed it (the text is preserved), but no live tag
    # survives: "<img" is neutralized, so the payload cannot execute in the app's origin.
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "<img src=x" not in r.text


def test_a_missing_backup_file_is_reported_without_its_path(tmp_path):
    """The message reaches a page (Settings > Restore), so it names no path (D-SEC-1)."""
    from kestrel.services.backup import InvalidBackupError, restore_backup

    missing = tmp_path / "secret-dir" / "gone.zip"
    with pytest.raises(InvalidBackupError) as err:
        restore_backup(missing)
    assert "secret-dir" not in str(err.value) and "gone.zip" not in str(err.value)
    assert str(err.value) == "The backup file was not found."


def test_an_unreadable_backup_entry_is_reported_without_the_error_text(tmp_path, monkeypatch):
    """An OSError while unpacking carries a staging path; the page gets the entry and the error
    type only (D-SEC-1)."""
    from kestrel.services import backup

    zpath = tmp_path / "b.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("kestrel.db", b"x")

    def refuse(fsrc, fdst):
        raise PermissionError(13, "Permission denied", r"C:\secret-dir\staging\kestrel.db")

    monkeypatch.setattr(backup.shutil, "copyfileobj", refuse)
    with pytest.raises(InvalidBackupError) as err:
        restore_backup(zpath)
    assert "secret-dir" not in str(err.value) and "Permission denied" not in str(err.value)
    assert str(err.value) == "Could not read kestrel.db from the backup (PermissionError)."
