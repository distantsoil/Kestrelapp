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
def focus_running_instance() -> None:
    """A lock only DETECTS; focus is a POST to the running instance found via runtime.json."""
    try:
        info = json.loads(paths.runtime_json().read_text(encoding="utf-8"))
        import httpx

        httpx.post(f"http://127.0.0.1:{info['port']}/api/focus", timeout=5.0)
        log.info("focused_existing_instance", port=info["port"])
    except Exception as exc:  # a second launch must never crash; it just reports
        log.warning("focus_request_failed", error=str(exc))


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


def acquire_single_instance() -> bool:
    """True iff we are the first instance. Otherwise focus the running instance and return False."""
    first = _acquire_windows_mutex() if IS_WINDOWS else _acquire_lock_file()
    if not first:
        focus_running_instance()
    return first


def release_single_instance() -> None:
    """Give the single-instance lock back on a FAILED start.

    The mutex is normally held until the process dies, which is what a clean quit relies on. But a
    start that never reached a healthy server used to return while the non-daemon uvicorn thread
    kept the process, and therefore the mutex, alive: every later launch decided Kestrel was
    already running, POSTed /api/focus at a server that never came up, and exited. Releasing here
    is what lets the next double-click start fresh."""
    global _mutex_handle, _lock_file
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
    server_thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="uvicorn"
    )
    server_thread.start()
    return port


# ------------------------------------------------------------------------- window ops (trap 8)
def hide_window() -> None:
    if window is not None:
        window.hide()
    log.info("window_hidden_worker_continues")


def show_window() -> None:
    if window is not None:
        window.show()
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
    if _quitting:
        return True
    from kestrel.web.deps import close_to_tray

    if icon is None or not close_to_tray():
        _begin_quit()
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
    "this tray icon. To quit on close instead, go to Settings > Appearance > Desktop window."
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


def _open_folder(p: Path) -> None:
    """Open the operator's own data/log folder in the OS file manager."""
    try:
        if IS_WINDOWS:
            os.startfile(str(p))  # noqa: S606
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.warning("open_folder_failed", path=str(p), error=str(exc))


def reveal_file(p: Path) -> None:
    """Show a file Kestrel just saved for the operator (the diagnostics zip, D-UI-41): Explorer
    opens its folder with the file selected. Explorer is a windowed program, so no console
    flashes. Elsewhere the folder opens (Finder selects the file on macOS)."""
    try:
        if IS_WINDOWS:
            # One command string: explorer parses `/select,"path"` itself and misreads the whole
            # argument quoted, which is what list-form quoting would produce for a path with
            # spaces. The path comes from the Downloads Known Folder and cannot contain a quote.
            subprocess.Popen(f'explorer /select,"{p}"')  # noqa: S603
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            _open_folder(p.parent)
    except OSError as exc:
        log.warning("reveal_file_failed", path=str(p), error=str(exc))
        raise


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

    return {"idle": _mk(False, None), "working": _mk(True, None),
            "working_att": _mk(True, "#b3261e")}


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


