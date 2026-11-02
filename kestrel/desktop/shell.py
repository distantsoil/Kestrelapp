"""The desktop shell process model (SPEC-INTERFACES 1.1, transcribed from spike/FINDINGS.md).

Startup order (each a numbered trap from the spec / a FINDINGS correction):
  freeze_support + stdout/stderr repair happen in __main__ BEFORE this module's main() runs.
  1  Local\\ single-instance mutex; a second instance POSTs /api/focus and exits 0 (F5).
     On macOS and Linux the same contract is an exclusive flock on <data_home>/kestrel.lock.
  2  resolve paths + seed config + configure logging.
  3  programmatic Alembic on a sync sqlite:// URL (migrate.run_migrations).
  4  uvicorn on a deterministic port ladder (8765..8774), falling back
   to OS-assigned; off the main thread, no signal handlers (F5).
  5  worker + scheduler from the FastAPI lifespan, on uvicorn's loop (build_app).
  6  wait for /health, then write runtime.json.
  7  preflight (doctor); the tray thread with live tooltip + full menu + icon states.
  8  webview.create_window + the closing handler (F1 _quitting guard), then webview.start() last.

Quit is GRACEFUL (F8): it flips _quitting, stops the scheduler+worker via server.should_exit (whose
lifespan shutdown waits up to 30s for in-flight tasks and RELEASES LEASES), stops the tray, and only
then destroys the window. It never hard-kills mid-drain; only a server still running after the drain
window is ended, so a Quit always leaves no process behind (D-UI-17).

Windows is the packaged, double-clickable product and its path is unchanged. On macOS and Linux
this module imports cleanly (the kernel32 bits are loaded inside the functions that need them) and,
when pywebview has no backend to show a window, falls back to the default browser with the server
kept in the foreground until Ctrl+C."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from kestrel import __version__, paths

log = structlog.get_logger()

IS_WINDOWS = sys.platform == "win32"
# The Windows app identity (AppUserModelID). The installer stamps the same value on its shortcuts
# (packaging/kestrel.iss), so toasts read "Kestrel" with the shortcut's current icon instead of
# "Kestrel.exe" with whatever icon Windows recorded the first time an old build notified.
APP_USER_MODEL_ID = "Kestrel.Desktop"
IS_MACOS = sys.platform == "darwin"

# ------------------------------------------------------------------- module-scope singletons
_mutex_handle = None  # never closed: closing the handle would release the single-instance lock
_lock_file = None  # the flock'd file object on macOS/Linux; held open for the process lifetime
window = None
icon = None
server = None
server_thread: threading.Thread | None = None
_quitting = False  # F1: window.destroy() re-fires `closing`; the handler allows it when quitting
_toast_shown = False
_port = 0
_last_status: dict[str, Any] = {}  # the poller's last /api/status, for the macOS menu (no HTTP)
# True when the tray's own run() is the main loop (the macOS browser fallback, where there is no
# window loop to join). Then a quit must stop() the icon to return the main thread; with a window
# the icon shares pywebview's loop and stop() would end that loop under the window instead.
_tray_owns_loop = False

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\Kestrel"  # Local\ not Global\ (Global needs a privilege, blocks a 2nd user)
LOCK_FILENAME = "kestrel.lock"  # macOS/Linux: flock'd under data_home(), the same contract


def _normalized(path: Path) -> str:
    """One spelling per real folder: resolve() follows symlinks, junctions and 8.3 short names
    (strict=False, so a folder not yet created still works), then normcase folds case on Windows.
    Mutex names are case-sensitive, so two cases of one Windows path must not split."""
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _mutex_name() -> str:
    """Return the Windows mutex name for the current data home.

    The default data home (no KESTREL_DATA_HOME, or KESTREL_DATA_HOME naming the default folder
    under any spelling) uses exactly MUTEX_NAME, the installer's AppMutex. Any other folder gets
    Local\\Kestrel-<8 hex> from a hash of its normalized real path, so two folders never share a
    lock and two spellings of one folder never get two.
    """
    current = _normalized(paths.data_home())
    if current == _normalized(paths.default_data_home()):
        return MUTEX_NAME
    return f"Local\\Kestrel-{hashlib.sha256(current.encode('utf-8')).hexdigest()[:8]}"


def _lock_file_name() -> str:
    """macOS/Linux: always LOCK_FILENAME. The file lives inside the data home, so it is already
    per folder, and flock on the same inode holds across spellings of the path. A hashed name
    would also let an older build (which uses kestrel.lock) and this one both get a lock."""
    return LOCK_FILENAME


# --------------------------------------------------------------------------- mutex (trap 1)
_focused = False  # whether the running instance answered this launch's focus request


def focus_running_instance() -> bool:
    """A lock only DETECTS; focus is a POST to the running instance found via runtime.json.
    True when it answered; False means the other copy is gone or still closing (D-UI-89)."""
    global _focused
    _focused = False
    try:
        info = json.loads(paths.runtime_json().read_text(encoding="utf-8"))
        import httpx

        r = httpx.post(f"http://127.0.0.1:{info['port']}/api/focus", timeout=5.0)
        # {"focused": false} means the server is up but its window handler is not registered
        # yet (still loading); only a true answer is a focus.
        _focused = r.status_code == 200 and bool(r.json().get("focused"))
        log.info("focused_existing_instance", port=info["port"], ok=_focused)
    except Exception as exc:  # a second launch must never crash; it just reports
        log.warning("focus_request_failed", error=str(exc))
    return _focused


def _acquire_windows_mutex() -> bool:
    import ctypes
    from ctypes import wintypes

    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex_handle = kernel32.CreateMutexW(None, False, _mutex_name())
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def lock_file_path() -> Path:
    return paths.data_home() / _lock_file_name()


def _acquire_lock_file() -> bool:
    """macOS/Linux: an exclusive, non-blocking flock. flock is per open file description, so even
    a second open() in the same process is refused, and the kernel drops it when the process dies
    (no stale-pid problem, no cleanup needed on a crash)."""
    import fcntl

    global _lock_file
    paths.ensure_dirs()
    f = open(lock_file_path(), "a+", encoding="utf-8")  # held open on purpose, not scoped
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_file = f
    return True


_have_lock = False  # this process holds the single-instance lock


def acquire_single_instance() -> bool:
    """True iff we are the first instance. Otherwise focus the running instance and return False."""
    global _have_lock
    first = _acquire_windows_mutex() if IS_WINDOWS else _acquire_lock_file()
    _have_lock = first
    if not first and "--tray" not in sys.argv[1:]:
        # A quiet start by the start-with-my-computer entry must not raise a window that is
        # already open (or one the person closed to the tray on purpose).
        focus_running_instance()
    return first


def release_single_instance() -> None:
    """Give the single-instance lock back on a FAILED start.

    The mutex is normally held until the process dies, which is what a clean quit relies on. But a
    start that never reached a healthy server used to return while the non-daemon uvicorn thread
    kept the process, and therefore the mutex, alive: every later launch decided Kestrel was
    already running, POSTed /api/focus at a server that never came up, and exited. Releasing here
    is what lets the next double-click start fresh."""
    global _mutex_handle, _lock_file, _have_lock
    _have_lock = False
    if _mutex_handle is not None:
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(_mutex_handle)
            kernel32.CloseHandle(_mutex_handle)
        except Exception as exc:  # best-effort on a path that is already failing
            log.warning("mutex_release_failed", error=str(exc))
        _mutex_handle = None
    if _lock_file is not None:
        try:
            import fcntl

            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
            _lock_file.close()
        except Exception as exc:
            log.warning("lock_file_release_failed", error=str(exc))
        _lock_file = None


# -------------------------------------------------------------------- server thread (traps 4-6)
def start_server() -> int:
    from kestrel.web.app import build_app
    from kestrel.web.serve import _make_server, bind_free_port

    global server, server_thread, _port
    sock, port = bind_free_port()
    _port = port
    # worker AND scheduler from the lifespan, on uvicorn's single loop (never a second loop).
    server = _make_server(build_app(worker=True, scheduler=True), log_level="info")
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn")
    server_thread.start()
    return port


# ------------------------------------------------------------------------- window ops (trap 8)
def hide_window() -> None:
    global _hidden
    _save_geometry()
    if window is not None:
        window.hide()
        _hidden = True
    log.info("window_hidden_worker_continues")


_hidden = False  # the window is hidden to the tray


def window_visible() -> bool:
    return window is not None and not _hidden


def show_window() -> None:
    global _hidden
    if window is not None:
        window.show()
        _hidden = False
        try:
            window.restore()
        except Exception:  # not every backend implements restore(); hide/show is the contract
            pass


def on_closing():
    """closing event: hide to tray and cancel the OS close (return False). F1: window.destroy() on
    quit re-fires this event, so when _quitting is set we ALLOW the close (return True), otherwise
    the app could never exit. With close-to-tray off (D-UI-8), or with no tray to come back
    from, closing the window is a real quit: the same graceful path as the tray's Quit, minus the
    destroy(), since the OS close this handler allows does that."""
    global _toast_shown
    _save_geometry()
    if _quitting:
        return True
    if not _started and _frame_api is not None:
        # Closed on the loading page (or its problem card): there is nothing to keep running.
        _begin_quit()
        return True
    from kestrel.web.deps import close_to_tray

    if icon is None or not close_to_tray():
        _begin_quit()
        if window is not None and _started and window_visible():
            # A real quit from the OS close: keep the window up with the closing screen while
            # the server stops (D-UI-90), then close it from there.
            threading.Thread(target=_close_with_overlay, name="closing", daemon=True).start()
            return False
        return True
    if not _toast_shown:
        # Once per launch, not once ever (D-UI-16): a balloon is easy to miss, and the first
        # close of each session is when "where did it go?" is asked.
        _toast_shown = True
        _notify(CLOSE_TO_TRAY_TITLE, CLOSE_TO_TRAY_BODY)
    hide_window()
    return False


CLOSE_TO_TRAY_TITLE = "Kestrel is still running"
CLOSE_TO_TRAY_BODY = (
    "Kestrel keeps working in the background while the window is closed. Open it again from "
    "this tray icon. To quit on close instead, go to Settings > This computer > Everyday options."
)


def _on_tray_thread(fn: Callable[[], None]) -> None:
    """Run a change to the tray icon where its toolkit allows it. On macOS every AppKit call must
    happen on the main thread, which pywebview's Cocoa loop owns; macOS 27 kills the app outright
    on a status-item change from any other thread. So there the call is queued onto the main run
    loop (or run at once when already on it). Elsewhere pystray is thread-safe and the call runs
    at once."""
    if IS_MACOS and threading.current_thread() is not threading.main_thread():
        from PyObjCTools import AppHelper

        AppHelper.callAfter(fn)
    else:
        fn()


def _menu_action(fn: Callable[[], None]) -> Callable[[Any, Any], None]:
    """A tray menu callback. On macOS AppKit calls it on the main thread, where a blocking call
    freezes the window, and window.evaluate_js waits on that same thread forever. So there it
    runs on a short-lived thread, as every callback did before the tray joined the main loop."""

    def handler(_icon: Any, _item: Any) -> None:
        if IS_MACOS:
            threading.Thread(target=fn, name="tray-action", daemon=True).start()
        else:
            fn()

    return handler


def _notify(title: str, body: str) -> None:
    if icon is not None:
        try:
            icon.notify(body, title)
            log.info("tray_notified", title=title)
        except Exception as exc:  # notifications are best-effort; never break the flow
            # Warning, not debug: the diagnostics bundle must show why a notice never appeared.
            log.warning("tray_notify_failed", error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- graceful quit (F8)
def _begin_quit() -> None:
    """Stop the server (graceful lifespan drain) and the tray; leaves the window to the caller."""
    global _quitting
    _quitting = True
    log.info("quit_requested")
    _mark_closing()
    if server is not None:
        # server.should_exit triggers uvicorn's shutdown, which runs the lifespan finally: it
        # signals the worker, waits up to 30s for in-flight tasks, and releases the leases (F8).
        server.should_exit = True
    if icon is not None:
        try:
            if IS_MACOS and not _tray_owns_loop:
                # The icon shares pywebview's NSApplication, and pystray's stop() would stop that
                # loop under the window. Hide the item; it goes with the process.
                _on_tray_thread(lambda: setattr(icon, "visible", False))
            else:
                # The icon's own loop (its thread, or the main thread in the browser fallback):
                # stop() ends it and, in the fallback, hands the main thread back to _finish_quit.
                icon.stop()
        except Exception as exc:
            log.debug("tray_stop_failed", error=str(exc))


def do_quit() -> None:
    """The one place a real quit happens. Never a hard kill mid-drain; only after the drain window
    has passed does _finish_quit end a process whose server will not stop (D-UI-17)."""
    if _quitting:
        return
    _begin_quit()
    if window is not None:
        _close_with_overlay()


CLOSING_MIN_S = 0.6  # the closing screen stays at least this long, so it reads as a step


def _close_with_overlay() -> None:
    """The closing screen (D-UI-90): shown over the app while the server stops (the worker is
    stopped within a second and its leases released), then the window goes. The window never
    vanishes before shutdown is done; QUIT_JOIN_S is only the hard ceiling."""
    started = time.monotonic()
    if window is not None and _started and window_visible():
        with __import__("contextlib").suppress(Exception):
            window.evaluate_js("window.KUI && KUI.frame.closing()")
    if server_thread is not None:
        server_thread.join(timeout=QUIT_JOIN_S)
    rest = CLOSING_MIN_S - (time.monotonic() - started)
    if rest > 0 and window_visible():
        time.sleep(rest)
    if window is not None:
        window.destroy()  # the only destroy() in the program; unblocks webview.start()


# ------------------------------------------------------------------- control helpers (loopback)
def _post(path: str) -> None:
    try:
        import httpx

        httpx.post(f"http://127.0.0.1:{_port}{path}", timeout=5.0)
    except Exception as exc:
        log.warning("control_post_failed", path=path, error=str(exc))


def _status() -> dict:
    try:
        import httpx

        return httpx.get(f"http://127.0.0.1:{_port}/api/status", timeout=3.0).json()
    except Exception:
        return {}


def _fault(exc: BaseException) -> dict[str, Any]:
    """What a log line may say about a failure: the exception type and its numeric codes. Never
    the exception text or arguments, because an OSError's text carries the file name."""
    return {"error_type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None)}


def _open_folder(p: Path) -> bool:
    """Open the operator's own data/log folder in the OS file manager. Returns whether the
    request worked; a failure is logged here (type and code only) and reported to the caller."""
    try:
        if IS_WINDOWS:
            os.startfile(str(p))  # noqa: S606
            return True
        return _run_opener(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    except OSError as exc:
        log.warning("open_folder_failed", **_fault(exc))
        return False


def _open_folder_async(p: Path) -> None:
    """Open a folder from the tray without holding the tray thread: the opener can wait on the
    file manager for several seconds."""
    threading.Thread(target=_open_folder, args=(p,), name="open-folder", daemon=True).start()


def _run_opener(argv: list[str]) -> bool:
    """Run `open` (macOS) or `xdg-open` (Linux) and report its exit status. Both hand the request
    to the file manager and return, so waiting is brief; a non-zero exit, a timeout or a missing
    command is a failure to report, logged without the path."""
    try:
        done = subprocess.run(  # noqa: S603
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        if argv[0] == "xdg-open":
            # xdg-open stays in the foreground while a file manager it started is running: the
            # folder is open, so this is a success, not a failure.
            log.info("open_command_still_running", command=argv[0])
            return True
        log.warning("open_command_failed", command=argv[0], reason="timeout")
        return False
    except OSError as exc:
        log.warning("open_command_failed", command=argv[0], **_fault(exc))
        return False
    if done.returncode != 0:
        log.warning("open_command_failed", command=argv[0], exit_code=done.returncode)
        return False
    return True


_win_dlls: tuple[Any, Any] | None = None


def _windows_dlls() -> tuple[Any, Any]:
    """Private (shell32, ole32) library instances with their signatures set, created once. They
    are not `ctypes.windll`'s shared ones, so setting argtypes here cannot change how any other
    code in the process calls the same functions."""
    global _win_dlls
    if _win_dlls is None:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32")  # type: ignore[attr-defined]  # Windows only
        ole32 = ctypes.WinDLL("ole32")  # type: ignore[attr-defined]
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p  # a pointer: 64-bit on x64
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.ILFree.restype = None
        _win_dlls = (shell32, ole32)
    return _win_dlls


def _select_in_explorer(p: Path) -> bool:
    """Open the file's folder in Explorer with the file selected, through the shell API
    (SHOpenFolderAndSelectItems) rather than `explorer /select,` argument parsing, which opened a
    blank window that closed itself (#100). Returns whether the call succeeded. COM is initialised
    for this thread (the web server's, not the UI thread) and released again."""
    shell32, ole32 = _windows_dlls()
    # S_OK (0) and S_FALSE (1) both need a matching CoUninitialize; a thread already in another
    # COM mode (RPC_E_CHANGED_MODE, negative) is usable as it is and must not be uninitialised.
    hr_init = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    pidl = None
    try:
        pidl = shell32.ILCreateFromPathW(str(p))
        if not pidl:
            return False
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) >= 0
    finally:
        if pidl:
            shell32.ILFree(pidl)
        if hr_init in (0, 1):
            ole32.CoUninitialize()


def reveal_file(p: Path) -> bool:
    """Show a file Kestrel just saved for the operator (the diagnostics zip, D-UI-41): the file
    manager opens its folder with the file selected. Returns whether that worked, so the page
    reports "Opened" only when it did (#100). On Windows the shell API selects the file; if that
    fails the folder is opened instead, and only when both fail is the result False. Failures are
    logged with the error type and code, never the text or a path. macOS selects the file in
    Finder; elsewhere the folder opens."""
    if IS_WINDOWS:
        import ctypes

        try:
            if _select_in_explorer(p):
                return True
            log.warning("reveal_file_select_failed")
        except (OSError, AttributeError, ValueError, ctypes.ArgumentError) as exc:
            log.warning("reveal_file_select_failed", **_fault(exc))
        try:
            os.startfile(str(p.parent))  # noqa: S606
            return True
        except OSError as exc:
            log.warning("reveal_file_failed", **_fault(exc))
            return False
    if sys.platform == "darwin":
        return _run_opener(["open", "-R", str(p)])
    return _open_folder(p.parent)


# ------------------------------------------------------------------------------ tray (trap 7)
def _icons() -> dict:
    """The three tray states, from the shipped mark.

    Prefers the rasterized mark in kestrel/resources/icons (generated from docs/images by
    scripts/make_icons.py) so the tray matches the installer, the taskbar, and the browser tab.
    Falls back to the drawn placeholder if those are missing, because a tray icon failing to load
    must not stop the app from starting.
    """
    from PIL import Image, ImageDraw

    try:
        from kestrel import paths

        icons_dir = paths.packaged_icons()
        loaded = {}
        for state in ("idle", "working", "working_att"):
            with paths.resource_path(icons_dir / f"tray-{state}.png") as p:
                loaded[state] = Image.open(p).convert("RGBA")
        return loaded
    except Exception as exc:  # a missing or unreadable icon is cosmetic, never fatal
        log.warning("tray_icons_fallback", error=str(exc))

    def _mk(fill: bool, accent: str | None) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill="#4F5BD5" if fill else None, outline="#4F5BD5", width=6)
        if not fill:
            d.ellipse((22, 22, 42, 42), fill="#4F5BD5")
        if accent:
            d.ellipse((40, 6, 58, 24), fill=accent)
        return img

    return {
        "idle": _mk(False, None),
        "working": _mk(True, None),
        "working_att": _mk(True, "#b3261e"),
    }


def _tooltip(status: dict) -> tuple[str, str]:
    """(tooltip text, icon state) from /api/status. Shows live run progress when a run is active."""
    act = status.get("activity") or {}
    run = act.get("run") or act.get("active_run") or {}
    done, total = run.get("done"), run.get("total")
    if status.get("paused"):
        return f"Kestrel {__version__} - paused", "idle"
    if run and total:
        rate = run.get("per_minute") or run.get("rate")
        eta = run.get("eta_minutes") or run.get("eta")
        tip = f"Kestrel {__version__} - run: {done}/{total}"
        if rate:
            tip += f", {rate}/min"
        if eta:
            tip += f", ~{eta}m left"
        state = "working_att" if act.get("errors") or act.get("quarantined") else "working"
        return tip, state
    return f"Kestrel {__version__} - idle", "idle"


_update_version: str | None = None  # set by the poller; drives the tray's "Update available" item
_UPDATE_POLL_EVERY = 15  # tooltip ticks (4 s each): the update state changes at most daily


def _poll_update_status() -> None:
    """Refresh the cached update version from the local API and redraw the menu on a change."""
    global _update_version
    try:
        import httpx

        resp = httpx.get(f"http://127.0.0.1:{_port}/api/updates/status", timeout=3.0)
        data = resp.json() if resp.status_code == 200 else None
    except Exception:
        return  # keep the last known value; the next poll retries
    if not isinstance(data, dict) or "available" not in data:
        return  # an error page or unexpected body is not an answer; keep the last value
    version = data.get("version") if data["available"] else None
    if version != _update_version:
        _update_version = version
        if icon is not None:
            _on_tray_thread(icon.update_menu)


def _show_update_dialog() -> None:
    """Bring the window forward and open the release-notes dialog the banner's button opens."""
    show_window()
    if window is not None:
        try:
            window.evaluate_js(
                "htmx.ajax('GET','/updates/details',{target:'#update-modal',swap:'innerHTML'})"
            )
        except Exception as exc:  # the window may not have a page yet; Open still worked
            log.debug("update_dialog_failed", error=str(exc))


def _tooltip_poller() -> None:
    global _last_status
    imgs = _icons()
    tick = 0
    while not _quitting:
        if tick % _UPDATE_POLL_EVERY == 0:
            _poll_update_status()
        tick += 1
        try:
            status = _status()
            # One rebinding, never clear() then update(): the macOS menu reads this dict on the
            # main thread and must never see it half filled.
            paused_changed = bool(status.get("paused")) != bool(_last_status.get("paused"))
            _last_status = status
            tip, state = _tooltip(status)
            if icon is not None:
                image = imgs.get(state, imgs["idle"])

                def _apply(tip=tip, image=image) -> None:
                    icon.title = tip
                    icon.icon = image

                _on_tray_thread(_apply)
                if paused_changed:
                    # The "Pause worker" check is read when the menu is built, and on macOS the
                    # menu is built once (before this poller has an answer) and again only on
                    # update_menu. Without this a paused worker shows as running.
                    _on_tray_thread(icon.update_menu)
        except Exception as exc:
            log.debug("tooltip_poll_error", error=str(exc))
        time.sleep(4.0)


# D-UI-87 (SPEC-INTERFACES 1.7): one fixed design size, never a percentage of the screen.
DESIGN_SIZE = (1680, 1000)
MAX_SIZE = (1710, 1107)  # a 15-inch laptop's default scaled resolution: never larger
MIN_SIZE = (960, 640)
FALLBACK_SIZE = (1440, 900)  # when the screen size is unknown
# Kept clear on every side of the work area (logical pixels): 40 px of desktop around the window.
WORK_MARGIN = 40
# When only the whole screen is known: the margin plus a typical taskbar or dock (48 px).
SCREEN_MARGIN = (WORK_MARGIN, WORK_MARGIN + 48)


def _work_area(first: Any) -> tuple[int, int] | None:
    """The usable area of a pywebview screen in logical pixels (the screen less the taskbar or
    dock), from the native screen object it carries; None when the backend does not say."""
    frame_obj = getattr(first, "frame", None)
    scale = float(getattr(first, "scale", 1.0) or 1.0)
    try:
        if IS_WINDOWS and frame_obj is not None:  # System.Windows.Forms.Screen, physical pixels
            area = frame_obj.WorkingArea
            return int(area.Width / scale), int(area.Height / scale)
        if IS_MACOS and frame_obj is not None:  # NSScreen, points
            vis = frame_obj.visibleFrame()
            return int(vis.size.width), int(vis.size.height)
    except Exception as exc:  # the screen size alone still gives a sensible size
        log.debug("work_area_unknown", error=str(exc))
    return None


def _initial_size(
    screen: tuple[int, int] | None = None, work: tuple[int, int] | None = None
) -> tuple[int, int]:
    """The window opens at 1680x1000 logical pixels, not maximized and not a share of the screen
    (D-UI-87): an ultrawide gets the same window as a laptop. Each side is clamped on its own to
    the usable area less WORK_MARGIN (the work area when known, else the screen less a typical
    taskbar), so a short screen never narrows the window. The minimum stays 960x640; with no
    screen size known, 1440x900. pywebview reports screens in logical pixels."""
    if screen is None:
        try:
            import webview

            first = webview.screens[0]
            screen = (int(first.width), int(first.height))
            work = work or _work_area(first)
        except Exception:  # no backend yet, or no screen: the fallback size
            return FALLBACK_SIZE
    if work is not None:
        usable_w, usable_h = work[0] - WORK_MARGIN, work[1] - WORK_MARGIN
    else:
        usable_w, usable_h = screen[0] - SCREEN_MARGIN[0], screen[1] - SCREEN_MARGIN[1]
    w = max(MIN_SIZE[0], min(DESIGN_SIZE[0], MAX_SIZE[0], usable_w))
    h = max(MIN_SIZE[1], min(DESIGN_SIZE[1], MAX_SIZE[1], usable_h))
    return w, h


# ------------------------------------------------------- the first frame's color (D-UI-89)
def _token(name: str, dark: bool) -> str | None:
    """A color from tokens.css (light block, or the pinned dark block), so the window's first
    frame uses the theme's own value rather than a second copy of it."""
    import re

    css = Path(paths.web_static_dir(), "css", "tokens.css").read_text(encoding="utf-8")
    marker = ':root:root:root[data-theme="dark"] {' if dark else ":root:root:root {"
    block = css.split(marker, 1)[1].split("}", 1)[0] if marker in css else ""
    m = re.search(rf"--{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})", block)
    return m.group(1) if m else None


def _os_dark() -> bool:
    """The operating system's own light or dark setting, when it can be read."""
    try:
        if IS_WINDOWS:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if IS_MACOS:
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            return out.stdout.strip().lower() == "dark"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("os_theme_unknown", error=str(exc))
    return False


def _saved_theme() -> str:
    """'light', 'dark' or 'auto': the theme the app's window last chose (WindowApi records it)."""
    try:
        data = json.loads(paths.window_state_json().read_text(encoding="utf-8"))
        theme = data.get("theme") if isinstance(data, dict) else None
        return theme if theme in ("light", "dark", "auto") else "auto"
    except (OSError, ValueError):
        return "auto"


def _window_state() -> dict[str, Any]:
    try:
        data = json.loads(paths.window_state_json().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_window_state(**values: Any) -> None:
    """Merge into window.json (theme and geometry share it); local only, never sent anywhere."""
    state = _window_state()
    state.update(values)
    try:
        paths.ensure_dirs()
        paths.window_state_json().write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log.warning("window_state_save_failed", error=str(exc))


def _remember_theme(theme: str) -> None:
    if theme in ("light", "dark", "auto"):
        _update_window_state(theme=theme)


# ------------------------------------------------- the window's last place (D-UI-195)
Rect = tuple[int, int, int, int]  # x, y, width, height (pywebview's logical pixels)


def _fits(rect: Rect, area: Rect) -> bool:
    x, y, w, h = rect
    ax, ay, aw, ah = area
    return ax <= x and ay <= y and x + w <= ax + aw and y + h <= ay + ah


def _restore_geometry(saved: Any, areas: list[Rect]) -> tuple[Rect, bool] | None:
    """The saved rectangle and maximized flag when that rectangle still lies wholly inside one
    connected display's work area (and is no smaller than the minimum); None otherwise (first
    launch, the display unplugged, a lower resolution), so the D-UI-87 default size is used."""
    if not isinstance(saved, dict):
        return None
    try:
        rect = tuple(int(saved[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if rect[2] < MIN_SIZE[0] or rect[3] < MIN_SIZE[1]:
        return None
    if not any(_fits(rect, a) for a in areas):  # type: ignore[arg-type]
        return None
    return rect, bool(saved.get("maximized"))  # type: ignore[return-value]


def _screen_areas() -> list[Rect]:
    """Each connected display's work area (the screen when the backend gives no work area)."""
    areas: list[Rect] = []
    try:
        import webview

        for sc in webview.screens:
            x, y = int(getattr(sc, "x", 0) or 0), int(getattr(sc, "y", 0) or 0)
            work = _work_area(sc)
            w, h = work or (int(sc.width), int(sc.height))
            areas.append((x, y, w, h))
    except Exception as exc:  # no screens known: never restore blind
        log.debug("screens_unknown", error=str(exc))
    return areas


_geom_timer: threading.Timer | None = None
GEOMETRY_DEBOUNCE_S = 1.0


def _save_geometry() -> None:
    """Record where the window is. While maximized only the flag changes, so the normal rectangle
    a restore returns to is the one kept. Best effort."""
    if window is None or not _started:
        return
    try:
        maximized = _frame_api._is_maximized() if _frame_api is not None else False
        geom = dict(_window_state().get("geometry") or {})
        if not maximized and not _minimized:
            geom.update(x=int(window.x), y=int(window.y),
                        width=int(window.width), height=int(window.height))
        if not _minimized:
            geom["maximized"] = bool(maximized)
        if {"x", "y", "width", "height"} <= geom.keys():
            _update_window_state(geometry=geom)
    except Exception as exc:  # the window keeps working; next launch uses the default
        log.warning("window_geometry_save_failed", error=f"{type(exc).__name__}: {exc}")


def _geometry_changed(*_args: Any) -> None:
    """moved and resized events: save once the window has settled."""
    global _geom_timer
    if _geom_timer is not None:
        _geom_timer.cancel()
    _geom_timer = threading.Timer(GEOMETRY_DEBOUNCE_S, _save_geometry)
    _geom_timer.daemon = True
    _geom_timer.start()


def _launch_theme() -> tuple[str, bool]:
    """(the saved choice, whether the first frame is dark)."""
    theme = _saved_theme()
    return theme, (theme == "dark" or (theme == "auto" and _os_dark()))


def start_tray(*, with_window: bool = True) -> bool:
    """Start the tray icon. False (with a log line, never a crash) when this machine cannot host
    one: pystray's import on Linux needs an X display or an appindicator/GTK stack, and a headless
    session or an SSH shell has neither. Kestrel runs without a tray, and `icon` stays None so a
    window close is a real quit (on_closing) rather than a hide to an item that is not there.
    On macOS the icon joins the window's Cocoa loop when there is one; without a window (the
    browser fallback) the icon's own run() becomes the main loop, which the caller enters, and
    _tray_owns_loop records that a quit must stop() it."""
    global icon
    try:
        import pystray
    except Exception as exc:  # Xlib raises DisplayNameError, not ImportError, with no DISPLAY
        log.warning("tray_unavailable", error=f"{type(exc).__name__}: {exc}")
        return False
    try:
        _build_tray(pystray, with_window=with_window)
    except Exception as exc:  # a backend that imports but cannot create or show an icon
        icon = None  # never a half-started icon that on_closing would hide the window behind
        log.warning("tray_unavailable", error=f"{type(exc).__name__}: {exc}")
        return False
    return True


def _build_tray(pystray: Any, *, with_window: bool) -> None:
    imgs = _icons()

    def _paused_checked(item: Any) -> bool:
        # On macOS the menu is built on the main thread: read the poller's last answer, no HTTP.
        status = _last_status if IS_MACOS else _status()
        return bool(status.get("paused"))

    global icon, _tray_owns_loop
    new_icon = pystray.Icon(
        "kestrel",
        imgs["idle"],
        f"Kestrel {__version__}",
        menu=pystray.Menu(
            pystray.MenuItem("Open", _menu_action(lambda: show_window()), default=True),
            pystray.MenuItem(
                lambda item: f"Update available ({_update_version})",
                _menu_action(lambda: _show_update_dialog()),
                visible=lambda item: _update_version is not None,
            ),
            pystray.MenuItem(
                "Pause worker",
                _menu_action(lambda: _post("/api/worker/pause")),
                checked=_paused_checked,
            ),
            pystray.MenuItem("Resume worker", _menu_action(lambda: _post("/api/worker/resume"))),
            pystray.MenuItem("Run scan now", _menu_action(lambda: _post("/api/scan/now"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Recent notifications", _menu_action(lambda: show_window())),
            pystray.MenuItem(
                "Open data folder", _menu_action(lambda: _open_folder_async(paths.data_dir()))
            ),
            pystray.MenuItem(
                "Open logs", _menu_action(lambda: _open_folder_async(paths.logs_dir()))
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _menu_action(lambda: do_quit())),
        ),
    )
    if IS_MACOS and with_window:
        # pystray's run() would start a second NSApplication loop on this thread and build the
        # menu off the main thread, which macOS 27 kills at launch. run_detached() joins the loop
        # pywebview runs; this is called on the main thread, so the menu and the item are built
        # here, before webview.start().
        new_icon.run_detached(setup=lambda _icon: None)
        new_icon.visible = True
    elif IS_MACOS:
        # No window loop to join: the icon's own run() is the main loop. _run_in_browser enters
        # it on the main thread (run_tray_loop), so nothing starts here.
        _tray_owns_loop = True
    else:
        threading.Thread(target=new_icon.run, name="tray", daemon=True).start()
    # Published only once it is showing (or ready to run): a failure above leaves `icon` None.
    icon = new_icon
    threading.Thread(target=_tooltip_poller, name="tray-tooltip", daemon=True).start()


def run_tray_loop() -> None:
    """Run the tray's own loop on the calling (main) thread until a quit stops it. Only when
    _tray_owns_loop: the macOS browser fallback, where AppKit needs the main thread and there is
    no window loop to share. The item is shown here, on this thread, before the loop starts, the
    same order as the window path (run_detached then visible, then the loop). pystray's loop
    installs its own Ctrl+C handler (NSApp terminate), so on this path Ctrl+C ends the process at
    once rather than through the drain; the tray's Quit and the in-app Quit button are the
    graceful paths, and an expired lease is picked up at the next launch (hard rule 5)."""
    if icon is None or not _tray_owns_loop or _quitting:
        return
    icon.visible = True
    # pystray's stop() is a no-op until the loop is running, so a quit that lands before run()
    # would leave it blocked; setup runs once the loop is live and stops it then.
    icon.run(setup=lambda i: i.stop() if _quitting else None)  # blocks until icon.stop()


# --------------------------------------------------------------------------- preflight (trap 7)
def run_preflight() -> None:
    """Run the SPEC-INTERFACES 1.2 checks (doctor). A blocking failure is logged and surfaced in the
    UI (/about and, on first run, the operator opens the profile/sources editors); the app still
    starts so the operator can fix things from within it rather than being locked out."""
    try:
        from kestrel import doctor

        report = doctor.run_doctor(quick=True)
        log.info("preflight", blocking=report.has_blocking_failure)
        if report.has_blocking_failure:
            log.warning("preflight_blocking", report=doctor.format_report(report)[:2000])
    except Exception as exc:  # preflight is advisory; never block launch on it
        log.warning("preflight_error", error=str(exc))


# ------------------------------------------------------------------------------------ main
def abort_start(reason: str) -> int:
    """Tear down a start that failed before the window existed, and return the exit code.

    Stops the server thread (which also runs the lifespan shutdown and releases leases), waits for
    it, and releases the single-instance mutex so the next launch is not turned away as a
    duplicate. Returning without doing this left a headless, windowless process holding the lock."""
    log.error("desktop_start_aborted", reason=reason)
    if server is not None:
        server.should_exit = True
    if server_thread is not None:
        server_thread.join(timeout=35.0)
        if server_thread.is_alive():
            log.warning("server_thread_still_alive_after_abort")
    release_single_instance()
    with __import__("contextlib").suppress(OSError):
        paths.runtime_json().unlink()
    return 1


def set_app_identity() -> bool:
    """Declare Kestrel's Windows app identity for this process, before any window, tray icon or
    toast exists. Only the installed app: from source there is no shortcut carrying the identity,
    and Windows drops toasts for an identity it cannot resolve. Returns True when it was set."""
    if not (IS_WINDOWS and paths.is_frozen()):
        return False
    try:
        import ctypes

        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError) as exc:
        log.warning("app_identity_failed", error_type=type(exc).__name__, error=str(exc))
        return False
    if hr != 0:
        log.warning("app_identity_failed", hresult=hr)
        return False
    register_notification_icon()
    return True


AUMID_KEY = rf"Software\Classes\AppUserModelId\{APP_USER_MODEL_ID}"


def register_notification_icon(winreg_mod: Any = None, icon: Path | None = None) -> bool:
    """Name and picture for Kestrel's notifications, per user (Windows): a toast is drawn with the
    icon Windows has for the app's identity. Without this key Windows took it from the Start-menu
    shortcut through Explorer's icon cache, which an upgrade did not refresh, so the "Kestrel
    keeps running" toast showed the pre-upgrade orange mark (hardware check, round 4). Best
    effort and logged; returns whether the key was written."""
    try:
        if winreg_mod is None:
            import winreg as winreg_mod  # noqa: N813
        if icon is None:
            with paths.resource_path(paths.packaged_app_icon()) as p:
                icon = Path(p)
        if not icon.exists():
            log.warning("notification_icon_missing", path=str(icon))
            return False
        with winreg_mod.CreateKeyEx(winreg_mod.HKEY_CURRENT_USER, AUMID_KEY, 0,
                                    winreg_mod.KEY_SET_VALUE) as key:
            winreg_mod.SetValueEx(key, "DisplayName", 0, winreg_mod.REG_SZ, "Kestrel")
            winreg_mod.SetValueEx(key, "IconUri", 0, winreg_mod.REG_SZ, str(icon))
        log.info("notification_identity_registered", icon=str(icon))
        return True
    except (OSError, ImportError) as exc:
        log.warning("notification_identity_failed", error_type=type(exc).__name__, error=str(exc))
        return False


# ---------------------------------------------------------------- the loading window (D-UI-89)
OPENING = "Opening your data"
UPDATING = "Updating your data to the new version"
UPDATING_NOTE = "This can take a minute after an update. Your data stays on this computer."
STARTING = "Starting up"
CHECKING = "Checking your setup"
BUSY = "Finishing closing the last session"
BUSY_NOTE = "Kestrel opens by itself in a moment. There is no need to click again."
BUSY_WAIT_S = 60.0  # how long a relaunch waits for the closing copy before saying so


class StartupError(Exception):
    """A start that cannot finish: `code` for the log, `reason` (one plain sentence) and `details`
    (the step and the cause, never a stack trace) for the loading window's problem card."""

    def __init__(self, code: str, reason: str, details: str) -> None:
        super().__init__(code)
        self.code, self.reason, self.details = code, reason, details


class _Splash:
    """Drives the loading page (static/splash.html) through window.KSplash. With no window (the
    browser fallback) every call is a no-op. Never raises: a status line is not worth a crash."""

    def __init__(self, win: Any = None) -> None:
        self.win = win

    def _js(self, fn: str, *args: Any) -> None:
        if self.win is None:
            return
        call = f"window.KSplash && window.KSplash.{fn}({', '.join(json.dumps(a) for a in args)})"
        try:
            self.win.evaluate_js(call)
        except Exception as exc:
            log.debug("splash_update_failed", fn=fn, error=str(exc))

    def say(self, text: str, pct: int | None, later: str = "", after_ms: int = 0) -> None:
        self._js("say", text, pct, later, after_ms)

    def fail(self, reason: str, details: str) -> None:
        self._js("fail", reason, details)

    def ready(self) -> None:
        self._js("ready")


def splash_html(theme: str = "auto") -> str:
    """The loading page, self-contained: tokens.css and the mark are inlined, since no server
    exists yet when it shows (D-UI-89). A saved light or dark theme is pinned on it, so its first
    paint matches the app's."""
    import base64

    static = Path(paths.web_static_dir())
    page = (static / "splash.html").read_text(encoding="utf-8")
    tokens = (static / "css" / "tokens.css").read_text(encoding="utf-8")
    mark = base64.b64encode((static / "icon.svg").read_bytes()).decode("ascii")
    page = page.replace("/*K-TOKENS*/", tokens).replace(
        "/*K-MARK*/", f"data:image/svg+xml;base64,{mark}"
    )
    if theme in ("light", "dark"):
        page = page.replace('<html lang="en">', f'<html lang="en" data-theme="{theme}">', 1)
    return page


def _updating() -> bool:
    """True when this launch has migrations to run on an existing database (an update)."""
    from kestrel.db import migrate

    try:
        current = migrate.current_revision()
        return bool(current) and current != migrate.head_revision()
    except Exception as exc:  # only the wording depends on it
        log.debug("revision_check_failed", error=str(exc))
        return False


class _Cancelled(Exception):
    """A quit arrived while the start was still running: stop where it stands."""


def _check(stop: Callable[[], bool]) -> None:
    if stop():
        raise _Cancelled


def _start_backend(splash: _Splash, stop: Callable[[], bool] = lambda: False) -> int:
    """Migrations, the server, its health, runtime.json and preflight, reporting each step to the
    loading window. Raises StartupError with a plain reason when a step cannot finish, and
    _Cancelled as soon as `stop()` turns true (a quit during loading): no server is started after
    a quit, and one already started is stopped by the caller."""
    from kestrel.db import migrate
    from kestrel.web.serve import wait_for_health, write_runtime

    _check(stop)
    if _updating():
        splash.say(UPDATING, None, UPDATING_NOTE, 3500)
    else:
        splash.say(OPENING, 30)
    try:
        migrate.run_migrations()
    except Exception as exc:  # the operator sees a sentence; the log keeps the cause
        log.error("startup_migrations_failed", error_type=type(exc).__name__, error=str(exc))
        raise StartupError(
            "migrations_failed",
            "Kestrel could not open your data on this computer.",
            f"Startup step: open your data\nCause: {type(exc).__name__}\n"
            "Log file: kestrel.log (in the logs folder)",
        ) from exc
    _check(stop)
    splash.say(STARTING, 60)
    try:
        port = start_server()
    except OSError as exc:
        log.error("startup_no_port", error=str(exc))
        raise StartupError(
            "no_port",
            "Another app is using the connection Kestrel needs on this computer. Closing other "
            "apps and trying again usually fixes it.",
            "Startup step: start the local server\nPorts 8765 to 8774 were busy, and the system "
            "could not assign another.\nLog file: kestrel.log (in the logs folder)",
        ) from exc
    _check(stop)
    # Polled a second at a time so a quit during the wait is noticed at once.
    deadline = time.monotonic() + HEALTH_WAIT_S
    healthy = False
    while not healthy and time.monotonic() < deadline:
        _check(stop)
        healthy = wait_for_health(port, timeout=1.0)
    if not healthy:
        raise StartupError(
            "health_never_up",
            "Kestrel's local server did not answer in time. Trying again usually fixes it.",
            f"Startup step: start the local server\nThe server on port {port} did not answer "
            f"within {int(HEALTH_WAIT_S)} seconds.\nLog file: kestrel.log (in the logs folder)",
        )
    _check(stop)
    write_runtime(port, os.getpid())
    splash.say(CHECKING, 85)
    run_preflight()
    _check(stop)
    return port


HEALTH_WAIT_S = 30.0


# ------------------------------------------------- runtime.json as the lock owner's marker
def _runtime_info() -> dict[str, Any]:
    try:
        data = json.loads(paths.runtime_json().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_marker(**extra: Any) -> None:
    """runtime.json before the server exists: this process holds the lock and is starting, so a
    second launch waits for it and focuses it instead of taking the "still closing" path."""
    try:
        paths.ensure_dirs()
        paths.runtime_json().write_text(
            json.dumps({"pid": os.getpid(), **extra}), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("runtime_marker_failed", error=str(exc))


def _mark_closing() -> None:
    """A quit has begun: say so in runtime.json, so a relaunch shows "Finishing closing the last
    session" and waits, rather than trying to focus a window that is going away."""
    info = _runtime_info()
    if info.get("pid") == os.getpid():
        _write_marker(**{k: v for k, v in info.items() if k != "pid"}, closing=True)


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = wintypes.DWORD()
            try:
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            finally:
                k32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:  # unknown: assume alive, the bounded wait still ends
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


SECOND_LAUNCH_WAIT_S = 60.0
NO_MARKER_GRACE_S = 1.0  # a first copy writes its marker within milliseconds of the lock


def _second_launch(wait_s: float = SECOND_LAUNCH_WAIT_S) -> str:
    """Another process holds the lock. Returns 'focused' (its window came forward), 'running'
    (it is alive and still loading past the wait; its own window shows it), or 'closing' (it is
    quitting or gone: take the busy path and wait for the lock)."""
    start = time.monotonic()
    deadline = start + wait_s
    while True:
        if _focused:
            return "focused"
        info = _runtime_info()
        # No marker, or one without a pid (the last copy removed it on its way out while still
        # holding the lock for an instant, Mac hardware check 2026-10-27): it is closing, so take
        # the busy path now instead of waiting the full minute.
        if not info.get("pid") and time.monotonic() - start >= NO_MARKER_GRACE_S:
            # The lock is held but no copy says it is starting or running: the last one has
            # removed its marker on the way out. Show "Finishing closing" now, not in a minute.
            return "closing"
        pid = info.get("pid")
        alive = _pid_alive(pid) if pid else False
        if info.get("closing") or (pid and not alive):
            return "closing"
        if info.get("failed"):
            return "running"  # its window shows Kestrel couldn't start, with Try again
        if info.get("port") and not info.get("starting"):
            focus_running_instance()
            if _focused:
                return "focused"
            if not _server_answers(info["port"]):
                return "closing"
        if time.monotonic() >= deadline:
            return "running" if alive else "closing"
        time.sleep(0.5)


def _server_answers(port: Any) -> bool:
    try:
        import httpx

        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0).status_code == 200
    except Exception:
        return False


def _stop_server() -> None:
    """Stop a server a failed start left running (it also runs the lifespan shutdown)."""
    global server, server_thread
    if server is not None:
        server.should_exit = True
    if server_thread is not None:
        server_thread.join(timeout=35.0)
        if server_thread.is_alive():
            log.warning("server_thread_still_alive_after_abort")
    server, server_thread = None, None
    with __import__("contextlib").suppress(OSError):
        paths.runtime_json().unlink()


def _wait_for_lock(timeout: float = BUSY_WAIT_S) -> bool:
    """A relaunch while the previous copy is still closing: wait for its lock to free, then take
    it. The Windows mutex lives while any handle is open, so this launch's own handle from the
    failed first try is closed before each new try."""
    global _mutex_handle, _have_lock
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _quitting:
        if IS_WINDOWS:
            if _mutex_handle is not None:
                with __import__("contextlib").suppress(Exception):
                    import ctypes

                    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_mutex_handle)
                _mutex_handle = None
            if _acquire_windows_mutex():
                _have_lock = True
                return True
        elif _acquire_lock_file():
            _have_lock = True
            return True
        time.sleep(0.5)
    return False


def _import_webview() -> Any:
    """pywebview with a backend that can show a window, or None (the browser fallback). On
    Windows a missing WebView2 is a real failure, as before, so the import error is raised."""
    try:
        import webview

        if sys.platform == "linux":
            # Say up front what a missing GTK WebKit means (release round R1); the browser
            # fallback shows the same in a panel when the window then cannot open.
            from kestrel.desktop import linux_check

            gap = linux_check.detect()
            if gap is not None:
                log.warning("linux_webkit_probe", missing=gap.missing)
        if not IS_WINDOWS:
            # pywebview picks its backend lazily, inside webview.start(); a Linux box with no
            # GTK/Qt bindings (or a mac without the Cocoa extras) would pass create_window and
            # then raise out of start(). Probe now so the browser fallback runs instead.
            webview.initialize()
        return webview
    except Exception as exc:
        if IS_WINDOWS:
            raise
        log.warning("webview_unavailable_using_browser", error=f"{type(exc).__name__}: {exc}")
        if sys.platform == "linux":
            from kestrel.desktop import linux_check

            linux_check.record_fallback(f"{type(exc).__name__}: {exc}")
        return None


def run_desktop() -> int:
    """The full startup sequence. Called from __main__ after freeze_support + stdout repair.
    Returns the process exit code: 0 for a clean run or a second-instance handoff, non-zero when
    the server never became healthy.

    Window first (D-UI-89): the frameless window opens at its final size on the loading page,
    which names each step while migrations, the server and preflight run behind it, then the
    app replaces it. A failed start shows its problem card in the same window (Try again, Open
    logs folder, Quit). A relaunch while the previous copy is still closing shows "Finishing
    closing the last session" and opens by itself once that copy has exited."""
    paths.ensure_dirs()
    _configure_logging()
    log.info(
        "kestrel_start",
        pid=os.getpid(),
        version=__version__,
        frozen=getattr(__import__("sys"), "frozen", False),
    )

    busy = False
    if acquire_single_instance():
        _write_marker(starting=True)
    else:
        if "--tray" in sys.argv[1:]:
            log.info("second_instance_exit", reason="quiet_start")
            return 0
        state = _second_launch()
        if state != "closing":
            # Focused, or alive and still loading in its own window (D-UI-89).
            log.info("second_instance_exit", reason=state)
            return 0
        # The other copy holds the lock but is quitting: wait for it (D-UI-89).
        busy = True
        log.info("previous_instance_closing")
    set_app_identity()

    webview = _import_webview()
    if webview is None:
        if busy and not _wait_for_lock():
            log.warning("second_instance_exit", reason="previous_never_closed")
            return 0
        if busy:
            _write_marker(starting=True)
        try:
            port = _start_backend(_Splash())
        except StartupError as exc:
            return abort_start(exc.code)
        return _run_in_browser(f"http://127.0.0.1:{port}/")
    return _run_window(webview, busy=busy)


_started = False  # the app replaced the loading page
_boot_failed = False  # the last start attempt ended on the problem card
_frame_api: Any = None
_quiet = False  # started by the start-with-my-computer entry (`--tray`)


def _close_intent() -> str:
    """What the window's close does right now: 'tray' (hide, keep working) or 'quit'."""
    from kestrel.web.deps import close_to_tray

    if not _started or icon is None or not close_to_tray():
        return "quit"
    return "tray"


def _close_from_page() -> None:
    """The sidebar's close button: exactly the OS close (on_closing), then the window goes when
    that close is a real quit."""
    if on_closing() and window is not None:
        window.destroy()


_boot_guard = threading.Lock()  # one start attempt at a time (first boot, Try again)
_minimized = False


def _retry_start() -> bool:
    """Try again on the problem card. At most one start runs: the guard is taken here, atomically,
    and released when that start ends. A failed wait for the previous session's lock is retried
    as that wait (busy), never as a start without the lock. Returns whether a start began."""
    if _started or _quitting:
        return False
    if not _boot_guard.acquire(blocking=False):
        return False
    threading.Thread(
        target=_boot_held, kwargs={"busy": not _have_lock}, name="startup", daemon=True
    ).start()
    return True


def _resize_to(win: Any, width: int, height: int) -> None:
    try:
        win.resize(width, height)
    except Exception as exc:  # the window still works at the size it has
        log.warning("window_resize_failed", error=str(exc))


def _maximize_restored() -> None:
    if _frame_api is not None and not _frame_api._is_maximized():
        _frame_api.window_toggle_maximize()


def _on_maximized(value: bool) -> None:
    if _frame_api is not None:
        _frame_api._set_maximized(value)
    if window is not None and _started:
        with __import__("contextlib").suppress(Exception):
            window.evaluate_js(f"window.KUI && KUI.frame.state({'true' if value else 'false'})")


def _on_minimized() -> None:
    global _minimized
    _minimized = True


def _on_restored() -> None:
    """Back from a minimize the window returns to the size it had, maximized or not; only a
    restore from maximized clears the maximized state."""
    global _minimized
    if _minimized:
        _minimized = False
        return
    _on_maximized(False)


def _boot(busy: bool = False) -> None:
    """The startup steps behind the loading page (webview.start runs this on its own thread)."""
    if not _boot_guard.acquire(blocking=False):
        return
    _boot_held(busy)


def _boot_held(busy: bool) -> None:
    try:
        _boot_steps(busy)
    finally:
        _boot_guard.release()


def _show_problem(splash: _Splash, reason: str, details: str) -> None:
    global _boot_failed
    _boot_failed = True
    splash.fail(reason, details)
    if _quiet:
        show_window()  # a quiet start that fails must say so, never sit hidden with the lock


def _boot_steps(busy: bool) -> None:
    global _started, _boot_failed
    splash = _Splash(window)
    _boot_failed = False
    if _quitting:
        return
    if busy:
        splash.say(BUSY, None, BUSY_NOTE, 0)
        if not _wait_for_lock():
            if _quitting:
                return
            _show_problem(
                splash,
                "The last Kestrel session is still closing. Wait a moment, then try again.",
                f"Startup step: wait for the previous session to close\nWaited "
                f"{int(BUSY_WAIT_S)} seconds.\nLog file: kestrel.log (in the logs folder)",
            )
            return
        _write_marker(starting=True)
    try:
        port = _start_backend(splash, stop=lambda: _quitting)
    except _Cancelled:
        log.info("startup_cancelled_by_quit")
        _stop_server()
        return
    except StartupError as exc:
        log.error("desktop_start_aborted", reason=exc.code)
        _stop_server()
        _write_marker(failed=True)  # the lock owner shows its problem card: a relaunch defers
        _show_problem(splash, exc.reason, exc.details)
        return
    from kestrel.web.desktop_bridge import (
        set_focus_handler,
        set_folder_handler,
        set_quit_handler,
        set_reveal_handler,
    )

    # Registered only once the app is up, so a second launch (or a Quit) never finds it half
    # started. Saved files go to Downloads and open in Explorer (D-UI-41).
    set_focus_handler(show_window)
    set_quit_handler(do_quit)  # the in-app "Quit Kestrel" (D-UI-27)
    set_reveal_handler(reveal_file)
    set_folder_handler(_open_folder)  # About's Open buttons for the data and logs folders
    if not IS_MACOS:
        start_tray()  # macOS: already on the main thread before webview.start()
    splash.ready()
    time.sleep(0.45)  # the loading page fades out (launch-mockup-locked.html :41-60)
    if _quitting:
        _stop_server()
        return
    _started = True
    log.info("webview_app_loading")
    if window is not None:
        # #k-intro asks the page for the one-time launch entrance (kestrel-ui.js).
        window.load_url(f"http://127.0.0.1:{port}/#k-intro")
        if _quiet and icon is None:
            show_window()  # a quiet start with no tray to open it from must still show


def _run_window(webview: Any, *, busy: bool) -> int:
    global window, _frame_api, _quiet, _started
    from kestrel.desktop import frame
    from kestrel.web.desktop_bridge import set_focus_handler, set_quit_handler, set_window_frame

    global _hidden, _minimized
    _started = False
    _minimized = False
    _quiet = "--tray" in sys.argv[1:]
    _hidden = _quiet
    _frame_api = frame.WindowApi(
        lambda: window,
        close_intent=_close_intent,
        close=_close_from_page,
        retry=_retry_start,
        open_logs=lambda: _open_folder(paths.logs_dir()),
        quit_app=do_quit,
        remember_theme=_remember_theme,
    )
    # pywebview cancels every download unless told otherwise (edgechromium's
    # on_download_starting sets args.Cancel when ALLOW_DOWNLOADS is off): the diagnostics
    # zip, a backup, a rendered PDF or the notices archive flashed a window and vanished.
    # With it on, WebView2 shows the standard Save As dialog (D-UI-12).
    webview.settings["ALLOW_DOWNLOADS"] = True
    # The drag region is the EMPTY top of the sidebar and of the page's top row: a control inside
    # it stays a control. Windows drags natively instead (frame.py), so pywebview's JavaScript
    # drag must not also move the window there.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    if IS_WINDOWS:
        webview.settings["DRAG_REGION_SELECTOR"] = ".k-no-pywebview-drag"
    width, height = _initial_size()
    place: dict[str, int] = {}
    restored = _restore_geometry(_window_state().get("geometry"), _screen_areas())
    maximize_on_show = False
    if restored is not None:
        (x, y, width, height), maximize_on_show = restored
        place = {"x": x, "y": y}
    theme, dark = _launch_theme()
    # The window's own ground before any page paints: the loading page's top color in the theme
    # it will show, so a dark launch never flashes white (hardware check, 2026-10-26).
    background = _token("sky-top", dark) or ("#000000" if dark else "#FFFFFF")
    window = webview.create_window(
        f"Kestrel {__version__}",
        html=splash_html(theme),
        background_color=background,
        width=width,
        height=height,
        **place,  # D-UI-195: the last place when it still fits a display, else centered
        min_size=MIN_SIZE,
        # pywebview blocks text selection app-wide unless asked (#26): the LAN secret, a
        # path or an error message could not be selected or copied. Draggable board cards
        # opt out in CSS (.pcard) so a drag never starts a selection.
        text_select=True,
        # D-UI-88: no OS title bar; the controls are drawn in the sidebar (macOS keeps its
        # own traffic lights, see frame.show_traffic_lights).
        frameless=True,
        easy_drag=False,
        js_api=_frame_api,
        hidden=_quiet,
    )
    window.events.closing += on_closing
    window.events.maximized += lambda: _on_maximized(True)
    window.events.minimized += _on_minimized
    window.events.restored += _on_restored
    set_window_frame(True)
    if IS_WINDOWS:
        window.events.shown += lambda: frame.install_resizable_frame(window)
        # WinForms keeps the client size when the border style turns to None, so the
        # frameless window comes up short by the frame it no longer has (1664x961 for
        # 1680x1000 on the hardware check): size it again once it is shown.
        window.events.shown += lambda: _resize_to(window, width, height)
    window.events.moved += _geometry_changed
    window.events.resized += _geometry_changed
    if maximize_on_show:
        # The normal rectangle first (above), then maximized, so a restore returns to it.
        window.events.shown += _maximize_restored
    if IS_MACOS:
        window.events.shown += lambda: frame.show_traffic_lights(window)
        start_tray()  # the icon joins the window's Cocoa loop, built on the main thread
    log.info("webview_start")
    webview.start(_boot, (busy,))  # blocks until window.destroy()
    set_window_frame(False)
    _frame_api = None  # the window is gone: on_closing goes back to its plain rules
    if not _started:
        # The window closed before the app ever loaded (a failed start, or closed while
        # loading). A start still running notices the quit at its next step and stops; wait for
        # it, so no server thread is started behind us and none is left to hold the process.
        if _boot_guard.acquire(timeout=HEALTH_WAIT_S + 10):
            _boot_guard.release()
        else:
            log.warning("startup_thread_still_running_at_quit")
        _stop_server()
        release_single_instance()
        set_focus_handler(None)
        set_quit_handler(None)
        return 1 if _boot_failed else 0
    # webview returned => a real quit is in progress. Let uvicorn's graceful shutdown (worker drain
    # + lease release) finish before the process exits; never truncate it.
    return _finish_quit()


# The hard ceiling on a quit: open requests (serve.GRACEFUL_REQUEST_S), the worker's stop
# (app.WORKER_STOP_S) and lease release normally take one to two seconds (D-UI-90).
QUIT_JOIN_S = 30.0


def _finish_quit() -> int:
    if server_thread is not None:
        server_thread.join(timeout=QUIT_JOIN_S)
    with __import__("contextlib").suppress(OSError):
        paths.runtime_json().unlink()
    if server_thread is not None and server_thread.is_alive():
        # The drain window has passed and the server still has not stopped. Returning would leave
        # a process with no window and no tray that only Task Manager can end (0.2.0, D-UI-17), so
        # end it here. Nothing durable is lost: every unit of work is a task row whose lease
        # expires and is picked up at next launch (hard rule 5).
        log.warning("quit_forced_after_drain_timeout", waited_s=QUIT_JOIN_S)
        logging.shutdown()
        os._exit(0)
    log.info("kestrel_exit_clean")
    return 0


def _run_in_browser(url: str) -> int:
    """The no-window path (macOS/Linux without a pywebview backend): print the URL, open the
    default browser, keep the server in the foreground until Ctrl+C or a tray/API quit, then run
    the same graceful shutdown the window path does. The tray and the in-app Quit button are
    wired here too: a packaged app on this path has no terminal, so without them nothing could
    end the process but a force quit."""
    from kestrel.web.desktop_bridge import set_focus_handler, set_quit_handler

    print(f"Kestrel is running at {url}  (Ctrl+C to quit)")
    try:
        webbrowser.open(url)
    except Exception as exc:  # no browser registered is not fatal; the URL is on screen
        log.warning("browser_open_failed", error=str(exc))
    # A second launch's /api/focus brings the UI back by opening a tab, the closest thing to a
    # raise when there is no window to raise.
    set_focus_handler(lambda: webbrowser.open(url))
    set_quit_handler(do_quit)  # the in-app "Quit Kestrel" button (D-UI-27)
    start_tray(with_window=False)
    try:
        if _tray_owns_loop:
            # The wait loop below also ended when the server died; keep that while the tray
            # loop holds the main thread.
            threading.Thread(
                target=_quit_when_server_stops, name="server-watch", daemon=True
            ).start()
            run_tray_loop()
        else:
            while not _quitting and server_thread is not None and server_thread.is_alive():
                time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("ctrl_c")
    do_quit()
    set_focus_handler(None)
    set_quit_handler(None)
    return _finish_quit()


def _quit_when_server_stops() -> None:
    if server_thread is not None:
        server_thread.join()
    if not _quitting:
        log.warning("server_stopped_quitting")
        do_quit()


def _configure_logging() -> None:
    """Route stdlib logging AND structlog into kestrel.log. Configuring stdlib alone left every
    Kestrel event (all structlog) printing to a stdout the windowed build does not have."""
    from kestrel.logging_setup import configure_logging

    configure_logging()
