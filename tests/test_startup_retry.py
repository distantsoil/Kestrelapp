"""#148 startup retry: the window opens before the background loops; a slow start is waited
for, a dead one is diagnosed with a thread dump and task counts; "Try again" never starts a
second server beside a running one; a Cancel abandons the attempt without spending it.
Real uvicorn servers where the issue requires them; no window, no network, no real model."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from kestrel import startup_health
from tests.test_mcp_switch import _not_live  # noqa: F401 - autouse fixture, reused


@pytest.fixture(autouse=True)
def _clean_startup_state(monkeypatch):
    from kestrel import paths

    paths.startup_health_json().unlink(missing_ok=True)
    monkeypatch.delenv(startup_health.SAFE_MODE_ENV, raising=False)
    monkeypatch.setattr(startup_health, "_offered", False)
    yield
    paths.startup_health_json().unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean_shell(monkeypatch):
    from kestrel.desktop import shell as sh

    for attr in ("_quitting", "_started", "_boot_failed", "_cancelling"):
        monkeypatch.setattr(sh, attr, False)
    for attr in ("server", "server_thread", "window", "icon"):
        monkeypatch.setattr(sh, attr, None)
    yield
    sh._stop_server()  # no test may leave a live uvicorn thread behind


@pytest.fixture(autouse=True)
def _off_the_ladder(monkeypatch):
    """Real servers here bind an ephemeral loopback port, never a rung of the 8765 ladder.

    A rung is shared with other test workers and with a real Kestrel the operator may be
    running. On Windows a loopback bind succeeds beside a Kestrel bound to 0.0.0.0 on the same
    rung, and these tests' servers stay bound but not listening while their lifespan is held
    back, so a health probe there would be answered by the other Kestrel instead."""
    from kestrel.web import serve

    real_bind = serve.bind_free_port

    def _ephemeral(*, ladder=True, host=None):
        return real_bind(ladder=False, host="127.0.0.1")

    monkeypatch.setattr(serve, "bind_free_port", _ephemeral)


@pytest.fixture
def reserved_port():
    """A port nothing answers on and nothing else can take for the test's duration: bound to
    loopback (ephemeral, off the ladder) and held open, never listening."""
    from kestrel.web import serve

    sock, port = serve.bind_free_port(ladder=False, host="127.0.0.1")
    assert port not in serve.PORT_LADDER
    try:
        yield port
    finally:
        sock.close()


class _Rec:
    """A splash recorder standing in for the loading page."""

    def __init__(self) -> None:
        self.said: list[tuple[str, object]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.readied = False
        self.cancels: list[bool] = []

    def say(self, text, pct=None, later="", after_ms=0):
        self.said.append((text, pct))

    def fail(self, reason, details, code=""):
        self.failed.append((reason, details, code))

    def ready(self):
        self.readied = True

    def cancel(self, show):
        self.cancels.append(show)


def _recorders(monkeypatch):
    """The worker app's lifespan with every loop and side effect replaced by a recorder."""
    from kestrel.mcp import runtime
    from kestrel.web import app as app_mod

    ran: list[str] = []

    def recorder(name: str):
        async def idle(stop, *a, **k):
            ran.append(name)
            await stop.wait()

        return idle

    for name, label in (("_worker_loop", "worker"), ("_online_lane", "online_lane"),
                        ("_stall_monitor", "stall"), ("_delivery_loop", "delivery"),
                        ("_setup_models_loop", "models"), ("_update_loop", "updates"),
                        ("_scheduler_loop", "scheduler")):
        monkeypatch.setattr(app_mod, name, recorder(label))

    async def noop(*a, **k):
        return None

    for name in ("_seed_shipped_sources", "_rebuild_generated_sources",
                 "_seed_shipped_layouts", "_mark_update_seen"):
        monkeypatch.setattr(app_mod, name, noop)
    monkeypatch.setattr(app_mod, "_apply_keep_awake", noop)
    monkeypatch.setattr(app_mod, "_graceful_worker_shutdown", noop)

    async def mcp_start(port, *, bind_all=False):
        ran.append("mcp")

    monkeypatch.setattr(runtime, "start", mcp_start)
    return ran


