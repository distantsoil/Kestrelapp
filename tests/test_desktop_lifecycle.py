"""The desktop start and quit lifecycle with a fake window on real threads (D-UI-89, D-UI-90):
a quit while loading, Try again (once at a time, and after a failed wait for the previous
session), a failing quiet start, a second launch while the first is loading or closing, and
the maximized state across a minimize. No real window, server or network."""

from __future__ import annotations

import json
import os
import sys
import threading

import httpx
import pytest

from kestrel import paths


@pytest.fixture
def shell(monkeypatch):
    from kestrel.desktop import shell as sh

    monkeypatch.setattr(sh, "_configure_logging", lambda: None)
    monkeypatch.setattr(sh, "IS_WINDOWS", False)
    monkeypatch.setattr(sh, "IS_MACOS", False)
    # This fake lifecycle knows only its own process. The POSIX probe sends a console interrupt
    # on Windows when IS_WINDOWS is forced off, stopping pytest and its parent check process.
    monkeypatch.setattr(sh, "_pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(sh, "_server_answers", lambda port: False)
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: httpx.Response(200, json={"focused": False})
    )
    monkeypatch.setattr(sh, "start_tray", lambda **_k: False)
    monkeypatch.setattr(sh, "run_preflight", lambda: None)
    monkeypatch.setattr(sh, "set_app_identity", lambda: False)
    monkeypatch.setattr(sh, "_quitting", False)
    monkeypatch.setattr(sh, "_started", False)
    monkeypatch.setattr(sh, "_boot_failed", False)
    monkeypatch.setattr(sh, "_have_lock", True)
    monkeypatch.setattr(sh, "_focused", False)
    monkeypatch.setattr(sh, "server", None)
    monkeypatch.setattr(sh, "server_thread", None)
    monkeypatch.setattr(sh, "icon", None)
    monkeypatch.setattr(sh.time, "sleep", lambda _s: None)
    monkeypatch.setattr(sh, "_boot_guard", threading.Lock())
    yield sh
    paths.runtime_json().unlink(missing_ok=True)


class _Handler:
    def __init__(self) -> None:
        self.fns: list = []

    def __iadd__(self, fn):
        self.fns.append(fn)
        return self


class _Events:
    def __init__(self) -> None:
        for name in ("closing", "maximized", "minimized", "restored", "shown", "moved",
                     "resized"):
            setattr(self, name, _Handler())


class _Window:
    def __init__(self) -> None:
        self.events = _Events()
        self.js: list[str] = []
        self.loaded: list[str] = []
        self.shown = 0

    def evaluate_js(self, js: str) -> None:
        self.js.append(js)

    def load_url(self, url: str) -> None:
        self.loaded.append(url)

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        pass

    def destroy(self) -> None:
        pass


def _webview(win: _Window, run):
    class _Fake:
        settings: dict = {}

        @staticmethod
        def initialize() -> None:
            pass

        @staticmethod
        def create_window(*a, **k):
            return win

        @staticmethod
        def start(func=None, args=()):
            run(func, args)

    return _Fake


def test_a_quit_while_loading_starts_no_server_and_leaves_nothing_running(shell, monkeypatch):
    """Close the loading window during migrations: the start stops at its next step, never starts
    the server, and run_desktop returns with the lock released."""
    win = _Window()
    in_migrations, go_on = threading.Event(), threading.Event()
    started: list[int] = []
    released = {"n": 0}

    def _migrate(*a, **k):
        in_migrations.set()
        go_on.wait(5)

    def run(func, args):
        t = threading.Thread(target=func, args=args, daemon=True)
        t.start()
        assert in_migrations.wait(5)
        assert shell.on_closing() is True  # the window's close during loading is a quit
        go_on.set()  # the window is gone; the start thread carries on behind it

    monkeypatch.setattr("kestrel.db.migrate.run_migrations", _migrate)
    monkeypatch.setattr(shell, "start_server", lambda: started.append(1) or 8765)
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "release_single_instance",
                        lambda: released.__setitem__("n", released["n"] + 1))
    monkeypatch.setitem(sys.modules, "webview", _webview(win, run))

    assert shell.run_desktop() == 0
    assert started == [], "no server may start after a quit"
    assert released["n"] == 1 and not shell._boot_guard.locked()
    assert not win.loaded
    assert not paths.runtime_json().exists()


def test_try_again_runs_one_start_at_a_time(shell, monkeypatch):
    gate = threading.Event()
    calls: list[bool] = []

    def _steps(busy: bool) -> None:
        calls.append(busy)
        gate.wait(5)

    monkeypatch.setattr(shell, "_boot_steps", _steps)
    assert shell._retry_start() is True
    assert shell._retry_start() is False  # a second click while the first start runs
    gate.set()
    assert shell._boot_guard.acquire(timeout=5)
    shell._boot_guard.release()
    assert calls == [False]


def test_try_again_after_a_failed_wait_waits_for_the_lock_again(shell, monkeypatch):
    """A start that never got the previous session's lock must not boot without it."""
    waited: list[int] = []
    monkeypatch.setattr(shell, "_have_lock", False)
    monkeypatch.setattr(shell, "_wait_for_lock", lambda timeout=0: waited.append(1) or False)
    monkeypatch.setattr(shell, "start_server", lambda: pytest.fail("booted without the lock"))
    monkeypatch.setattr(shell, "window", _Window())
    assert shell._retry_start() is True
    assert shell._boot_guard.acquire(timeout=5)
    shell._boot_guard.release()
    assert waited == [1] and shell._boot_failed is True
    assert any("KSplash.fail(" in js for js in shell.window.js)


