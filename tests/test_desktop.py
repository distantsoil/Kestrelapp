"""Phase 14 desktop shell, the parts verifiable without a GUI: the graceful-quit lease release (F8),
the worker pause flag, and the tray control endpoints. The window/tray/frozen-build/installer gate
is interactive and local (see the build log)."""

from __future__ import annotations

import json

from sqlalchemy import select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Task
from kestrel.queue.queue import TaskQueue


async def test_release_all_leased_returns_leases_to_pending(sm):
    async with sm() as s:
        # A task leased by a worker that is about to quit, lease not yet expired.
        s.add(Task(type="t1", payload=json.dumps({"opp": 1}), status="leased",
                   worker_id="pipeline-t1-0", lease_until="2999-01-01T00:00:00+00:00",
                   attempts=1, max_attempts=3, created_at=utcnow_iso(), updated_at=utcnow_iso()))
        s.add(Task(type="t1", payload="{}", status="pending",
                   attempts=0, max_attempts=3, created_at=utcnow_iso(), updated_at=utcnow_iso()))
        await s.commit()

    released = await TaskQueue(sm).release_all_leased()
    assert released == 1  # only the leased one

    async with sm() as s:
        tasks = (await s.execute(select(Task).order_by(Task.id))).scalars().all()
    assert all(t.status == "pending" for t in tasks)
    assert all(t.worker_id is None and t.lease_until is None for t in tasks)


def test_pause_flag_and_control_endpoints():
    import asyncio

    from starlette.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.web.app import build_app, is_paused, set_paused

    migrate.run_migrations()  # /api/status + /api/scan/now touch the DB
    asyncio.run(reset_engine())  # let the app build its engine on the request loop
    set_paused(False)
    client = TestClient(build_app(worker=False, scheduler=False))

    assert client.post("/api/worker/pause").json() == {"paused": True}
    assert is_paused() is True
    status = client.get("/api/status").json()
    assert status["paused"] is True
    assert "version" in status

    assert client.post("/api/worker/resume").json() == {"paused": False}
    assert is_paused() is False
    # scan-now only enqueues; with no sources it enqueues nothing and never errors.
    assert client.post("/api/scan/now").status_code == 200


def test_failed_health_check_stops_the_server_and_releases_the_lock(monkeypatch):
    """When /health never comes up, run_desktop used to return with the non-daemon uvicorn thread
    still running. The process stayed alive, windowless, holding the single-instance mutex, so
    every later double-click decided Kestrel was already running and exited."""
    from kestrel.desktop import shell

    class _FakeServer:
        should_exit = False

    class _FakeThread:
        joined_with: float | None = None

        def join(self, timeout: float | None = None) -> None:
            self.joined_with = timeout

        def is_alive(self) -> bool:
            return False

    fake_server, fake_thread = _FakeServer(), _FakeThread()
    released = {"n": 0}

    def _start_server() -> int:
        shell.server, shell.server_thread = fake_server, fake_thread
        return 8765

    monkeypatch.setattr(shell, "server", None)
    monkeypatch.setattr(shell, "server_thread", None)
    monkeypatch.setattr(shell, "_configure_logging", lambda: None)
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "start_server", _start_server)
    monkeypatch.setattr(shell, "release_single_instance",
                        lambda: released.__setitem__("n", released["n"] + 1))
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: False)

    code = shell.run_desktop()

    assert code != 0, "a start that never became healthy must not exit 0"
    assert fake_server.should_exit is True, "the uvicorn thread was left running"
    assert fake_thread.joined_with is not None, "the server thread was not waited for"
    assert released["n"] == 1, "the single-instance mutex was not released"


def test_release_single_instance_forgets_the_handle_even_if_the_os_call_fails(monkeypatch):
    # On a non-Windows host ctypes.WinDLL does not exist; the release must still clear the handle
    # and never raise, because it runs on a path that is already failing.
    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "_mutex_handle", 1234)
    shell.release_single_instance()
    assert shell._mutex_handle is None
    shell.release_single_instance()  # idempotent


def test_focus_bridge_raises_the_window():
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app
    from kestrel.web.desktop_bridge import request_focus, set_focus_handler

    # No handler registered (headless serve): focus is a harmless no-op, never an error.
    set_focus_handler(None)
    assert request_focus() is False

    raised = {"n": 0}
    set_focus_handler(lambda: raised.__setitem__("n", raised["n"] + 1))
    client = TestClient(build_app(worker=False, scheduler=False))
    # A second launch POSTs /api/focus; the registered show_window handler must actually run.
    assert client.post("/api/focus").json() == {"focused": True}
    assert raised["n"] == 1
    set_focus_handler(None)  # leave the global clean for other tests