# ------------------------------------------------------------------ acceptance 5: window first
def test_the_window_opens_before_the_background_loops(monkeypatch):
    """Health must answer before any background loop exists, even though the loops start."""
    from kestrel.db import migrate
    from kestrel.web import app as app_mod

    migrate.run_migrations()
    ran = _recorders(monkeypatch)
    monkeypatch.setattr(app_mod, "WORKER_DELAY_S", 1.0)
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=True, scheduler=True)) as c:
        assert c.get("/health").status_code == 200
        assert "worker" not in ran  # the health answer beat the first loop
        deadline = time.monotonic() + 5
        while "worker" not in ran and time.monotonic() < deadline:
            time.sleep(0.01)
    assert {"worker", "online_lane", "stall", "delivery", "models", "updates"} <= set(ran)


# ------------------------------------------------------------------ acceptance 5, no loss
def test_a_shutdown_inside_the_delay_spawns_nothing_and_loses_nothing(monkeypatch):
    """Closing the window while the loops are still in their delay cancels the spawn:
    nothing was made, so nothing is lost (hard rule 5)."""
    from sqlalchemy import text

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_sync_engine
    from kestrel.web import app as app_mod

    ran = _recorders(monkeypatch)
    monkeypatch.setattr(app_mod, "WORKER_DELAY_S", 10.0)  # longer than the test lasts
    from kestrel.web.app import build_app

    migrate.run_migrations()
    from sqlalchemy.orm import Session

    from kestrel.db.models import Task

    engine = make_sync_engine(paths.db_path())
    with Session(engine) as s:
        s.add(Task(type="role", status="pending", payload={}))
        s.commit()
    engine.dispose()

    with TestClient(build_app(worker=True, scheduler=True)) as c:
        assert c.get("/health").status_code == 200
    # The lifespan exited while the delay was still running.
    assert ran == []  # no loop was ever made, and none is left running
    engine = make_sync_engine(paths.db_path())
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status FROM task")).fetchone()
        count = conn.execute(text("SELECT COUNT(*) FROM task")).fetchone()
    engine.dispose()
    assert row == ("pending",) and count == (1,)  # the task survived the cancellation


# ------------------------------------------------------------------ acceptance 1: the log
def test_the_startup_log_shows_each_step_and_its_time(monkeypatch):
    """The operator's log says what finished and how long it took, in order."""
    import structlog.testing

    from kestrel.db import migrate
    from kestrel.web import app as app_mod

    migrate.run_migrations()
    _recorders(monkeypatch)
    monkeypatch.setattr(app_mod, "WORKER_DELAY_S", 1.0)
    from kestrel.web.app import build_app

    with structlog.testing.capture_logs() as logs:
        with TestClient(build_app(worker=True, scheduler=True)) as c:
            assert c.get("/health").status_code == 200

    steps = [e for e in logs if e.get("event") == "startup_step"]
    assert [e["step"] for e in steps] == ["data_ready", "ready"]
    for e in steps:
        assert isinstance(e["elapsed_ms"], int) and e["elapsed_ms"] >= 0


# ------------------------------------------------------------------ acceptance 3: slow start
def test_a_slow_start_that_finishes_inside_the_extended_wait_opens_with_no_error(monkeypatch):
    """The window opens and the app runs; the log says the start was slow, not that it failed."""
    import asyncio as aio

    from kestrel.desktop import shell as sh
    from kestrel.web import app as app_mod
    from kestrel.web import serve

    real_build = app_mod.build_app
    monkeypatch.setattr(app_mod, "build_app", lambda **k: real_build(worker=False, scheduler=False))

    async def slow_tracks():
        await aio.sleep(2.5)

    monkeypatch.setattr(app_mod, "_load_enabled_tracks", slow_tracks)
    monkeypatch.setattr(sh, "HEALTH_WAIT_S", 0.4)
    monkeypatch.setattr(sh, "HEALTH_EXTENDED_S", 5.0)
    monkeypatch.setattr(sh, "run_preflight", lambda: None)

    rec = _Rec()
    port = sh._start_backend(rec)
    assert port == sh._port
    assert port not in serve.PORT_LADDER  # its own port: no other Kestrel can answer the probe
    assert sh.server_thread.is_alive()
    try:
        assert serve.wait_for_health(port, 5.0) is True
        assert any("Still starting" in t for t, _ in rec.said)
        assert rec.cancels == [True, False]  # Cancel on while waiting, off on success
        assert any(t == "Checking your setup" for t, _ in rec.said)  # the success path ran
    finally:
        sh._stop_server()


