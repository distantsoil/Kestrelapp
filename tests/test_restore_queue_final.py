"""Restore, migration 0034 and the work loops: the final review's restore and queue findings.

INVARIANTS: a backup that cannot be brought up to date is refused with the fixed sentence and
nothing changed, whatever the migration raised; an upload that is refused stops nothing; a restore
or a reset hands back only the leases of the loops it stopped, so a model build in flight in the
setup loop is never run twice; no setting row that stands for queued work comes from a backup; a
worker cancelled mid-cycle gives up the settings its run was confirmed on; a restore that wrote
the database says so even when a later part failed. All text is invented; nothing touches the
network."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import sqlite3
import zipfile

import httpx
import pytest

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import get_sessionmaker, reset_engine
from kestrel.match_text import requirement_hash
from kestrel.services import backup as bk
from kestrel.services.backup import restore_backup
from kestrel.web import app as app_mod
from tests.test_restore_import import NOW, _exec, _rows, _schema, _this_computer

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_OLD = re.compile(r"[^a-z0-9]+")


def _old_hash(text: str) -> str:
    form = _OLD.sub(" ", text.lower()).strip()
    return hashlib.sha1(form.encode("utf-8"), usedforsecurity=False).hexdigest()


def _alembic(db, rev: str, *, down: bool = False) -> None:
    from alembic import command

    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        (command.downgrade if down else command.upgrade)(cfg, rev)


def _correction(con, rid, text, h, *, scope="person", opp=None, track="employment"):
    con.execute(
        "INSERT INTO requirement_correction (id, track, opportunity_id, requirement_text, "
        "requirement_hash, kestrel_state, operator_state, reason, scope, basis, agrees_now, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'gap', 'met', 'all', ?, 'none', 0, ?, ?)",
        (rid, track, opp, text, h, scope, NOW, NOW),
    )


def _older_backup(tmp_path, fill):
    older = tmp_path / "older"
    older.mkdir()
    db = older / "kestrel.db"
    _alembic(db, "0033")
    con = sqlite3.connect(str(db))
    fill(con)
    con.commit()
    con.close()
    archive = tmp_path / "older.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(db, "kestrel.db")
    return archive


# ------------------------------------------------------------------- 1. the work copy boundary
def test_a_blob_requirement_text_in_an_older_backup_is_restored_as_text(tmp_path):
    """Migration 0034 reads every stored requirement as text: a BLOB value (never written by
    Kestrel) is decoded, stored back as text and hashed in today's form, not a TypeError."""
    archive = _older_backup(
        tmp_path, lambda con: _correction(con, 1, "Invented skill é".encode() + b"\xff", "x")
    )
    _this_computer()

    result = restore_backup(archive)

    assert result.db_restored
    rows = _exec(
        "SELECT requirement_text, typeof(requirement_text), requirement_hash "
        "FROM requirement_correction"
    )
    assert len(rows) == 1
    text, kind, h = rows[0]
    assert kind == "text" and text.startswith("Invented skill é")
    assert h == requirement_hash(text)


@pytest.mark.parametrize(
    "exc", [TypeError("invented inner detail"), RuntimeError("invented inner detail")]
)
def test_any_failure_migrating_the_work_copy_is_the_fixed_refusal(tmp_path, monkeypatch, exc):
    archive = _older_backup(tmp_path, lambda con: None)
    _this_computer()
    before = _schema(paths.db_path()), _rows()
    real = migrate.run_migrations

    def migrations(db=None, *args, **kwargs):
        if db is not None and str(db).replace("\\", "/").endswith("/work/kestrel.db"):
            raise exc
        return real(db, *args, **kwargs)

    monkeypatch.setattr(migrate, "run_migrations", migrations)
    with pytest.raises(bk.InvalidBackupError) as caught:
        restore_backup(archive)

    assert str(caught.value) == bk.DB_NOT_UPGRADED
    assert (_schema(paths.db_path()), _rows()) == before
    assert not list(paths.db_path().parent.glob("kestrel.db.restore-*"))


