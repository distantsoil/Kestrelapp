"""Restore while the app holds the database open (R1).

Every other restore test releases the database first (`reset_engine`, `forget_engine`, or a
`NullPool` engine), which is exactly what the running app never does: its worker, scheduler and
delivery loops capture `get_sessionmaker()` once at startup and keep pooled connections open for
the life of the process. Reproduced by hand on Windows on 2026-10-17: 13 of 13 restores through
the running app failed with WinError 32 or 33 (10 under load, 3 idle).

These tests keep a REAL pooled connection to the live database open across the restore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import get_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Org
from kestrel.services.backup import create_backup, restore_backup

pytestmark = pytest.mark.usefixtures("_isolate_data_home", "_real_pool")


@pytest.fixture
def _real_pool(monkeypatch):
    """Undo conftest's session-wide NullPool for these tests only. NullPool closes every connection
    when its session ends, so the database is never held open across a restore, which is exactly
    the condition the running app is always in. Each test disposes the engine on its own loop."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from kestrel.db import engine as engine_mod

    monkeypatch.setattr(engine_mod, "create_async_engine", create_async_engine)


async def _seed(names: list[str]) -> None:
    """A fresh migrated database holding exactly these orgs, written through the app's own
    engine, which is then left open: its pool keeps the file (and its -wal) open, as in the app."""
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    async with get_sessionmaker()() as s:
        for n in names:
            s.add(Org(name=n, normalized_name=n.lower()))
        await s.commit()


async def _org_names() -> list[str]:
    async with get_sessionmaker()() as s:
        return sorted((await s.execute(select(Org.name))).scalars().all())


async def test_restore_succeeds_while_a_pooled_connection_holds_the_database(tmp_path):
    await _seed(["Acme Robotics", "Beacon Health"])
    archive = await asyncio.to_thread(create_backup, tmp_path)
    await _seed(["Someone Else"])
    assert await _org_names() == ["Someone Else"]  # the pool now holds the live file open

    try:
        await asyncio.to_thread(restore_backup, archive)
        # The same engine the app's loops hold sees the restored rows: no restart, no new engine.
        assert await _org_names() == ["Acme Robotics", "Beacon Health"]
    finally:
        await reset_engine()


async def test_each_restore_keeps_its_own_pre_restore_copy(tmp_path):
    await _seed(["Acme Robotics"])
    archive = await asyncio.to_thread(create_backup, tmp_path)
    try:
        first = await asyncio.to_thread(restore_backup, archive)
        second = await asyncio.to_thread(restore_backup, archive)
        assert first.pre_restore_backup and second.pre_restore_backup
        assert first.pre_restore_backup != second.pre_restore_backup, (
            "a second restore overwrote the first one's safety copy"
        )
        for pre in (first.pre_restore_backup, second.pre_restore_backup):
            assert pre.exists()
            # One consistent file, not a main file plus a -wal/-shm that may not match it.
            assert not Path(f"{pre}-wal").exists() and not Path(f"{pre}-shm").exists()
    finally:
        await reset_engine()


def test_the_restore_route_works_with_the_app_running(tmp_path):
    """Through the real route, with the app's pool warm, and the worker left as it was."""
    from kestrel.web import app as app_mod
    from kestrel.web.app import build_app

    # Each asyncio.run disposes its own pool before its loop closes (aiosqlite connections must
    # be closed on the loop that opened them).
    async def seed_then_release(names: list[str]) -> None:
        await _seed(names)
        await reset_engine()

    async def names_then_release() -> list[str]:
        try:
            return await _org_names()
        finally:
            await reset_engine()

    asyncio.run(seed_then_release(["Acme Robotics", "Beacon Health"]))
    archive = create_backup(tmp_path)
    asyncio.run(seed_then_release(["Someone Else"]))

    held: dict = {}

    async def hold_a_reader() -> None:
        """What a background loop looks like mid-cycle: a checked-out connection inside an OPEN
        read transaction (an explicit BEGIN, then a read), so SQLite holds a WAL snapshot for it.
        Disposing the pool does not close a checked-out connection."""
        conn = await get_engine().connect()
        await conn.exec_driver_sql("BEGIN")
        await conn.exec_driver_sql("SELECT count(*) FROM org")
        held["conn"] = conn

    async def release_the_reader() -> None:
        await held["conn"].exec_driver_sql("COMMIT")
        await held["conn"].close()

    # No real background loops: they could open connections during the TestClient's shutdown that
    # are never disposed. The held reader above is the state they leave the database in.
    with TestClient(build_app(worker=False)) as c:
        try:
            assert c.get("/api/status").status_code == 200  # the app's pool is open on the file
            c.portal.call(hold_a_reader)
            r = c.post(
                "/settings/restore",
                files={"backup_file": ("backup.zip", archive.read_bytes(), "application/zip")},
            )
            assert r.status_code == 200
            assert "Restore failed" not in r.text, r.text
            assert "warnbox" not in r.text, r.text
            assert app_mod.is_paused() is False, "the restore left the worker paused"
        finally:
            if "conn" in held:
                c.portal.call(release_the_reader)
            c.portal.call(reset_engine)  # dispose on the app's own loop

    assert asyncio.run(names_then_release()) == ["Acme Robotics", "Beacon Health"]