def _initial_size(screen: tuple[int, int] | None = None) -> tuple[int, int]:
    """Nine tenths of the primary screen, capped at 1600x1000 and never below the minimum.
    pywebview reports screens in logical pixels, so this already accounts for scaling."""
    if screen is None:
        try:
            import webview

            first = webview.screens[0]
            screen = (int(first.width), int(first.height))
        except Exception:  # no backend yet, or no screen: a sensible fixed size
            screen = (1440, 900)
    w = max(960, min(1600, int(screen[0] * 0.9)))
    h = max(640, min(1000, int(screen[1] * 0.9) - 40))  # 40: the taskbar
    return w, h


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
            pystray.MenuItem("Pause worker", _menu_action(lambda: _post("/api/worker/pause")),
                             checked=_paused_checked),
            pystray.MenuItem("Resume worker", _menu_action(lambda: _post("/api/worker/resume"))),
            pystray.MenuItem("Run scan now", _menu_action(lambda: _post("/api/scan/now"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Recent notifications", _menu_action(lambda: show_window())),
            pystray.MenuItem("Open data folder",
                             _menu_action(lambda: _open_folder(paths.data_dir()))),
            pystray.MenuItem("Open logs", _menu_action(lambda: _open_folder(paths.logs_dir()))),
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
    if icon is None or not _tray_owns_loop:
        return
    icon.visible = True
    icon.run(setup=lambda _icon: None)  # blocks until icon.stop() from _begin_quit


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


def run_desktop() -> int:
    """The full startup sequence. Called from __main__ after freeze_support + stdout repair.
    Returns the process exit code: 0 for a clean run or a second-instance handoff, non-zero when
    the server never became healthy."""
    from kestrel.db import migrate
    from kestrel.web.serve import wait_for_health, write_runtime

    paths.ensure_dirs()
    _configure_logging()
    log.info("kestrel_start", pid=os.getpid(), version=__version__,
             frozen=getattr(__import__("sys"), "frozen", False))

    if not acquire_single_instance():
        log.info("second_instance_exit")
        return 0

    migrate.run_migrations()
    port = start_server()
    if not wait_for_health(port):
        return abort_start("health_never_up")
    write_runtime(port, os.getpid())
    run_preflight()

    from kestrel.web.desktop_bridge import set_focus_handler

    url = f"http://127.0.0.1:{port}/"
    global window
    try:
        import webview

        if sys.platform == "linux":
            # Say up front what a missing GTK WebKit means (release round R1); the browser
            # fallback below shows the same in a panel when the window then cannot open.
            from kestrel.desktop import linux_check

            gap = linux_check.detect()
            if gap is not None:
                log.warning("linux_webkit_probe", missing=gap.missing)
        if not IS_WINDOWS:
            # pywebview picks its backend lazily, inside webview.start(), AFTER create_window has
            # returned; a Linux box with no GTK/Qt bindings (or a mac without the Cocoa extras)
            # would therefore pass create_window and then raise out of start() with the tray
            # already up. Probe now so that failure lands in the except below and the browser
            # fallback runs instead. Windows is untouched: WebView2 is loaded by start() as before.
            webview.initialize()
        # pywebview cancels every download unless told otherwise (edgechromium's
        # on_download_starting sets args.Cancel when ALLOW_DOWNLOADS is off): the diagnostics
        # zip, a backup, a rendered PDF or the notices archive flashed a window and vanished.
        # With it on, WebView2 shows the standard Save As dialog (D-UI-12).
        webview.settings["ALLOW_DOWNLOADS"] = True
        # Opens large but not maximized (D-UI-14, revised by the operator in 0.3.0: "full
        # screen is fine but not preferred"): most of the primary screen, never past it, so the
        # bottom of a page (the Hosted key and Next button) is never below the fold at 150%
        # scaling. min_size keeps a resized window usable.
        width, height = _initial_size()
        window = webview.create_window(
            f"Kestrel {__version__}", url=url, width=width, height=height,
            min_size=(960, 640),
        )
    except Exception as exc:
        if IS_WINDOWS:  # the packaged product: a missing WebView2 is a real failure, as before
            raise
        # macOS without the Cocoa backend wired, Linux without GTK/Qt: not a reason to refuse to
        # run. The server is already healthy, so hand the UI to the default browser instead.
        log.warning("webview_unavailable_using_browser", error=f"{type(exc).__name__}: {exc}")
        if sys.platform == "linux":
            from kestrel.desktop import linux_check

            linux_check.record_fallback(f"{type(exc).__name__}: {exc}")
        window = None
        return _run_in_browser(url)

    window.events.closing += on_closing
    # Register the window with the tray and /api/focus only AFTER it exists, so a second launch (or
    # a tray Quit) during startup never finds `window is None`.
    set_focus_handler(show_window)
    from kestrel.web.desktop_bridge import set_quit_handler

    set_quit_handler(do_quit)  # the in-app "Quit Kestrel" button (D-UI-27)
    from kestrel.web.desktop_bridge import set_reveal_handler

    # Saved files (the diagnostics zip) go to Downloads and open in Explorer, never through the
    # WebView2 downloads flyout (D-UI-41).
    set_reveal_handler(reveal_file)
    start_tray()
    log.info("webview_start")
    webview.start()  # blocks the main thread until window.destroy()

    # webview returned => a real quit is in progress. Let uvicorn's graceful shutdown (worker drain
    # + lease release) finish before the process exits; never truncate it.
    return _finish_quit()


# Open requests (serve.GRACEFUL_REQUEST_S) plus the lifespan's 30 s worker drain, with headroom.
QUIT_JOIN_S = 45.0


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
            threading.Thread(target=_quit_when_server_stops, name="server-watch",
                             daemon=True).start()
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