# ------------------------------------------------------------------ acceptance 2: the dump
def test_a_forced_health_timeout_writes_a_thread_dump_and_task_counts(monkeypatch):
    """A start that never answers fails with a dump the operator can send back: thread
    stacks plus the task queue by type and status."""
    import asyncio as aio

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import make_sync_engine
    from kestrel.desktop import shell as sh
    from kestrel.web import app as app_mod
    from kestrel.web import serve

    real_build = app_mod.build_app
    monkeypatch.setattr(app_mod, "build_app", lambda **k: real_build(worker=False, scheduler=False))

    async def stuck_tracks():
        await aio.sleep(4)  # still working well past the wait

    monkeypatch.setattr(app_mod, "_load_enabled_tracks", stuck_tracks)
    monkeypatch.setattr(sh, "HEALTH_WAIT_S", 0.3)
    monkeypatch.setattr(sh, "HEALTH_EXTENDED_S", 0.3)

    migrate.run_migrations()
    from sqlalchemy.orm import Session

    from kestrel.db.models import Task

    engine = make_sync_engine(paths.db_path())
    with Session(engine) as s:
        s.add(Task(type="role", status="pending", payload={}, last_error="database is locked"))
        s.add(Task(type="letter", status="pending", payload={}))
        s.commit()
    engine.dispose()

    rec = _Rec()
    monkeypatch.setattr(sh, "_Splash", lambda win: rec)
    sh._boot_steps(busy=False)
    assert sh._port not in serve.PORT_LADDER  # its own port: no other Kestrel can answer
    assert rec.failed, "the problem card must be shown"
    reason, details, code = rec.failed[0]
    assert code == "health_never_up"
    assert "did not answer in time" in reason
    assert "thread dump was saved to" in details  # the card names the dump file
    assert sh._boot_failed is True and sh._started is False

    dumps = sorted(paths.logs_dir().glob("startup-stuck-*.txt"))
    assert dumps, "the problem card must point at a dump file that exists"
    body = dumps[-1].read_text(encoding="utf-8")
    assert "Kestrel" in body and "threads:" in body
    assert "role\tpending\t1" in body
    assert "letter\tpending\t1" in body
    assert "database is locked" in body


# ------------------------------------------------------------------ the invariant: one server
def test_try_again_refuses_to_start_beside_a_live_server(monkeypatch, reserved_port):
    """If a server is already running and answering, a second start is refused; the live
    one keeps running and the second attempt is counted as a failed start."""
    from kestrel.db import migrate
    from kestrel.desktop import shell as sh
    from kestrel.web import app as app_mod
    from kestrel.web import serve

    real_build = app_mod.build_app
    monkeypatch.setattr(app_mod, "build_app", lambda **k: real_build(worker=False, scheduler=False))
    monkeypatch.setattr(migrate, "run_migrations", lambda *a, **k: None)

    live_port = sh.start_server()  # a running server (a prior instance) answers fine
    live_server, live_thread = sh.server, sh.server_thread
    try:
        # start_server returns once the thread starts, before uvicorn listens: the premise
        # (a prior instance that answers) holds only once its health answers, as the shell waits.
        assert serve.wait_for_health(live_port, 10.0) is True
        assert httpx.get(f"http://127.0.0.1:{live_port}/health", timeout=5).status_code == 200
        # The new start: stuck, its port answered by nothing.
        monkeypatch.setattr(sh, "start_server", lambda: reserved_port)
        monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: False)
        monkeypatch.setattr(sh, "HEALTH_WAIT_S", 0.2)
        monkeypatch.setattr(sh, "HEALTH_EXTENDED_S", 0.3)

        rec = _Rec()
        with pytest.raises(sh.StartupError) as err:
            sh._start_backend(rec)
        assert err.value.code == "health_never_up"
        assert sh.server is live_server and sh.server_thread is live_thread  # nothing new started
        assert httpx.get(f"http://127.0.0.1:{live_port}/health",
                         timeout=5).status_code == 200  # the live one is untouched
        assert startup_health.failed_starts() == 1
    finally:
        # The globals still name the live server (the fake attempt touched none): stop exactly it.
        sh._stop_server()


