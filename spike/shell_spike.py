"""Kestrel Phase -1 process-model spike (throwaway).

Proves the exact desktop process model in SPEC-INTERFACES.md 1.1 and the frozen-build hooks in
1.4. Disposable: the deliverable is spike/FINDINGS.md and the corrected spec, not this file.

Order in __main__ (each a numbered trap from the spec):
  1  multiprocessing.freeze_support() first
  2  replace sys.stdout/stderr when None (--windowed)
  3  resolve %LOCALAPPDATA%\\Kestrel paths, nothing writable beside the exe
  4  Local\\ named mutex; second instance POSTs /api/focus and exits 0
  5  Alembic upgrade (SYNC sqlite:// engine, versions/ bundled as data)
  6  uvicorn on an OS-assigned free port, off the main thread, install_signal_handlers=False
  7  worker + self-tests started from the FastAPI lifespan, on uvicorn's loop
  8  pystray icon in a background thread (Open / Quit)
  9  window.events.closing -> return False + window.hide(); never destroy() except on quit
 10  webview.start() on the main thread, last (blocks forever)
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from ctypes import wintypes
from pathlib import Path

# ---------------------------------------------------------------------------- paths (trap 3)
APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Kestrel"
DATA = APPDATA / "data"
LOGS = APPDATA / "logs"
RUNTIME_JSON = APPDATA / "runtime.json"
DB_PATH = DATA / "spike.db"
SELFTEST_JSON = DATA / "selftest.json"
for d in (DATA, LOGS):
    d.mkdir(parents=True, exist_ok=True)

FROZEN = getattr(sys, "frozen", False)


def resource_dir() -> Path:
    """Bundled data root: sys._MEIPASS when frozen, this file's dir in dev. No __file__ math
    leaks into alembic/env.py; only this launcher resolves paths."""
    if FROZEN:
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


log = logging.getLogger("spike")

# ------------------------------------------------------------------- module-scope singletons
_mutex_handle = None  # never closed (trap: closing releases the single-instance lock)
window = None
icon = None
server = None
counter = {"n": 0}
_port = 0
_quitting = False  # FINDING: window.destroy() re-fires `closing`; the handler must allow it then


def configure_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOGS / "spike.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # also mirror to (possibly file-backed) stdout so a dev console shows progress too
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(stream)


# --------------------------------------------------------------------------- mutex (trap 4)
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\KestrelSpike"  # Local\\, not Global\\ (needs a privilege, blocks 2nd user)


def acquire_single_instance() -> bool:
    """True if we are the first instance. If not, focus the running window and return False."""
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log.info("mutex already held; focusing existing instance")
        try:
            info = json.loads(RUNTIME_JSON.read_text())
            import httpx

            httpx.post(f"http://127.0.0.1:{info['port']}/api/focus", timeout=5.0)
            log.info("posted /api/focus to port %s", info["port"])
        except Exception as exc:  # spike: report, never crash the second launch
            log.warning("focus request failed: %s", exc)
        return False
    return True


# ------------------------------------------------------------------------- alembic (trap 5)
def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(resource_dir() / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{DB_PATH.as_posix()}")  # SYNC, not aiosqlite
    command.upgrade(cfg, "head")
    log.info("alembic upgrade head OK -> %s", DB_PATH)


# ------------------------------------------------------------- self-tests (traps 6 & 7 payload)
def _selftest_tiktoken() -> int:
    os.environ["TIKTOKEN_CACHE_DIR"] = str(resource_dir() / "vendor" / "tiktoken_cache")
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")  # offline: cache dir is vendored
    return len(enc.encode("Kestrel hovers, scans a wide field, and strikes rarely."))


def _selftest_playwright() -> dict:
    from playwright.sync_api import sync_playwright

    out = DATA / "spike_msedge.pdf"
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page()
        pg.set_content("<h1>Kestrel frozen-build PDF</h1><p>channel=msedge</p>")
        pg.pdf(path=str(out), header_template="", footer_template="", display_header_footer=False)
        b.close()
    return {"path": str(out), "bytes": out.stat().st_size}


def _selftest_sklearn() -> float:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor

    m = HistGradientBoostingRegressor(max_iter=5)
    m.fit(np.arange(20).reshape(10, 2).astype(float), np.arange(10).astype(float))
    return float(m.predict(np.array([[0.0, 1.0]]))[0])


def run_self_tests() -> None:
    """Blocking; runs in a worker thread off the loop (sync Playwright needs no running loop)."""
    result: dict = {"frozen": FROZEN}
    try:
        result["tiktoken_tokens"] = _selftest_tiktoken()
        result["playwright"] = _selftest_playwright()
        result["sklearn_pred"] = _selftest_sklearn()
        result["ok"] = True
    except Exception as exc:  # spike: capture the failure as evidence, do not crash
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("self-test failed")
    SELFTEST_JSON.write_text(json.dumps(result, indent=2))
    log.info("self-tests: %s", json.dumps(result))


# --------------------------------------------------------------------- FastAPI app (traps 6/7)
async def worker_loop() -> None:
    """Fake worker: proves the background loop keeps running with the window hidden."""
    while True:
        await asyncio.sleep(1.0)
        counter["n"] += 1
        log.info("worker tick counter=%d", counter["n"])
        if icon is not None:
            icon.title = f"Kestrel Spike - counter {counter['n']}"


@asynccontextmanager
async def lifespan(app):
    app.state.worker = asyncio.create_task(worker_loop())
    asyncio.get_running_loop().run_in_executor(None, run_self_tests)  # sync payload off-loop
    log.info("lifespan: worker + self-tests started on uvicorn loop")
    try:
        yield
    finally:
        app.state.worker.cancel()
        log.info("lifespan: worker cancelled")


def build_app():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "counter": counter["n"], "pid": os.getpid()}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return "<html><body><h1>hello</h1><p>Kestrel spike</p></body></html>"

    @app.post("/api/focus")
    async def focus():
        show_window()
        return JSONResponse({"focused": True})

    # test-only hooks: drive the SAME functions the real close/tray handlers call (decision log)
    @app.post("/api/_test/hide")
    async def _test_hide():
        hide_window()
        return {"hidden": True, "counter": counter["n"]}

    @app.post("/api/_test/show")
    async def _test_show():
        show_window()
        return {"shown": True, "counter": counter["n"]}

    @app.post("/api/_test/closing")
    async def _test_closing():
        # Exercise the REAL close-to-tray decision branch: on_closing() must cancel (return False)
        # and hide, exactly as an X-button click does. Distinct from _test_hide, which calls the
        # helper directly and so never proves the handler's cancel semantics.
        cancelled = on_closing() is False
        return {"cancelled_close": cancelled, "counter": counter["n"]}

    @app.post("/api/_test/quit")
    async def _test_quit():
        threading.Timer(0.25, do_quit).start()  # let the HTTP response return first
        return {"quitting": True}

    return app


# ------------------------------------------------------------- window ops (traps 9/10 shared)
def hide_window() -> None:
    if window is not None:
        window.hide()
    log.info("window hidden (worker keeps running)")


def show_window() -> None:
    if window is not None:
        window.show()
        try:
            window.restore()
        except Exception:  # not all backends implement restore; hide/show is the contract
            pass
    log.info("window shown")


def on_closing():
    """closing event: cancel the close and hide to tray. Return False = do not close.

    FINDING (Phase -1): window.destroy() re-fires this same event. Without the _quitting guard the
    handler cancels its own quit and the process can never exit. On a real quit, allow the close.
    """
    if _quitting:
        log.info("closing during quit -> allow close")
        return True
    log.info("closing event -> hide to tray, cancel close")
    hide_window()
    return False


def do_quit() -> None:
    """The one place a real quit happens. Stops worker/scheduler, tray, then destroys window."""
    global _quitting, server
    _quitting = True
    log.info("quit requested: stopping server, tray, window")
    if server is not None:
        server.should_exit = True
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass
    if window is not None:
        window.destroy()  # only destroy() call in the whole program; unblocks webview.start()


# -------------------------------------------------------------------- uvicorn thread (trap 6)
def start_server() -> int:
    """Bind a free port ourselves so we know it before serving, then run uvicorn off-main-thread."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    global server
    config = uvicorn.Config(build_app(), log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    # Off-main-thread signal handling. FINDING (Phase -1): uvicorn 0.52.0 has NO
    # `install_signal_handlers` attribute -- capture_signals() already skips signal install when not
    # on the main thread, so off-thread serving works with or without this line. On OLDER uvicorn it
    # is a METHOD that raises off-thread; overriding it with a no-op CALLABLE is the version-robust
    # fix. (`= False` is wrong there: the bool gets called and raises TypeError.)
    server.install_signal_handlers = lambda: None

    threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="uvicorn", daemon=True
    ).start()
    return port


