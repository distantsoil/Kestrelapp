"""Cross-platform support: Kestrel runs on Windows (the packaged desktop shell, unchanged), macOS
and Linux (`uv run kestrel serve` with the UI in the default browser). Everything here is
cloud-safe: sys.platform and the OS tools are monkeypatched; nothing launches a real window or
browser."""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from kestrel import paths

# ------------------------------------------------------------------------------- 1. data_home


def _no_override(monkeypatch, tmp_path):
    # conftest sets KESTREL_DATA_HOME for isolation; these tests exercise the per-OS default.
    monkeypatch.delenv("KESTREL_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def test_data_home_windows_is_localappdata(monkeypatch, tmp_path):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.data_home() == tmp_path / "Local" / "Kestrel"
    monkeypatch.delenv("LOCALAPPDATA")
    assert paths.data_home() == tmp_path / "home" / "AppData" / "Local" / "Kestrel"


def test_data_home_macos_is_application_support(monkeypatch, tmp_path):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("LOCALAPPDATA", "C:/should-be-ignored")
    expected = tmp_path / "home" / "Library" / "Application Support" / "Kestrel"
    assert paths.data_home() == expected


def test_data_home_linux_honors_xdg_then_falls_back(monkeypatch, tmp_path):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_home() == tmp_path / "xdg" / "kestrel"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert paths.data_home() == tmp_path / "home" / ".local" / "share" / "kestrel"


def test_data_home_override_wins_on_every_platform(monkeypatch, tmp_path):
    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path / "override"))
    for plat in ("win32", "darwin", "linux"):
        monkeypatch.setattr(sys, "platform", plat)
        assert paths.data_home() == tmp_path / "override"


# ------------------------------------------------------------------------------ 2. toast channel


class _Run:
    def __init__(self, fail: bool = False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs.get("check") is True and "shell" not in kwargs
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)


@pytest.fixture
def delivery(monkeypatch):
    from kestrel.services import delivery

    monkeypatch.setattr(delivery, "_missing_tools_logged", set())
    return delivery


def test_toast_macos_passes_operator_text_as_arguments_not_script(delivery, monkeypatch):
    run = _Run()
    monkeypatch.setattr(delivery.subprocess, "run", run)
    monkeypatch.setattr(delivery.shutil, "which", lambda n: "/usr/bin/osascript")
    monkeypatch.setattr(sys, "platform", "darwin")
    hostile = 'Acme" & do shell script "rm -rf ~'
    assert delivery._toast(hostile, "body text") is True
    argv = run.calls[0]
    assert argv[:2] == ["/usr/bin/osascript", "-e"]
    assert hostile not in argv[2], "operator text must never be spliced into the script"
    assert argv[3:] == [hostile, "body text"]