async def test_the_restore_route_answers_the_fixed_refusal_never_a_500(tmp_path, monkeypatch):
    from kestrel.web.app import build_app

    archive = _older_backup(tmp_path, lambda con: None)
    await asyncio.to_thread(_this_computer)
    real = migrate.run_migrations

    def migrations(db=None, *args, **kwargs):
        if db is not None and str(db).replace("\\", "/").endswith("/work/kestrel.db"):
            raise TypeError("invented inner detail")
        return real(db, *args, **kwargs)

    monkeypatch.setattr(migrate, "run_migrations", migrations)
    transport = httpx.ASGITransport(app=build_app(worker=False), client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post(
            "/settings/restore",
            files={"backup_file": ("b.zip", archive.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200, r.text
    assert "brought up to this version of Kestrel" in r.text
    assert "invented inner detail" not in r.text and "TypeError" not in r.text
    await reset_engine()


# ------------------------------------------------------------- 2. migration 0034 round trip
def test_0034_downgrade_keeps_every_row_when_the_old_form_collides(tmp_path):
    """C++, C# and C share one old form. Downgrade gives the old form to one row per collision
    (the one whose text already has it, else the oldest) and keeps the others in today's form,
    so the unique indexes hold and nothing is merged or deleted; upgrade re-derives all."""
    db = tmp_path / "k.db"
    _alembic(db, "0033")
    con = sqlite3.connect(str(db))
    _correction(con, 1, "Proficiency in C++", _old_hash("Proficiency in C++"))
    _correction(con, 2, "Fluent Invented Script жизнь", _old_hash("Fluent Invented Script жизнь"))
    con.commit()
    con.close()
    _alembic(db, "head")
    con = sqlite3.connect(str(db))
    _correction(con, 3, "Proficiency in C#", requirement_hash("Proficiency in C#"))
    _correction(con, 4, "Proficiency in C", requirement_hash("Proficiency in C"))
    # Two without a plain-form twin, on one posting: the oldest keeps the old form.
    _correction(
        con, 6, "Invented tool#", requirement_hash("Invented tool#"), scope="posting", opp=41
    )
    _correction(
        con, 5, "Invented tool++", requirement_hash("Invented tool++"), scope="posting", opp=41
    )
    con.commit()
    con.close()

    _alembic(db, "0033", down=True)

    con = sqlite3.connect(str(db))
    got = dict(con.execute("SELECT id, requirement_hash FROM requirement_correction"))
    con.close()
    assert len(got) == 6
    assert got[4] == _old_hash("Proficiency in C")  # its text already has the old form
    assert got[1] == requirement_hash("Proficiency in C++")  # kept in today's form
    assert got[3] == requirement_hash("Proficiency in C#")
    assert got[2] == _old_hash("Fluent Invented Script жизнь")
    assert got[5] == _old_hash("Invented tool++")
    assert got[6] == requirement_hash("Invented tool#")

    _alembic(db, "head")
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT requirement_text, requirement_hash FROM requirement_correction")
    assert all(h == requirement_hash(t) for t, h in rows)
    con.close()


def test_0034_downgrade_picks_the_oldest_row_when_no_text_has_the_old_form(tmp_path):
    db = tmp_path / "k.db"
    _alembic(db, "head")
    con = sqlite3.connect(str(db))
    _correction(con, 7, "Invented lang#", requirement_hash("Invented lang#"))
    _correction(con, 3, "Invented lang++", requirement_hash("Invented lang++"))
    _correction(con, 9, "Invented lang++", requirement_hash("Invented lang++"), track="consulting")
    con.commit()
    con.close()

    _alembic(db, "0033", down=True)

    con = sqlite3.connect(str(db))
    got = dict(con.execute("SELECT id, requirement_hash FROM requirement_correction"))
    con.close()
    old = _old_hash("Invented lang++")
    assert got == {3: old, 7: requirement_hash("Invented lang#"), 9: old}


# ------------------------------------------------------ 3. only the stopped loops' leases
@pytest.fixture
def loops(monkeypatch):
    state = {"events": []}

    async def worker(stop):
        await stop.wait()
        state["events"].append("worker stopped")

    async def lane(stop):
        await stop.wait()
        state["events"].append("lane stopped")

    monkeypatch.setattr(app_mod, "_worker_loop", worker)
    monkeypatch.setattr(app_mod, "_online_lane", lane)
    monkeypatch.setattr(app_mod, "_quit_grace_s", lambda: 0.05)
    monkeypatch.setattr(app_mod, "CANCEL_WAIT_S", 0.1)
    app_stop = asyncio.Event()
    state["app_stop"] = app_stop
    yield state
    app_stop.set()
    own, w, la = app_mod._current_work_loops()
    if own is not None:
        own.set()
    for t in (w, la):
        if t is not None and not t.done():
            t.cancel()
    app_mod._work_loops.update(stop=None, worker=None, lane=None, app_stop=None)


async def _fresh() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    await asyncio.to_thread(migrate.run_migrations)
    await asyncio.to_thread(paths.seed_config)


async def test_a_model_build_in_flight_is_not_run_twice_across_a_maintenance_stop(loops):
    from kestrel.services import model_tasks as mt

    await _fresh()
    mt.reset()
    sm = get_sessionmaker()
    from kestrel.queue.queue import TaskQueue

    await TaskQueue(sm).enqueue(
        mt.CREATE_TYPE,
        {
            "name": "kestrel-invented",
            "tier": "tier2",
            "base": "invented:1b",
            "modelfile": "FROM invented:1b",
        },
    )
    posts: list[str] = []
    started, finish = asyncio.Event(), asyncio.Event()

    async def body():
        started.set()
        await finish.wait()
        yield b'{"status": "success"}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, content=body())

    app_mod._spawn_work_loops(loops["app_stop"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        build = asyncio.create_task(mt.drain_once(sm, "http://127.0.0.1:11434", http=http))
        await asyncio.wait_for(started.wait(), 5)
        async with app_mod.work_loops_stopped() as stopped:
            assert stopped is True
        finish.set()
        assert await build is True
        assert await mt.drain_once(sm, "http://127.0.0.1:11434", http=http) is False
    assert posts == ["/api/create"]
    con = sqlite3.connect(str(paths.db_path()))
    assert con.execute("SELECT status FROM task WHERE type = ?", (mt.CREATE_TYPE,)).fetchall() == [
        ("done",)
    ]
    con.close()
    mt.reset()
    await reset_engine()


async def test_quit_still_hands_back_every_lease_this_process_holds(loops):
    from kestrel.queue.queue import TaskQueue
    from kestrel.services import model_tasks as mt

    await _fresh()
    q = TaskQueue(get_sessionmaker())
    await q.enqueue(mt.PULL_TYPE, {"tag": "invented:1b"})
    task = await q.lease(mt.WORKER_ID, types=[mt.PULL_TYPE])
    assert task is not None
    await app_mod._graceful_worker_shutdown(None, asyncio.Event(), 0.01)
    assert (await q.get(task.id)).status == "pending"
    await reset_engine()


async def test_an_invalid_upload_stops_no_loop(loops, tmp_path):
    from kestrel.web.app import build_app

    await _fresh()
    app_mod._spawn_work_loops(loops["app_stop"])
    await asyncio.sleep(0.01)
    _own, worker, lane = app_mod._current_work_loops()
    transport = httpx.ASGITransport(app=build_app(worker=False), client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post(
            "/settings/restore",
            files={"backup_file": ("b.zip", b"not a zip at all", "application/zip")},
        )
    assert r.status_code == 200, r.text
    assert bk.NOT_A_ZIP in r.text
    _own2, worker2, lane2 = app_mod._current_work_loops()
    assert worker2 is worker and lane2 is lane
    assert not worker.done() and not lane.done()
    assert loops["events"] == []
    await reset_engine()


# --------------------------------------------------------- 4. queue-state settings rows
def test_no_queue_state_setting_row_comes_from_a_backup(tmp_path):
    from kestrel.services.backup import create_backup

    _this_computer()
    for key, value in (
        ("pipeline.orphan_requeue", {"7:t0_pass": NOW}),
        ("worker.stop_request", {"run_id": 3}),
        ("scheduler.scan_hold_until", NOW),
        ("org_summary_hold.12", {"at": NOW}),
        ("org_summary_attempt.12", {"at": NOW}),
        ("ui.scheme", "dark"),
    ):
        _exec(
            "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), NOW),
        )
    archive = create_backup(tmp_path)
    _this_computer()

    restore_backup(archive)

    rows = _rows()
    for gone in (
        "pipeline.orphan_requeue",
        "worker.stop_request",
        "scheduler.scan_hold_until",
        "org_summary_hold.12",
    ):
        assert gone not in rows, gone
    assert rows["org_summary_attempt.12"] == {"at": NOW}  # history, not queued work
    assert rows["ui.scheme"] == "dark"


# ------------------------------------------------------ 5. a cancelled cycle lets go
async def test_a_cancelled_worker_releases_the_run_it_held(monkeypatch):
    held = asyncio.Event()

    async def cycles(stop):
        app_mod._hold_run({"invented": "settings"}, {"approval": "invented"})
        held.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_mod, "_worker_loop", cycles)
    app_mod._release_run()
    task = asyncio.create_task(app_mod._worker_task(asyncio.Event()))
    await asyncio.wait_for(held.wait(), 5)
    assert app_mod.confirmed_run_settings() == {"invented": "settings"}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert app_mod.confirmed_run_settings() is None
    assert app_mod._run_confirmed["online_approval"] is None


# ------------------------------------------------- 6. a failure after the database write
def test_a_config_copy_failure_after_the_write_reports_what_was_restored(tmp_path, monkeypatch):
    from kestrel.services.backup import create_backup

    _this_computer()
    _exec(
        "INSERT INTO org (name, normalized_name, first_seen_at, last_seen_at) VALUES "
        "('Invented Backup Org', 'invented backup org', ?, ?)",
        (NOW, NOW),
    )
    archive = create_backup(tmp_path)
    _this_computer()

    def broken(*_a, **_k):
        raise OSError("invented disk detail")

    monkeypatch.setattr(bk, "_restore_settings_yml", broken)
    result = restore_backup(archive)

    assert result.db_restored
    assert _exec("SELECT name FROM org") == [("Invented Backup Org",)]
    assert result.parts_failed == ["config files"]
    text = " ".join(result.summary_lines())
    assert "Database restored: yes." in text
    assert "The database was restored, but the config files could not be restored" in text
    assert "invented disk detail" not in text


async def test_the_route_resets_caches_and_says_the_database_was_restored(tmp_path, monkeypatch):
    from kestrel.services import reset as reset_mod
    from kestrel.services.backup import create_backup
    from kestrel.web.app import build_app

    await asyncio.to_thread(_this_computer)
    archive = await asyncio.to_thread(create_backup, tmp_path)
    await asyncio.to_thread(_this_computer)

    def broken(*_a, **_k):
        raise OSError("invented disk detail")

    forgot = []
    monkeypatch.setattr(bk, "_restore_settings_yml", broken)
    monkeypatch.setattr(reset_mod, "_forget_caches", lambda: forgot.append(True))
    transport = httpx.ASGITransport(app=build_app(worker=False), client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post(
            "/settings/restore",
            data={"from_settings": "1"},
            files={"backup_file": ("b.zip", archive.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200, r.text
    assert "Restored the database from b.zip" in r.text
    assert "the config files could not be restored" in r.text
    assert r.text.count("could not be restored") == 1  # said once, not twice
    assert "Restore failed" not in r.text and "invented disk detail" not in r.text
    assert forgot == [True]
    assert json.loads(r.headers["HX-Trigger"])["k-toast"]["tone"] == "warn"
    await reset_engine()


def test_the_cli_says_a_partial_restore_is_partial(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kestrel import cli as cli_mod
    from kestrel.services.backup import create_backup

    _this_computer()
    archive = create_backup(tmp_path)
    _this_computer()

    def broken(*_a, **_k):
        raise OSError("invented disk detail")

    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: False)
    monkeypatch.setattr(bk, "_restore_settings_yml", broken)
    out = CliRunner().invoke(cli_mod.app, ["restore", str(archive)])
    assert out.exit_code == 1, out.output
    assert "Database restored: yes." in out.output
    assert "the config files could not be restored" in out.output
    assert "Restore partly complete." in out.output
    assert "Restore complete." not in out.output