def wait_for_health(port: int, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
            if r.status_code == 200:
                log.info("health 200: %s", r.text)
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


# ------------------------------------------------------------------------- tray thread (trap 8)
def start_tray() -> None:
    import pystray
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (64, 64), "#D4622A")  # Kestrel orange
    d = ImageDraw.Draw(img)
    d.ellipse((16, 16, 48, 48), fill="white")

    global icon
    icon = pystray.Icon(
        "kestrel_spike",
        img,
        "Kestrel Spike",
        menu=pystray.Menu(
            pystray.MenuItem("Open", lambda i, item: show_window()),
            pystray.MenuItem("Quit", lambda i, item: do_quit()),
        ),
    )
    threading.Thread(target=icon.run, name="tray", daemon=True).start()


# ------------------------------------------------------------------------------------- main
def main() -> None:
    # trap 2: --windowed builds have sys.stdout/stderr == None; fix before any logging
    if sys.stdout is None:
        sys.stdout = open(LOGS / "stdout.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(LOGS / "stderr.log", "a", encoding="utf-8", buffering=1)

    configure_logging()
    log.info("=== spike start pid=%d frozen=%s ===", os.getpid(), FROZEN)

    if not acquire_single_instance():
        log.info("second instance: exiting 0 after focusing existing")
        sys.exit(0)

    run_migrations()

    global _port
    _port = start_server()
    log.info("uvicorn starting on free port %d", _port)
    if not wait_for_health(_port):
        log.error("health never came up; aborting")
        sys.exit(1)

    RUNTIME_JSON.write_text(json.dumps({"port": _port, "pid": os.getpid()}))
    log.info("wrote runtime.json: port=%d pid=%d", _port, os.getpid())

    start_tray()
    log.info("preflight OK (spike stub)")

    import webview

    global window
    window = webview.create_window("Kestrel Spike", url=f"http://127.0.0.1:{_port}/")
    window.events.closing += on_closing  # trap 9
    log.info("webview.start() (blocks main thread)")
    webview.start()  # trap 10: blocks until window.destroy()

    log.info("webview returned; process exiting cleanly")
    os._exit(0)  # guarantee no orphan from pythonnet/pywebview background threads


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # trap 1: first statement in the entry point
    main()