def test_toast_linux_uses_notify_send_with_end_of_options(delivery, monkeypatch):
    run = _Run()
    monkeypatch.setattr(delivery.subprocess, "run", run)
    monkeypatch.setattr(delivery.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(sys, "platform", "linux")
    assert delivery._toast("-title that looks like a flag", None) is True
    argv = run.calls[0]
    assert argv[0] == "/usr/bin/notify-send"
    assert argv[argv.index("--") + 1 :] == ["-title that looks like a flag", ""]


def test_toast_windows_uses_win11toast(delivery, monkeypatch):
    import types

    shown = []
    fake = types.SimpleNamespace(notify=lambda t, b: shown.append((t, b)))
    monkeypatch.setitem(sys.modules, "win11toast", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert delivery._toast("t", None) is True
    assert shown == [("t", "")]


def test_toast_missing_tool_is_skipped_and_logged_once(delivery, monkeypatch):
    run = _Run()
    monkeypatch.setattr(delivery.subprocess, "run", run)
    monkeypatch.setattr(delivery.shutil, "which", lambda n: None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert delivery._toast("a", "b") is False
    assert delivery._toast("a", "b") is False
    assert run.calls == []
    assert delivery._missing_tools_logged == {"notify-send"}


async def test_dispatch_never_raises_and_reports_no_toast_when_skipped(delivery, monkeypatch):
    from kestrel.config import get_settings

    s = get_settings()
    s.notify.toast = True
    s.notify.ntfy_topic = None
    s.notify.webhook_url = None
    monkeypatch.setattr(sys, "platform", "linux")
    # Tool absent: skipped, not a channel.
    monkeypatch.setattr(delivery.shutil, "which", lambda n: None)
    assert await delivery._dispatch(s, "t", "b") == []
    # Tool present but failing: caught, not a channel, never raises.
    monkeypatch.setattr(delivery.shutil, "which", lambda n: "/usr/bin/notify-send")
    monkeypatch.setattr(delivery.subprocess, "run", _Run(fail=True))
    assert await delivery._dispatch(s, "t", "b") == []
    # Tool present and working: the channel is reported.
    monkeypatch.setattr(delivery.subprocess, "run", _Run())
    assert await delivery._dispatch(s, "t", "b") == ["toast"]


# ------------------------------------------------------------------------- 3. desktop shell


@pytest.fixture
def shell(monkeypatch):
    from kestrel.desktop import shell

    monkeypatch.setattr(shell, "_mutex_handle", None)
    monkeypatch.setattr(shell, "_lock_file", None)
    monkeypatch.setattr(shell, "window", None)
    monkeypatch.setattr(shell, "icon", None)
    monkeypatch.setattr(shell, "server", None)
    monkeypatch.setattr(shell, "server_thread", None)
    monkeypatch.setattr(shell, "_quitting", False)
    monkeypatch.setattr(shell, "_tray_owns_loop", False)
    yield shell
    shell.release_single_instance()


def test_shell_module_imports_without_windows_apis():
    # The module used to do ctypes.WinDLL("kernel32") at import time, so `import kestrel.desktop`
    # raised on macOS and Linux. Importing it here, on Linux, is the assertion.
    import importlib

    import kestrel.desktop.shell as shell

    importlib.reload(shell)
    assert shell.IS_WINDOWS is (sys.platform == "win32")


@pytest.mark.skipif(sys.platform == "win32", reason="flock path is macOS/Linux")
def test_lock_file_detects_a_second_instance_and_focuses_it(shell, monkeypatch):
    posted: list[str] = []

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, timeout: posted.append(url))
    from kestrel.web.serve import write_runtime

    write_runtime(4321, os.getpid())

    assert shell.acquire_single_instance() is True
    assert shell.lock_file_path().exists()
    assert shell.lock_file_path().parent == paths.data_home()
    held = shell._lock_file

    # A second acquire (a distinct open file description, exactly like a second process) is
    # refused, and the running instance is focused through /api/focus, never raised directly.
    monkeypatch.setattr(shell, "_lock_file", None)
    assert shell.acquire_single_instance() is False
    assert posted == ["http://127.0.0.1:4321/api/focus"]

    # Release the first and the lock is free again.
    monkeypatch.setattr(shell, "_lock_file", held)
    shell.release_single_instance()
    assert shell._lock_file is None
    assert shell.acquire_single_instance() is True


@pytest.mark.skipif(sys.platform == "win32", reason="flock path is macOS/Linux")
def test_focus_failure_on_second_instance_never_raises(shell, monkeypatch):
    assert shell.acquire_single_instance() is True
    monkeypatch.setattr(shell, "_lock_file", None)
    paths.runtime_json().unlink(missing_ok=True)  # no runtime.json: nothing to focus
    assert shell.acquire_single_instance() is False  # reports, does not crash


# ---- per-data-home single-instance lock tests -----------------------------------------


def test_mutex_name_default_data_home_is_canonical(monkeypatch):
    """With no KESTREL_DATA_HOME override, the mutex name is exactly MUTEX_NAME."""
    from kestrel.desktop import shell

    monkeypatch.delenv("KESTREL_DATA_HOME", raising=False)
    importlib.reload(shell)
    assert shell._mutex_name() == shell.MUTEX_NAME


def test_mutex_name_non_default_data_home_is_hashed(monkeypatch, tmp_path):
    """A non-default data home yields a suffixed mutex name."""
    from kestrel.desktop import shell

    custom = tmp_path / "custom"
    monkeypatch.setenv("KESTREL_DATA_HOME", str(custom))
    importlib.reload(shell)
    assert shell._mutex_name() != shell.MUTEX_NAME
    assert shell._mutex_name().startswith("Local\\Kestrel-")


def test_two_different_data_homes_give_different_names(monkeypatch, tmp_path):
    """Two distinct data homes produce two distinct mutex/lock names."""
    from kestrel.desktop import shell

    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("KESTREL_DATA_HOME", str(a))
    importlib.reload(shell)
    name_a = shell._mutex_name()

    monkeypatch.setenv("KESTREL_DATA_HOME", str(b))
    importlib.reload(shell)
    name_b = shell._mutex_name()

    assert name_a != name_b
    assert name_a.startswith("Local\\Kestrel-")
    assert name_b.startswith("Local\\Kestrel-")


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are the Windows rule")
def test_same_data_home_different_case_gives_same_name(monkeypatch, tmp_path):
    """On Windows, mutex names are case-sensitive, so C:\\ and c:\\ must not split.
    We normalize with os.path.normcase so the hash is the same regardless of case."""
    from kestrel.desktop import shell

    # Use a path with mixed case
    mixed = tmp_path / "MyFolder"
    monkeypatch.setenv("KESTREL_DATA_HOME", str(mixed))
    importlib.reload(shell)
    name1 = shell._mutex_name()

    # Same path, different case
    mixed_lower = str(mixed).lower()
    monkeypatch.setenv("KESTREL_DATA_HOME", mixed_lower)
    importlib.reload(shell)
    name2 = shell._mutex_name()

    assert name1 == name2


def test_keystrel_data_home_explicitly_set_to_default_is_canonical(monkeypatch, tmp_path):
    """When KESTREL_DATA_HOME is explicitly set to the default data home path,
    the mutex name should still be the canonical MUTEX_NAME (not a hashed variant).
    This ensures the installer's AppMutex check and single-instance detection
    work correctly even when the env var is set to the default location."""
    from kestrel import paths
    from kestrel.desktop import shell

    # Get the default data home by temporarily clearing KESTREL_DATA_HOME
    monkeypatch.delenv("KESTREL_DATA_HOME", raising=False)
    importlib.reload(paths)
    default_path = paths.data_home()

    # Now explicitly set KESTREL_DATA_HOME to that same path
    monkeypatch.setenv("KESTREL_DATA_HOME", str(default_path))
    importlib.reload(paths)
    importlib.reload(shell)

    # Should return canonical MUTEX_NAME, not a hashed variant
    assert shell._mutex_name() == shell.MUTEX_NAME, (
        f"Expected {shell.MUTEX_NAME}, got {shell._mutex_name()}"
    )
    assert shell._lock_file_name() == shell.LOCK_FILENAME, (
        f"Expected {shell.LOCK_FILENAME}, got {shell._lock_file_name()}"
    )


def _point_default_home_at(monkeypatch, base: Path) -> Path:
    """Make the per-OS default data home live under base (via the env vars paths reads), so a
    test can alias it without touching the real one. Returns the default home."""
    monkeypatch.delenv("KESTREL_DATA_HOME", raising=False)
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(base))
    elif sys.platform == "darwin":
        monkeypatch.setenv("HOME", str(base))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(base))
    home = paths.default_data_home()
    home.mkdir(parents=True)
    return home


