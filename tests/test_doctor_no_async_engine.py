"""D-DB-2: the doctor and the setup writer run on a thread that owns no app loop. An async engine
under asyncio.run there leaves aiosqlite's worker thread calling into a closed loop when the
connect fails, an unhandled thread exception that failed an unrelated route test now and then.
So none of them may open an aiosqlite connection at all. All data is invented."""

from __future__ import annotations

import threading

import aiosqlite
import pytest

from kestrel import doctor, paths
from kestrel.db import migrate
from kestrel.services import setup_state as ss

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def no_aiosqlite(monkeypatch):
    """Record every aiosqlite connection attempt; none may happen."""
    attempts: list[str] = []
    real = aiosqlite.connect

    def spy(*a, **kw):
        attempts.append(str(a[:1]))
        return real(*a, **kw)

    monkeypatch.setattr(aiosqlite, "connect", spy)
    return attempts


@pytest.fixture
def thread_errors():
    seen: list[str] = []
    old = threading.excepthook
    threading.excepthook = lambda a: seen.append(repr(a.exc_value))
    yield seen
    threading.excepthook = old


def test_doctor_database_reads_open_no_async_connection(no_aiosqlite, thread_errors):
    paths.ensure_dirs()
    migrate.run_migrations()
    rep = doctor.DoctorReport()
    doctor._check_ats_shapes(rep, doctor.get_settings())
    assert doctor._run_is_active() is False
    assert no_aiosqlite == []
    assert thread_errors == []


def test_doctor_database_reads_on_threads_raise_nothing(no_aiosqlite, thread_errors):
    """Thirty runs on short-lived threads, as the Setup page's action thread does."""
    paths.ensure_dirs()
    migrate.run_migrations()
    for _ in range(30):
        rep = doctor.DoctorReport()
        t = threading.Thread(target=doctor._check_ats_shapes, args=(rep, doctor.get_settings()))
        t.start()
        t.join()
    assert no_aiosqlite == []
    assert thread_errors == []


def test_verify_thread_runs_the_whole_doctor_without_an_async_connection(
    no_aiosqlite, thread_errors, monkeypatch
):
    """The route's own path: ss.verify() starts the thread, which runs run_doctor."""
    paths.ensure_dirs()
    migrate.run_migrations()
    monkeypatch.setattr(doctor, "_check_browser", lambda rep: None)
    act = ss.verify()
    act.join(120)
    assert act.status in ("done", "failed")
    assert no_aiosqlite == []
    assert thread_errors == []


def test_setup_write_rows_uses_a_sync_engine(no_aiosqlite):
    from kestrel import setup as setup_mod

    paths.ensure_dirs()
    migrate.run_migrations()
    setup_mod._write_rows({"hosted.test_row": {"a": 1}})
    setup_mod._write_rows({"hosted.test_row": {"a": 2}})
    assert no_aiosqlite == []