# ---------------------------------------------------------------------------- review findings
def _with_revision(archive: Path, revision: str, out: Path) -> Path:
    """A copy of `archive` whose kestrel.db claims an alembic revision this build has never seen,
    which is what a backup made by a NEWER Kestrel looks like."""
    import sqlite3
    import zipfile

    work = out / "edit"
    work.mkdir()
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work)
    con = sqlite3.connect(work / "kestrel.db")
    try:
        con.execute("UPDATE alembic_version SET version_num = ?", (revision,))
        con.commit()
    finally:
        con.close()
    edited = out / "newer.zip"
    with zipfile.ZipFile(edited, "w") as zf:
        for f in sorted(work.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(work).as_posix())
    return edited


async def test_a_backup_from_a_newer_kestrel_is_refused_before_anything_changes(tmp_path):
    from kestrel.services.backup import InvalidBackupError

    await _seed(["Acme Robotics"])
    archive = await asyncio.to_thread(create_backup, tmp_path)
    newer = _with_revision(archive, "9999_from_the_future", tmp_path)
    await _seed(["Someone Else"])
    data_dir = paths.db_path().parent
    try:
        with pytest.raises(InvalidBackupError, match="newer version of Kestrel"):
            await asyncio.to_thread(restore_backup, newer)
        assert await _org_names() == ["Someone Else"], "the live database was changed"
        assert not list(data_dir.glob("kestrel.db.pre-restore*")), "refusal still wrote a snapshot"
    finally:
        await reset_engine()


async def test_only_the_newest_pre_restore_snapshots_are_kept(tmp_path):
    from kestrel.services.backup import PRE_RESTORE_KEEP

    await _seed(["Acme Robotics"])
    archive = await asyncio.to_thread(create_backup, tmp_path)
    try:
        results = [await asyncio.to_thread(restore_backup, archive) for _ in range(5)]
        left = sorted(paths.db_path().parent.glob("kestrel.db.pre-restore-*"))
        assert len(left) == PRE_RESTORE_KEEP
        assert results[-1].pre_restore_backup in left, "the newest snapshot was pruned"
    finally:
        await reset_engine()


def test_clear_everything_removes_pre_restore_snapshots():
    from kestrel.services.reset import _clear_files

    paths.ensure_dirs()
    snap = paths.db_path().with_name("kestrel.db.pre-restore-20261017T000000000000Z")
    legacy = paths.db_path().with_name("kestrel.db.pre-restore")
    snap.write_bytes(b"x")
    legacy.write_bytes(b"x")
    _clear_files(keep_keys=True)
    assert not snap.exists() and not legacy.exists(), (
        "a reset that promises to delete everything left full copies of the database behind"
    )


async def test_worker_holds_nest_and_survive_a_tray_resume():
    from kestrel.web import app as app_mod

    app_mod.set_paused(False)
    async with app_mod.worker_held():
        async with app_mod.worker_held():
            pass
        assert app_mod.is_paused(), "an inner hold ending released the outer one"
        app_mod.set_paused(False)  # the tray's Resume, mid-restore
        assert app_mod.is_paused(), "a tray Resume released a restore's hold"
    assert not app_mod.is_paused()


def test_a_second_restore_while_one_runs_is_refused(tmp_path):
    from kestrel.web import app as app_mod
    from kestrel.web.app import build_app

    asyncio.run(_seed(["Acme Robotics"]))
    archive = create_backup(tmp_path)
    asyncio.run(reset_engine())

    with TestClient(build_app(worker=False)) as c:
        try:
            c.portal.call(app_mod.maintenance_lock().acquire)
            try:
                r = c.post(
                    "/settings/restore",
                    files={"backup_file": ("b.zip", archive.read_bytes(), "application/zip")},
                )
            finally:
                c.portal.call(_release_maintenance)
            assert "already" in r.text.lower() and "nothing was restored" in r.text.lower()
        finally:
            c.portal.call(reset_engine)


async def _release_maintenance() -> None:
    from kestrel.web import app as app_mod

    app_mod.maintenance_lock().release()


def test_cli_restore_refuses_while_the_app_is_running(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kestrel import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: True)
    result = CliRunner().invoke(cli_mod.app, ["restore", str(tmp_path / "any.zip")])
    assert result.exit_code == 2 and "Kestrel is running" in result.output


def test_a_zip_that_lies_about_its_size_is_refused_not_unpacked(tmp_path):
    """A review worried that the size caps trust the archive's own headers, so a member
    declaring 1 KB could inflate to gigabytes. CPython's ZipExtFile stops producing output at the
    declared size and then fails the CRC, which extraction turns into InvalidBackupError. This
    pins that: the lie is refused, and nothing near the real size is written."""
    import io
    import struct
    import zipfile

    from kestrel.services.backup import InvalidBackupError

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("kestrel.db", b"\0" * 5_000_000)
    raw = bytearray(buf.getvalue())
    struct.pack_into("<I", raw, raw.find(b"PK\x03\x04") + 22, 1000)  # local header: 1,000 bytes
    struct.pack_into("<I", raw, raw.find(b"PK\x01\x02") + 24, 1000)  # central directory: same
    liar = tmp_path / "liar.zip"
    liar.write_bytes(bytes(raw))

    with pytest.raises(InvalidBackupError, match="Could not read"):
        restore_backup(liar)