def _alias_dir(target: Path, alias: Path) -> None:
    """A second spelling of one folder: a junction on Windows (no privilege needed), a symlink
    elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(alias))
    else:
        os.symlink(target, alias, target_is_directory=True)


def test_default_data_home_ignores_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path / "elsewhere"))
    assert paths.data_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("KESTREL_DATA_HOME")
    assert paths.default_data_home() == paths.data_home()


def test_default_home_reached_through_an_alias_is_canonical(monkeypatch, tmp_path):
    """KESTREL_DATA_HOME naming the default folder through a junction or symlink is still the
    default folder: it must take MUTEX_NAME, or two instances could open one database."""
    from kestrel.desktop import shell

    real_base = tmp_path / "real"
    home = _point_default_home_at(monkeypatch, real_base)
    alias_base = tmp_path / "alias"
    try:
        _alias_dir(real_base, alias_base)
    except OSError as exc:  # symlinks can be refused by policy; nothing to test then
        pytest.skip(f"cannot create a directory alias here: {exc}")
    monkeypatch.setenv("KESTREL_DATA_HOME", str(alias_base / home.relative_to(real_base)))
    assert shell._mutex_name() == shell.MUTEX_NAME


def test_two_aliases_of_one_custom_folder_share_a_name(monkeypatch, tmp_path):
    from kestrel.desktop import shell

    real = tmp_path / "custom"
    real.mkdir()
    alias = tmp_path / "custom-alias"
    try:
        _alias_dir(real, alias)
    except OSError as exc:
        pytest.skip(f"cannot create a directory alias here: {exc}")
    monkeypatch.setenv("KESTREL_DATA_HOME", str(real))
    name_real = shell._mutex_name()
    monkeypatch.setenv("KESTREL_DATA_HOME", str(alias))
    assert shell._mutex_name() == name_real != shell.MUTEX_NAME


def test_posix_lock_file_is_always_kestrel_lock(monkeypatch, tmp_path):
    """The lock file sits inside the data home, so it is per folder already. A hashed name let a
    0.5 instance (kestrel.lock) and a 0.6 instance on the same folder both get a lock."""
    from kestrel.desktop import shell

    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path / "custom"))
    assert shell._lock_file_name() == shell.LOCK_FILENAME == "kestrel.lock"
    assert shell.lock_file_path() == tmp_path / "custom" / "kestrel.lock"


def test_lock_name_does_not_reload_paths(monkeypatch, tmp_path):
    """Computing the name used to importlib.reload(paths), wiping every monkeypatch on it."""
    from kestrel.desktop import shell

    marker = tmp_path / "runtime.json"
    monkeypatch.setattr(paths, "runtime_json", lambda: marker)
    monkeypatch.setenv("KESTREL_DATA_HOME", str(tmp_path / "custom"))
    shell._mutex_name()
    shell._lock_file_name()
    assert paths.runtime_json() == marker
    assert os.environ["KESTREL_DATA_HOME"] == str(tmp_path / "custom")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex test")
def test_windows_two_different_data_homes_can_both_hold_lock(monkeypatch, tmp_path):
    """On Windows, two processes with different data homes can both hold the mutex."""
    from kestrel.desktop import shell

    a = tmp_path / "a"
    b = tmp_path / "b"

    # First data home
    monkeypatch.setenv("KESTREL_DATA_HOME", str(a))
    importlib.reload(shell)
    assert shell.acquire_single_instance() is True
    shell.release_single_instance()

    # Second data home
    monkeypatch.setenv("KESTREL_DATA_HOME", str(b))
    importlib.reload(shell)
    assert shell.acquire_single_instance() is True
    shell.release_single_instance()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex test")
def test_windows_same_data_home_cannot_hold_twice(monkeypatch, tmp_path):
    """On Windows, two attempts with the same data home cannot both hold the mutex."""
    from kestrel.desktop import shell

    custom = tmp_path / "custom"
    monkeypatch.setenv("KESTREL_DATA_HOME", str(custom))
    importlib.reload(shell)

    # First acquire succeeds
    assert shell.acquire_single_instance() is True
    # Second acquire (simulated by clearing _mutex_handle) fails
    shell._mutex_handle = None
    assert shell.acquire_single_instance() is False
    shell.release_single_instance()


def test_tray_start_is_a_log_line_not_a_crash_without_a_display(shell, monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)  # ImportError on import
    assert shell.start_tray() is False
    assert shell.icon is None


class _FakeIcon:
    """Records which thread touched it, the way AppKit would care on macOS."""

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, str]] = []
        self._visible = False

    def _log(self, what: str) -> None:
        self.calls.append((what, threading.current_thread().name))

    def run(self, setup=None):
        self._log("run")

    def run_detached(self, setup=None):
        self._log("run_detached")

    def stop(self):
        self._log("stop")

    def update_menu(self):
        self._log("update_menu")

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        self._log(f"visible={value}")
        self._visible = value


def _fake_pystray():
    class _Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    return types.SimpleNamespace(Icon=_FakeIcon, Menu=_Menu, MenuItem=lambda *a, **k: (a, k))


def _quiet_tray(shell, monkeypatch, *, macos: bool):
    monkeypatch.setattr(shell, "IS_MACOS", macos)
    monkeypatch.setattr(shell, "_icons", lambda: {"idle": object()})
    monkeypatch.setattr(shell, "_tooltip_poller", lambda: None)
    monkeypatch.setitem(sys.modules, "pystray", _fake_pystray())


def test_macos_tray_is_built_on_the_main_thread_not_its_own(shell, monkeypatch):
    # macOS 27 kills the app when a status item's menu is set off the main thread (0.6.1 crash:
    # NSStatusItem setMenu: from the "tray" thread, BSServiceMainRunLoopQueue assertBarrier).
    _quiet_tray(shell, monkeypatch, macos=True)
    assert shell.start_tray() is True
    main = threading.current_thread().name
    assert shell.icon.calls == [("run_detached", main), ("visible=True", main)]


def test_windows_and_linux_tray_still_runs_its_own_loop(shell, monkeypatch):
    _quiet_tray(shell, monkeypatch, macos=False)
    assert shell.start_tray() is True
    for _ in range(100):
        if shell.icon.calls:
            break
        time.sleep(0.01)
    assert shell.icon.calls == [("run", "tray")]


def test_macos_tray_changes_from_a_thread_are_queued_to_the_main_loop(shell, monkeypatch):
    queued: list = []
    tools = types.ModuleType("PyObjCTools")
    tools.AppHelper = types.SimpleNamespace(callAfter=queued.append)
    monkeypatch.setitem(sys.modules, "PyObjCTools", tools)
    monkeypatch.setattr(shell, "IS_MACOS", True)
    ran: list = []
    worker = threading.Thread(target=shell._on_tray_thread, args=(lambda: ran.append(1),))
    worker.start()
    worker.join()
    assert ran == [] and len(queued) == 1  # not run on the worker thread
    queued[0]()
    assert ran == [1]


def test_macos_tray_change_on_the_main_thread_runs_at_once(shell, monkeypatch):
    # on_closing runs on the main thread; a queued hide could miss the loop's last turn.
    monkeypatch.setattr(shell, "IS_MACOS", True)
    ran: list = []
    shell._on_tray_thread(lambda: ran.append(1))
    assert ran == [1]


def test_macos_menu_actions_leave_the_main_thread(shell, monkeypatch):
    # AppKit calls menu actions on the main thread; window.evaluate_js waits on that thread, so
    # "Update available" would freeze the app if the action ran there.
    monkeypatch.setattr(shell, "IS_MACOS", True)
    release = threading.Event()
    seen: list[str] = []

    def slow() -> None:
        seen.append(threading.current_thread().name)
        release.wait(5)

    started = time.monotonic()
    shell._menu_action(slow)(None, None)
    assert time.monotonic() - started < 1.0  # returned without waiting for the action
    release.set()
    for _ in range(100):
        if seen:
            break
        time.sleep(0.01)
    assert seen == ["tray-action"]


def test_windows_menu_actions_run_in_place(shell, monkeypatch):
    monkeypatch.setattr(shell, "IS_MACOS", False)
    seen: list[str] = []
    shell._menu_action(lambda: seen.append(threading.current_thread().name))(None, None)
    assert seen == [threading.current_thread().name]


def test_macos_paused_check_reads_the_last_status_without_http(shell, monkeypatch):
    _quiet_tray(shell, monkeypatch, macos=True)
    monkeypatch.setattr(shell, "_status", lambda: pytest.fail("HTTP on the main thread"))
    monkeypatch.setattr(shell, "_last_status", {"paused": True})
    items: list = []
    real = shell.sys.modules["pystray"]
    real.MenuItem = lambda *a, **k: items.append((a, k)) or (a, k)
    assert shell.start_tray() is True
    checked = next(k["checked"] for a, k in items if k.get("checked"))
    assert checked(None) is True


def test_macos_quit_hides_the_tray_instead_of_stopping_the_shared_loop(shell, monkeypatch):
    # pystray's stop() on macOS calls NSApp.stop_, which is pywebview's loop, not the tray's.
    monkeypatch.setattr(shell, "IS_MACOS", True)
    monkeypatch.setattr(shell, "_on_tray_thread", lambda fn: fn())
    fake = _FakeIcon()
    fake._visible = True
    monkeypatch.setattr(shell, "icon", fake)
    shell._begin_quit()
    assert [what for what, _ in fake.calls] == ["visible=False"]


def test_macos_browser_fallback_tray_runs_its_loop_on_the_main_thread(shell, monkeypatch):
    # No window loop to join, so the icon's own run() is the main loop: nothing starts in
    # start_tray, and run_tray_loop shows the item then runs it, both on the calling thread.
    _quiet_tray(shell, monkeypatch, macos=True)
    assert shell.start_tray(with_window=False) is True
    assert shell._tray_owns_loop is True
    assert shell.icon.calls == []
    shell.run_tray_loop()
    main = threading.current_thread().name
    assert shell.icon.calls == [("visible=True", main), ("run", main)]


def test_a_quit_before_the_owned_tray_loop_starts_never_leaves_it_blocked(shell, monkeypatch):
    # pystray's stop() is a no-op until run() has started, so a quit landing first must either
    # skip the loop or stop it from setup once it is live.
    _quiet_tray(shell, monkeypatch, macos=True)
    assert shell.start_tray(with_window=False) is True
    monkeypatch.setattr(shell, "_quitting", True)
    shell.run_tray_loop()
    assert shell.icon.calls == []
    stopped = []
    monkeypatch.setattr(shell, "_quitting", False)
    seen = {}
    monkeypatch.setattr(shell.icon, "run", lambda setup=None: seen.update(setup=setup))
    shell.run_tray_loop()
    monkeypatch.setattr(shell, "_quitting", True)
    seen["setup"](type("I", (), {"stop": lambda self: stopped.append(1)})())
    assert stopped == [1]


def test_window_path_tray_never_owns_the_loop(shell, monkeypatch):
    _quiet_tray(shell, monkeypatch, macos=True)
    assert shell.start_tray() is True
    assert shell._tray_owns_loop is False
    shell.run_tray_loop()  # a no-op, not a second loop under the window
    assert [what for what, _ in shell.icon.calls] == ["run_detached", "visible=True"]


def test_quit_stops_the_tray_loop_it_owns(shell, monkeypatch):
    # With the tray's run() as the main loop, hiding the item would leave run() blocking forever;
    # stop() is what returns the main thread to _finish_quit.
    monkeypatch.setattr(shell, "IS_MACOS", True)
    monkeypatch.setattr(shell, "_tray_owns_loop", True)
    fake = _FakeIcon()
    fake._visible = True
    monkeypatch.setattr(shell, "icon", fake)
    shell._begin_quit()
    assert [what for what, _ in fake.calls] == ["stop"]


def test_a_tray_that_fails_to_show_leaves_no_icon_so_a_close_quits(shell, monkeypatch):
    # The icon is published only once it is up: a backend that constructs but cannot show the
    # item must not leave on_closing hiding the window behind an item that is not there.
    _quiet_tray(shell, monkeypatch, macos=True)

    class _BrokenIcon(_FakeIcon):
        def run_detached(self, setup=None):
            raise RuntimeError("no status bar")

    shell.sys.modules["pystray"].Icon = _BrokenIcon
    assert shell.start_tray() is False
    assert shell.icon is None

    class _FakeServer:
        should_exit = False

    monkeypatch.setattr(shell, "server", _FakeServer())
    monkeypatch.setattr("kestrel.web.deps.close_to_tray", lambda: True)
    assert shell.on_closing() is True
    assert shell.server.should_exit is True


def test_poller_rebinds_last_status_whole_and_redraws_on_a_pause_change(shell, monkeypatch):
    # The macOS menu reads _last_status on the main thread: it must never see a half-filled dict,
    # and the "Pause worker" check is only re-read on update_menu, so a pause change must queue one.
    class _NeverEmptied(dict):
        def clear(self):
            pytest.fail("_last_status was emptied in place")

        def update(self, *a, **k):
            pytest.fail("_last_status was filled in place")

    answers = iter([{"paused": True}, {"paused": True}, {"paused": False}, {}])
    ticks: list[int] = []

    class _Done(Exception):
        pass

    def _sleep(_s: float) -> None:
        ticks.append(1)
        if len(ticks) == 4:
            raise _Done

    fake = _FakeIcon()
    monkeypatch.setattr(shell, "icon", fake)
    monkeypatch.setattr(shell, "_last_status", _NeverEmptied())
    monkeypatch.setattr(shell, "_status", lambda: next(answers))
    monkeypatch.setattr(shell, "_poll_update_status", lambda: None)
    monkeypatch.setattr(shell, "_icons", lambda: {"idle": object()})
    monkeypatch.setattr(shell, "_on_tray_thread", lambda fn: fn())
    monkeypatch.setattr(shell.time, "sleep", _sleep)
    with pytest.raises(_Done):
        shell._tooltip_poller()
    assert shell._last_status == {}
    # Redrawn on False->True (tick 1), not on True->True (tick 2), on True->False (tick 3), and
    # not on False->False (tick 4, an empty answer).
    assert [what for what, _ in fake.calls if what == "update_menu"] == ["update_menu"] * 2


def test_server_death_ends_the_tray_loop(shell, monkeypatch):
    class _FakeThread:
        def join(self, timeout=None) -> None:
            pass

    quits: list[int] = []
    monkeypatch.setattr(shell, "server_thread", _FakeThread())
    monkeypatch.setattr(shell, "do_quit", lambda: quits.append(1))
    shell._quit_when_server_stops()
    assert quits == [1]
    monkeypatch.setattr(shell, "_quitting", True)
    shell._quit_when_server_stops()
    assert quits == [1]  # a quit already under way is not started again


def test_macos_browser_fallback_wires_quit_and_runs_the_tray_loop(shell, monkeypatch):
    """A packaged app on the fallback path has no terminal: the tray's Quit and the in-app Quit
    button must both end the process. The tray loop runs on the main thread and a quit stops it."""
    from kestrel.web import desktop_bridge

    class _FakeServer:
        should_exit = False

    class _FakeThread:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout=None) -> None:
            for _ in range(500):  # the real thread ends when the server is told to exit
                if fake_server.should_exit:
                    break
                time.sleep(0.01)
            self.alive = False

    fake_server, fake_thread = _FakeServer(), _FakeThread()
    handlers: list = []
    _quiet_tray(shell, monkeypatch, macos=True)
    monkeypatch.setattr(shell, "server", fake_server)
    monkeypatch.setattr(shell, "server_thread", fake_thread)
    monkeypatch.setattr(desktop_bridge, "set_quit_handler", handlers.append)
    monkeypatch.setattr(desktop_bridge, "set_focus_handler", lambda fn: None)
    monkeypatch.setattr(shell.webbrowser, "open", lambda url: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)

    assert shell._run_in_browser("http://127.0.0.1:8765/") == 0

    assert handlers == [shell.do_quit, None]
    main = threading.current_thread().name
    assert shell.icon.calls == [("visible=True", main), ("run", main), ("stop", main)]
    assert fake_server.should_exit is True
    assert fake_thread.alive is False
    # The fallback starts a daemon "server-watch" thread that calls do_quit() when the server
    # stops on its own. Wait for it here: left running, it finished after this test's teardown and
    # set the module's _quitting during the NEXT test, which then skipped its own quit (#14 CI).
    for t in threading.enumerate():
        if t.name == "server-watch":
            t.join(timeout=5)
            assert not t.is_alive(), "the server-watch thread must end with the test"


def test_open_folder_uses_the_platform_opener(shell, monkeypatch):
    spawned: list[list[str]] = []
    monkeypatch.setattr(shell.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    shell._open_folder(Path("/tmp/x"))
    monkeypatch.setattr(sys, "platform", "linux")
    shell._open_folder(Path("/tmp/y"))
    # str(Path(...)) so the expectation carries the host's separator (Windows CI runs this too).
    assert spawned == [["open", str(Path("/tmp/x"))], ["xdg-open", str(Path("/tmp/y"))]]


def test_run_desktop_falls_back_to_the_browser_when_webview_is_unavailable(shell, monkeypatch):
    """No pywebview backend on macOS/Linux: the server stays up, the URL is printed and opened in
    the default browser, and the process waits (for Ctrl+C) instead of exiting or crashing."""
    opened: list[str] = []
    printed: list[str] = []

    class _FakeServer:
        should_exit = False

    class _FakeThread:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout=None) -> None:
            self.alive = False

    fake_server, fake_thread = _FakeServer(), _FakeThread()

    def _start_server() -> int:
        shell.server, shell.server_thread = fake_server, fake_thread
        return 8765

    def _sleep(_s: float) -> None:
        raise KeyboardInterrupt  # the operator's Ctrl+C after the first wait tick

    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    monkeypatch.setattr(shell, "_configure_logging", lambda: None)
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "start_server", _start_server)
    monkeypatch.setattr(shell, "run_preflight", lambda: None)
    monkeypatch.setattr(shell, "start_tray", lambda **_k: False)
    monkeypatch.setattr(shell.time, "sleep", _sleep)
    monkeypatch.setattr(shell.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: True)
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` -> ImportError

    code = shell.run_desktop()

    assert code == 0
    assert opened == ["http://127.0.0.1:8765/"]
    assert any("http://127.0.0.1:8765/" in line for line in printed)
    assert fake_server.should_exit is True, "Ctrl+C must run the graceful quit"
    assert fake_thread.alive is False, "the server thread must be joined before exit"
    assert not paths.runtime_json().exists()


def test_run_desktop_on_windows_still_raises_when_webview_is_missing(shell, monkeypatch):
    # The packaged product's behavior is unchanged: a broken WebView2 is a real failure there.
    class _FakeServer:
        should_exit = False

    class _FakeThread:
        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    def _start_server() -> int:
        shell.server, shell.server_thread = _FakeServer(), _FakeThread()
        return 8765

    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell, "_configure_logging", lambda: None)
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "start_server", _start_server)
    monkeypatch.setattr(shell, "run_preflight", lambda: None)
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: True)
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(ImportError):
        shell.run_desktop()


# ------------------------------------------------------------------------- 4. browser ladder


class _Chromium:
    def __init__(self, ok_when):
        self.ok_when = ok_when  # predicate over launch kwargs
        self.attempts: list[dict] = []

    async def launch(self, **kwargs):
        self.attempts.append(kwargs)
        if self.ok_when(kwargs):
            return f"browser:{kwargs}"
        raise RuntimeError(f"not installed: {kwargs}")


class _PW:
    def __init__(self, ok_when):
        self.chromium = _Chromium(ok_when)


@pytest.fixture
def render(monkeypatch):
    from kestrel.docs import render

    monkeypatch.delenv(render.BROWSER_PATH_ENV, raising=False)
    return render


def test_launch_order_is_edge_chrome_then_bundled_chromium(render):
    pw = _PW(lambda kw: "channel" not in kw)  # only the bundled Chromium is present
    browser = asyncio.run(render.launch_browser(pw))
    assert browser == "browser:{'headless': True}"
    assert [a.get("channel") for a in pw.chromium.attempts] == ["msedge", "chrome", None]


def test_edge_is_still_first_when_present(render):
    pw = _PW(lambda kw: True)
    asyncio.run(render.launch_browser(pw))
    assert pw.chromium.attempts == [{"headless": True, "channel": "msedge"}]


def test_browser_path_env_is_tried_first_as_executable_path(render, monkeypatch):
    monkeypatch.setenv(render.BROWSER_PATH_ENV, "/opt/brave/brave")
    pw = _PW(lambda kw: kw.get("executable_path") == "/opt/brave/brave")
    asyncio.run(render.launch_browser(pw))
    assert pw.chromium.attempts == [{"headless": True, "executable_path": "/opt/brave/brave"}]


def test_no_browser_raises_a_clear_error_naming_the_install_command(render):
    pw = _PW(lambda kw: False)
    with pytest.raises(render.BrowserUnavailable) as exc:
        asyncio.run(render.launch_browser(pw))
    msg = str(exc.value)
    assert "playwright install chromium" in msg
    assert "msedge" in msg and "chrome" in msg and render.BROWSER_PATH_ENV in msg
    assert len(pw.chromium.attempts) == 3


def test_render_pdf_goes_through_the_ladder(render, monkeypatch):
    # render_pdf must not keep a private channel="msedge" launch of its own.
    import inspect

    src = inspect.getsource(render.render_pdf)
    assert "launch_browser(" in src and 'channel="msedge"' not in src


def test_detect_browser_mirrors_the_ladder(render, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    assert render.detect_browser(exists=lambda p: p == edge) == ("Edge (msedge)", str(edge))
    assert render.detect_browser(exists=lambda p: p == chrome) == ("Chrome (chrome)", str(chrome))
    monkeypatch.setattr(render, "_bundled_chromium", lambda: Path("/c/chromium-1234"))
    assert render.detect_browser(exists=lambda p: False) == (
        "Playwright Chromium",
        str(Path("/c/chromium-1234")),
    )
    monkeypatch.setattr(render, "_bundled_chromium", lambda: None)
    assert render.detect_browser(exists=lambda p: False) is None
    monkeypatch.setenv(render.BROWSER_PATH_ENV, "/x/browser")
    assert render.detect_browser(exists=lambda p: True) == (render.BROWSER_PATH_ENV, "/x/browser")
    assert render.browser_path_warning(exists=lambda p: True) is None


def test_wrong_browser_path_env_falls_through_with_a_warning(render, monkeypatch):
    """A KESTREL_BROWSER_PATH naming nothing on disk is not "no browser": the probe falls
    through to Edge/Chrome/Chromium exactly as launch_browser does, and doctor says why."""
    from kestrel.doctor import PASS, WARN, DoctorReport, _check_browser

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv(render.BROWSER_PATH_ENV, "/x/missing")
    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    assert render.detect_browser(exists=lambda p: p == edge) == ("Edge (msedge)", str(edge))
    warning = render.browser_path_warning(exists=lambda p: p == edge)
    assert warning and "/x/missing" in warning and render.BROWSER_PATH_ENV in warning
    # doctor: the browser it will use, plus the warning text, as a WARN so the typo is seen
    monkeypatch.setattr(render, "detect_browser", lambda: ("Edge (msedge)", str(edge)))
    rep = DoctorReport()
    _check_browser(rep)
    row = rep.results[0]
    assert row.status == WARN and str(edge) in row.detail and "/x/missing" in row.detail
    # with the variable pointing at a real file, PASS and no warning text
    monkeypatch.setattr(render, "browser_path_warning", lambda: None)
    rep = DoctorReport()
    _check_browser(rep)
    assert rep.results[0].status == PASS and "/x/missing" not in rep.results[0].detail


def test_bundled_chromium_probe_matches_the_installed_playwright_revision(
    render, monkeypatch, tmp_path
):
    # A Chromium left behind by an older Playwright is in the same cache under another revision
    # and this Playwright cannot launch it; the probe must not report it as present.
    monkeypatch.setattr(render, "playwright_browsers_dir", lambda: tmp_path)
    monkeypatch.setattr(
        render, "_expected_chromium_dirs", lambda: ["chromium-1243", "chromium_headless_shell-1243"]
    )
    (tmp_path / "chromium_headless_shell-1194").mkdir()
    assert render._bundled_chromium() is None
    (tmp_path / "chromium-1243").mkdir()
    assert render._bundled_chromium() == tmp_path / "chromium-1243"
    # Without browsers.json knowledge, any chromium dir counts (the loose fallback).
    monkeypatch.setattr(render, "_expected_chromium_dirs", lambda: [])
    (tmp_path / "chromium-1243").rmdir()
    assert render._bundled_chromium() == tmp_path / "chromium_headless_shell-1194"


def test_expected_chromium_dirs_reads_the_installed_playwright_metadata(render):
    dirs = render._expected_chromium_dirs()
    assert dirs and all(d.startswith(("chromium-", "chromium_headless_shell-")) for d in dirs)


def test_playwright_browsers_dir_per_platform(render, monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert render.playwright_browsers_dir() == tmp_path / "Library" / "Caches" / "ms-playwright"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert render.playwright_browsers_dir() == tmp_path / ".cache" / "ms-playwright"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    assert render.playwright_browsers_dir() == tmp_path / "pw"


def test_doctor_browser_check_passes_on_any_and_warns_never_fails_on_none(monkeypatch):
    from kestrel.docs import render
    from kestrel.doctor import PASS, WARN, DoctorReport, _check_browser

    monkeypatch.setattr(render, "detect_browser", lambda: ("Chrome (chrome)", "/usr/bin/chrome"))
    rep = DoctorReport()
    _check_browser(rep)
    row = rep.results[0]
    assert row.name == "Browser (PDF)" and row.status == PASS
    assert "Chrome" in row.detail and "/usr/bin/chrome" in row.detail

    monkeypatch.setattr(render, "detect_browser", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    rep = DoctorReport()
    _check_browser(rep)
    row = rep.results[0]
    assert row.status == WARN and row.blocking is False
    assert "uv run playwright install chromium" in row.remediation
    assert rep.has_blocking_failure is False

    monkeypatch.setattr(sys, "platform", "win32")
    rep = DoctorReport()
    _check_browser(rep)
    assert "Edge" in rep.results[0].remediation


def test_doctor_browser_check_is_wired_into_run_doctor():
    import inspect

    from kestrel import doctor

    src = inspect.getsource(doctor.run_doctor)
    assert "_check_browser(rep)" in src and "_check_edge" not in src


@pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)
async def test_live_ladder_launches_some_browser(render, browser_fallback):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await render.launch_browser(p)
        await browser.close()


# ------------------------------------------------------------------------ 5. kestrel serve --open


def test_should_open_browser_defaults(monkeypatch):
    from kestrel.web import serve

    monkeypatch.delenv("KESTREL_HEADLESS", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert serve.should_open_browser() is False  # the desktop shell is the window on Windows
    monkeypatch.setattr(sys, "platform", "darwin")
    assert serve.should_open_browser() is True
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert serve.should_open_browser() is False
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert serve.should_open_browser() is True
    monkeypatch.setenv("KESTREL_HEADLESS", "1")
    assert serve.should_open_browser() is False
    monkeypatch.setenv("KESTREL_HEADLESS", "0")
    assert serve.should_open_browser() is True


def test_open_browser_waits_for_health_then_opens_in_a_daemon_thread(monkeypatch):
    from kestrel.web import serve

    opened: list[str] = []
    waited: list[int] = []
    monkeypatch.setattr(
        serve, "wait_for_health", lambda port, timeout=30.0: waited.append(port) or True
    )
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    t = serve.open_browser_when_healthy(8765, "http://127.0.0.1:8765/")
    assert t.daemon is True
    t.join(timeout=5)
    assert waited == [8765] and opened == ["http://127.0.0.1:8765/"]


def test_open_browser_does_not_open_when_health_never_comes_up(monkeypatch):
    from kestrel.web import serve

    opened: list[str] = []
    monkeypatch.setattr(serve, "wait_for_health", lambda port, timeout=30.0: False)
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    serve.open_browser_when_healthy(1, "http://127.0.0.1:1/").join(timeout=5)
    assert opened == []


def test_serve_announces_the_url_opens_once_healthy_and_stops_cleanly_on_ctrl_c(monkeypatch):
    """The whole `kestrel serve` contract without a real uvicorn: the URL is always announced, the
    browser opener runs only when asked, KeyboardInterrupt (uvicorn re-raises the SIGINT it handled
    after the graceful shutdown) is a clean stop, and runtime.json is removed."""
    from kestrel.web import serve

    announced: list[str] = []
    opened: list[tuple[int, str]] = []

    class _Server:
        def run(self, sockets):
            for s in sockets:
                s.close()
            assert paths.runtime_json().exists()
            raise KeyboardInterrupt

    real_bind = serve.bind_free_port
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    # Ephemeral loopback port: never a ladder rung, never LAN-visible, from a test.
    monkeypatch.setattr(serve, "bind_free_port", lambda: real_bind(ladder=False, host="127.0.0.1"))
    monkeypatch.setattr(serve, "build_app", lambda worker: object())
    monkeypatch.setattr(serve, "_make_server", lambda app, log_level: _Server())
    monkeypatch.setattr(
        serve,
        "open_browser_when_healthy",
        lambda port, url, timeout=30.0: opened.append((port, url)),
    )

    serve.serve(open_browser=False, announce=announced.append)
    assert opened == []
    assert announced[0].startswith("Kestrel is serving at http://127.0.0.1:")
    assert announced[-1] == "Kestrel stopped."
    assert not paths.runtime_json().exists()

    serve.serve(open_browser=True, announce=announced.append)
    assert len(opened) == 1 and opened[0][1] == f"http://127.0.0.1:{opened[0][0]}/"

    # None defers to the platform default.
    monkeypatch.setattr(serve, "should_open_browser", lambda: True)
    serve.serve(open_browser=None)
    assert len(opened) == 2


def test_cli_serve_passes_the_open_flag_through(monkeypatch):
    from typer.testing import CliRunner

    from kestrel import cli
    from kestrel.web import serve

    seen: list[dict] = []
    monkeypatch.setattr(serve, "serve", lambda **kw: seen.append(kw))
    runner = CliRunner()
    for args, expect in (
        (["serve"], None),
        (["serve", "--open"], True),
        (["serve", "--no-open", "--no-worker"], False),
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert seen[-1]["open_browser"] is expect
        assert callable(seen[-1]["announce"])
    assert seen[-1]["worker"] is False


# ---------------------------------------------------------------------- 6. dependency markers


def test_windows_only_dependencies_carry_environment_markers():
    import tomllib

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    deps = tomllib.loads(text)["project"]["dependencies"]
    by_name = {d.split(";")[0].strip().split(" ")[0]: d for d in deps}
    assert "sys_platform == 'win32'" in by_name["win11toast"]
    assert "sys_platform == 'win32'" in by_name["pythonnet"]
    assert ";" not in by_name["pywebview"] and ";" not in by_name["pystray"]


def test_windows_only_packages_are_never_imported_at_module_level():
    # A top-level import of any of these would make `kestrel serve` unimportable off Windows.
    import re

    root = Path(__file__).resolve().parent.parent / "kestrel"
    offenders = []
    for py in root.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if re.match(
                r"^(import|from)\s+(win11toast|pythonnet|clr|winreg|webview|pystray)\b", line
            ):
                offenders.append(f"{py.relative_to(root)}: {line}")
    assert offenders == []


# ------------------------------------------------------------------ the desktop shell downloads
def test_desktop_window_allows_downloads(shell, monkeypatch):
    """ "Download diagnostics" flashed a window and closed it: pywebview cancels every download
    until ALLOW_DOWNLOADS is on. The shell turns it on before the window is created."""

    class _Handler:
        fn = None

        def __iadd__(self, fn):
            self.fn = fn
            return self

    class _Events:
        closing = _Handler()

    class _Window:
        events = _Events()

    class _FakeWebview:
        settings = {"ALLOW_DOWNLOADS": False}

        @staticmethod
        def initialize() -> None:
            pass

        @staticmethod
        def create_window(*a, **k) -> _Window:
            assert _FakeWebview.settings["ALLOW_DOWNLOADS"] is True, "set before the window"
            return _Window()

        @staticmethod
        def start() -> None:
            pass

    class _FakeThread:
        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    def _start_server() -> int:
        shell.server, shell.server_thread = object(), _FakeThread()
        return 8765

    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    monkeypatch.setattr(shell, "_configure_logging", lambda: None)
    monkeypatch.setattr(shell, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(shell, "start_server", _start_server)
    monkeypatch.setattr(shell, "run_preflight", lambda: None)
    monkeypatch.setattr(shell, "start_tray", lambda **_k: False)
    monkeypatch.setattr("kestrel.db.migrate.run_migrations", lambda *a, **k: None)
    monkeypatch.setattr("kestrel.web.serve.wait_for_health", lambda port, timeout=30.0: True)
    monkeypatch.setitem(sys.modules, "webview", _FakeWebview)

    assert shell.run_desktop() == 0
    assert _FakeWebview.settings["ALLOW_DOWNLOADS"] is True


def test_the_installed_windows_app_declares_its_identity(shell, monkeypatch):
    # Toasts read "Kestrel" with the shortcut's icon only when the process and the installer's
    # shortcut carry the same AppUserModelID (an old build's icon otherwise sticks to Kestrel.exe).
    import types

    calls = []
    shell32 = types.SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=lambda aumid: calls.append(aumid) or 0
    )
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell.paths, "is_frozen", lambda: True)
    monkeypatch.setattr("ctypes.windll", types.SimpleNamespace(shell32=shell32), raising=False)
    assert shell.set_app_identity() is True
    assert calls == [shell.APP_USER_MODEL_ID]


def test_from_source_or_off_windows_no_identity_is_declared(shell, monkeypatch):
    # From source no shortcut carries the identity, and Windows drops toasts it cannot resolve.
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell.paths, "is_frozen", lambda: False)
    assert shell.set_app_identity() is False
    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    monkeypatch.setattr(shell.paths, "is_frozen", lambda: True)
    assert shell.set_app_identity() is False


def test_the_installed_app_toasts_under_its_own_identity(monkeypatch):
    import sys
    import types

    from kestrel.desktop.shell import APP_USER_MODEL_ID
    from kestrel.services import delivery

    sent = []
    fake = types.SimpleNamespace(notify=lambda *a, **kw: sent.append(kw.get("app_id")))
    monkeypatch.setitem(sys.modules, "win11toast", fake)
    monkeypatch.setattr(delivery.sys, "platform", "win32")
    monkeypatch.setattr("kestrel.paths.is_frozen", lambda: True)
    assert delivery._toast("Kestrel", "body") is True
    monkeypatch.setattr("kestrel.paths.is_frozen", lambda: False)
    assert delivery._toast("Kestrel", "body") is True
    assert sent == [APP_USER_MODEL_ID, None]