def test_a_quiet_start_that_fails_shows_its_window(shell, monkeypatch):
    win = _Window()
    monkeypatch.setattr(sys, "argv", ["kestrel", "--tray"])
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "start_server", lambda: 8765)
    monkeypatch.setattr(shell, "HEALTH_WAIT_S", 0.2)
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: False)
    monkeypatch.setitem(sys.modules, "webview", _webview(win, lambda f, a: f(*a)))
    assert shell.run_desktop() == 1
    assert win.shown >= 1, "a hidden window holding the lock with a failed start"
    assert any("KSplash.fail(" in js for js in win.js)


def _runtime(**info) -> None:
    paths.ensure_dirs()
    paths.runtime_json().write_text(json.dumps(info), encoding="utf-8")


def test_a_second_launch_defers_to_a_first_copy_that_is_still_loading(shell):
    _runtime(pid=os.getpid(), starting=True)
    assert shell._second_launch(wait_s=0.2) == "running"
    _runtime(pid=os.getpid(), failed=True)
    assert shell._second_launch(wait_s=5) == "running"  # its problem card is on screen


def test_a_second_launch_waits_for_a_copy_that_is_closing_or_gone(shell):
    _runtime(pid=os.getpid(), port=1, closing=True)
    assert shell._second_launch(wait_s=5) == "closing"
    _runtime(pid=2**22 + 12345, port=1)  # no such process
    assert shell._second_launch(wait_s=5) == "closing"


def test_a_missing_or_pidless_marker_is_closing_within_a_second_not_a_minute(shell):
    """Mac hardware check (2026-10-27): the last copy removed runtime.json while still holding
    the lock for an instant, and a relaunch waited the full minute."""
    import time

    paths.runtime_json().unlink(missing_ok=True)
    t0 = time.monotonic()
    assert shell._second_launch(wait_s=60) == "closing"
    _runtime(port=1)  # a marker with no pid
    assert shell._second_launch(wait_s=60) == "closing"
    assert time.monotonic() - t0 < 5


def test_focus_counts_only_a_true_focused_answer(shell, monkeypatch):
    import httpx

    answers = iter([{"focused": False}, {"focused": False}, {"focused": True}])

    class _R:
        status_code = 200

        def __init__(self) -> None:
            self.body = next(answers)

        def json(self):
            return self.body

    monkeypatch.setattr(httpx, "post", lambda url, timeout: _R())
    monkeypatch.setattr(shell, "_server_answers", lambda port: True)
    _runtime(pid=os.getpid(), port=4321)
    assert shell.focus_running_instance() is False  # up, but its window is not registered yet
    assert shell._second_launch(wait_s=5) == "focused"  # retried until it is


def test_restoring_from_a_minimize_keeps_the_window_maximized(shell, monkeypatch):
    from kestrel.desktop import frame

    api = frame.WindowApi(lambda: None, close_intent=lambda: "tray", close=lambda: None)
    monkeypatch.setattr(shell, "_frame_api", api)
    monkeypatch.setattr(shell, "_minimized", False)
    shell._on_maximized(True)
    shell._on_minimized()
    shell._on_restored()  # back from the taskbar: still maximized
    assert api.window_info()["maximized"] is True
    shell._on_restored()  # a real restore from maximized
    assert api.window_info()["maximized"] is False


def test_a_relaunch_right_after_exit_shows_the_busy_page_at_once(shell, monkeypatch):
    """The closing copy has removed runtime.json but still holds the lock for a moment: the new
    launch takes the "Finishing closing the last session" path within about a second."""
    import time as _time

    paths.runtime_json().unlink(missing_ok=True)
    t0 = _time.monotonic()
    assert shell._second_launch(wait_s=60) == "closing"
    assert _time.monotonic() - t0 < 3


def test_quitting_keeps_the_closing_screen_up_until_the_server_has_stopped(shell, monkeypatch):
    win = _Window()
    destroyed: list[float] = []
    win.destroy = lambda: destroyed.append(1)

    class _Thread:
        joined = None

        def join(self, timeout=None):
            self.joined = timeout

        def is_alive(self):
            return False

    th = _Thread()
    monkeypatch.setattr(shell, "window", win)
    monkeypatch.setattr(shell, "_started", True)
    monkeypatch.setattr(shell, "_hidden", False)
    monkeypatch.setattr(shell, "server_thread", th)
    monkeypatch.setattr(shell, "icon", object())  # a tray exists, but close-to-tray is off
    monkeypatch.setattr("kestrel.web.deps.close_to_tray", lambda: False)
    monkeypatch.setattr(shell, "_begin_quit", lambda: setattr(shell, "_quitting", True))
    started: list = []
    monkeypatch.setattr(shell.threading, "Thread",
                        lambda target, **k: type("T", (), {"start": lambda self: started.append(
                            target) or target()})())
    assert shell.on_closing() is False  # the OS close waits for the closing screen
    assert any("KUI.frame.closing()" in js for js in win.js)
    assert th.joined == shell.QUIT_JOIN_S and destroyed == [1]