def test_try_again_after_a_timeout_stops_the_first_server_and_leaves_one(monkeypatch):
    """The normal path: attempt one never answers, is stopped; attempt two answers and is
    the only server left. Exactly one, not two."""
    import threading

    from kestrel.db import migrate
    from kestrel.desktop import shell as sh
    from kestrel.web import app as app_mod
    from kestrel.web import serve

    real_build = app_mod.build_app
    monkeypatch.setattr(app_mod, "build_app", lambda **k: real_build(worker=False, scheduler=False))
    monkeypatch.setattr(migrate, "run_migrations", lambda *a, **k: None)

    state = {"healthy": False}
    monkeypatch.setattr(serve, "wait_for_health",
                        lambda port, timeout=30.0: state["healthy"])
    monkeypatch.setattr(sh, "HEALTH_WAIT_S", 0.2)
    monkeypatch.setattr(sh, "HEALTH_EXTENDED_S", 0.3)

    with pytest.raises(sh.StartupError) as err:
        sh._start_backend(_Rec())
    assert err.value.code == "health_never_up"
    first = sh.server_thread
    assert first is not None and first.is_alive()  # attempt one's server still up
    sh._stop_server()  # the shell stops it on the failure path
    first.join(timeout=10)
    assert not first.is_alive()

    state["healthy"] = True
    rec = _Rec()
    port = sh._start_backend(rec)
    try:
        live = [th for th in threading.enumerate() if th.name == "uvicorn"]
        assert live == [sh.server_thread]  # exactly one server: the new one
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200
    finally:
        sh._stop_server()


# ------------------------------------------------------------------ cancel, not quit
def test_cancelling_the_wait_abandons_the_attempt_and_keeps_the_card(monkeypatch, reserved_port):
    """Cancel during the health wait is not a failure and not a quit: the count is not
    spent, the window stays (the card offers Try again and Quit), and no server is left."""
    import json
    import threading

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.desktop import shell as sh

    paths.runtime_json().unlink(missing_ok=True)

    monkeypatch.setattr(migrate, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(sh, "start_server", lambda: reserved_port)  # never actually starts
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: False)
    monkeypatch.setattr(sh, "HEALTH_WAIT_S", 2.0)
    rec = _Rec()
    monkeypatch.setattr(sh, "_Splash", lambda win: rec)

    def _cancel_after_first_tick():
        time.sleep(0.5)
        sh._cancelling = True  # the operator pressed Cancel

    flipper = threading.Thread(target=_cancel_after_first_tick, daemon=True)
    flipper.start()
    sh._boot_steps(busy=False)

    assert rec.failed and "canceled" in rec.failed[0][0].lower()  # the card says so (US spelling)
    assert startup_health.failed_starts() == 0  # a Cancel spends no attempt
    assert sh._started is False and sh._quitting is False  # not started, and not a quit
    assert sh.server is None and sh.server_thread is None
    try:
        marker = json.loads(paths.runtime_json().read_text(encoding="utf-8"))
        assert marker.get("failed") is True  # a relaunch sees the problem card and defers
    finally:
        paths.runtime_json().unlink(missing_ok=True)


def test_waiting_for_health_survives_the_wall_clock_jumping_forward(monkeypatch):
    """The wait is measured on the monotonic clock: a wall clock that jumps an hour ahead (a
    sync, a sleep-wake, or the stress run's fast clock) must not end it before its timeout."""
    from types import SimpleNamespace

    from kestrel.web import serve

    calls = []
    jumped = SimpleNamespace(time=lambda: 1_000_000.0 + 3600.0 * len(calls),
                             monotonic=time.monotonic, sleep=lambda _s: None)

    def answer(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ConnectError("not listening yet")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(serve, "time", jumped)
    monkeypatch.setattr(serve.httpx, "get", answer)
    assert serve.wait_for_health(8765, 5.0) is True
    assert len(calls) == 3
