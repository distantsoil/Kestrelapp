"""Run the API + worker (SPEC-INTERFACES 1.1). Bind the port ourselves so we know it before serving,
write it and the pid to runtime.json (so a second launch, the MCP server, and the extension can find
the running instance), then run uvicorn. The port comes from a small deterministic ladder starting
at 8765, falling back to an OS-assigned one only if every rung is busy, because the browser
extension can only discover the app by probing (see bind_free_port). The desktop shell (Phase 14)
reuses build_app + this port-binding; `kestrel serve` is the headless path with no window."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable

import httpx
import structlog
import uvicorn

from kestrel import paths
from kestrel.web.app import build_app

log = structlog.get_logger()


# The browser extension cannot read runtime.json: an MV3 service worker has no filesystem access.
# Its only way to find a running Kestrel is to probe. A purely OS-assigned port makes that
# impossible (65k candidates), which is why the extension shipped unable to reach the app out of
# the box (AFTERACTION 3.9). So the app claims a deterministic, documented ladder first and only
# falls back to an ephemeral port if every rung is taken. runtime.json still records the truth.
PORT_LADDER = tuple(range(8765, 8775))


def bind_host() -> str:
    """0.0.0.0 when the operator has opted into LAN access, else loopback only.

    Read from `get_settings()`, which is the file plus environment layer and does NOT consult the
    `setting` table. That is deliberate and it matters: the bind is chosen once, before the app (and
    therefore before any database session) exists, so a DB-backed toggle could not take effect
    without a restart anyway. It also keeps this in step with app.py, which reads bind_all the same
    way for the Host and Origin allowlists. The consequence, stated because the UI has to say it
    too: a change takes effect after a restart. The Settings switch (#27, kestrel.services.
    lan_access) therefore writes the user's settings.yml, the one place every reader already
    looks, and says so; KESTREL_SERVER__BIND_ALL still overrides the file. A toggle that wrote
    only the table would leave LAN mode half-applied.

    Binding beyond loopback is safe only because kestrel/web/auth.py requires the shared key on
    every non-loopback request; do not widen this without that middleware in place.
    """
    try:
        from kestrel.config import get_settings

        return "0.0.0.0" if get_settings().server.bind_all else "127.0.0.1"  # noqa: S104
    except Exception as exc:  # a settings failure must not make the app unservable
        log.warning("bind_host_settings_failed", error=str(exc))
        return "127.0.0.1"


def _try_bind(port: int, host: str = "127.0.0.1") -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Deliberately NOT SO_REUSEADDR on a ladder rung. Setting it on Windows would let this bind
    # succeed onto a port another Kestrel is already serving, so a failed bind is the signal we
    # want: it means "taken, try the next rung". This is a collision check, NOT a defense against a
    # hostile process, which would need SO_EXCLUSIVEADDRUSE and is anyway covered by the
    # single-instance mutex. The tradeoff is that lingering TIME_WAIT connections on a rung can push
    # a quick restart onto the next one; that is harmless here because the extension discovers the
    # app by probing rather than by assuming a fixed port.
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        return None
    return sock


def bind_free_port(*, ladder: bool = True, host: str | None = None) -> tuple[socket.socket, int]:
    """Bind and return a listening socket plus its port.

    ladder=True (the app's real serve paths) prefers PORT_LADDER so the extension can find it.
    ladder=False (tests, and any caller that just needs some port) goes straight to ephemeral, so
    concurrent test servers never contend for a fixed rung.

    host defaults to bind_host(), i.e. 0.0.0.0 only when the operator turned on bind_all. Tests pass
    it explicitly so they never open a LAN-visible socket on the operator's machine.
    """
    target = host if host is not None else bind_host()
    if ladder:
        for port in PORT_LADDER:
            sock = _try_bind(port, target)
            if sock is not None:
                return sock, port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((target, 0))
    return sock, sock.getsockname()[1]


def write_runtime(port: int, pid: int) -> None:
    paths.ensure_dirs()
    paths.runtime_json().write_text(json.dumps({"port": port, "pid": pid}), encoding="utf-8")


GRACEFUL_REQUEST_S = 1  # open web requests get this long at quit, then they are cancelled (D-UI-90)


def _make_server(app, *, log_level: str) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        log_level=log_level,
        lifespan="on",
        # proxy_headers=False is a SECURITY setting here, not a preference. uvicorn defaults it to
        # True and trusts FORWARDED_ALLOW_IPS (default "127.0.0.1") to rewrite request.client from
        # X-Forwarded-For. The LAN auth middleware exempts loopback based on request.client, so with
        # that env var set to "*" by anything on the machine, a LAN device could set
        # X-Forwarded-For: 127.0.0.1 and skip authentication entirely. Kestrel binds its own socket
        # and is never behind a proxy, so there is nothing to lose by turning it off.
        proxy_headers=False,
        # access_log=False keeps the shared secret out of the log files. The QR join is
        # GET /auth?key=<secret>, and uvicorn's access logger writes the full path and query
        # verbatim to stderr, which the packaged --windowed build has already redirected to
        # logs/stderr.log. That would put the operator's key on disk in cleartext on every join,
        # against the standing rule that no secret goes in logs. Kestrel's own structlog output is
        # unaffected.
        access_log=False,
        # Quit must finish (D-UI-17). Without a cap uvicorn waits for every open request to end
        # before it runs the lifespan drain, and a hosted model call may stay open for minutes: on
        # 0.2.0 a Quit during an import left the process running with no window and no tray.
        # An abandoned page request is safe to cut; background work lives in task rows.
        timeout_graceful_shutdown=GRACEFUL_REQUEST_S,
    )
    server = uvicorn.Server(config)
    # Off-main-thread signal handling (SPEC-INTERFACES 1.1 trap 6): a no-op callable is the
    # version-robust fix for older uvicorn. Current uvicorn (capture_signals) checks the thread
    # itself: handlers are installed only when run() is on the main thread, which is exactly the
    # `kestrel serve` case, so Ctrl+C there triggers the graceful lifespan shutdown (worker drain,
    # lease release) rather than a bare KeyboardInterrupt mid-task.
    server.install_signal_handlers = lambda: None
    return server


def should_open_browser() -> bool:
    """The default for `kestrel serve --open`: open the UI in the default browser on macOS and
    Linux when a display is likely. Never on Windows (the desktop shell is the window there), never
    under KESTREL_HEADLESS=1, and not on a Linux session with no DISPLAY or WAYLAND_DISPLAY."""
    if os.environ.get("KESTREL_HEADLESS") == "1":
        return False
    if sys.platform == "win32":
        return False
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_browser_when_healthy(port: int, url: str, timeout: float = 30.0) -> threading.Thread:
    """Open `url` in the default browser from a daemon thread once /health answers, so the first
    page load never races the lifespan startup. Daemon: it must never hold the process open."""

    def _go() -> None:
        if wait_for_health(port, timeout=timeout):
            try:
                webbrowser.open(url)
            except Exception as exc:  # no registered browser: the URL was printed anyway
                log.warning("browser_open_failed", error=str(exc))
        else:
            log.warning("browser_not_opened_health_never_up", port=port)

    thread = threading.Thread(target=_go, name="open-browser", daemon=True)
    thread.start()
    return thread


def _configure_serve_logging(log_level: str) -> None:
    """Configure file logging for `kestrel serve` (same as desktop) plus a single console handler.

    The console handler is marked so we can detect and avoid adding duplicates on repeated calls.
    """
    import logging

    from kestrel.logging_setup import configure_logging

    configure_logging(log_level)
    root = logging.getLogger()
    # Check if our marked console handler is already attached
    for h in root.handlers:
        if getattr(h, "_kestrel_serve_console", False):
            return
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    console._kestrel_serve_console = True
    root.addHandler(console)


def serve(
    *,
    worker: bool = True,
    log_level: str = "info",
    open_browser: bool | None = None,
    announce: Callable[[str], None] | None = None,
) -> None:
    """Blocking headless serve for `kestrel serve`. Writes runtime.json, serves until interrupted,
    and removes runtime.json on exit. `announce` (the CLI passes typer.echo) always receives the
    bound URL; `open_browser` None means should_open_browser()."""
    from kestrel.db import migrate

    _configure_serve_logging(log_level)
    migrate.run_migrations()
    sock, port = bind_free_port()
    write_runtime(port, os.getpid())
    server = _make_server(build_app(worker=worker), log_level=log_level)
    url = f"http://127.0.0.1:{port}/"
    log.info("kestrel serve", port=port, pid=os.getpid(), worker=worker)
    if announce is not None:
        announce(f"Kestrel is serving at {url}  (Ctrl+C to stop)")
    if open_browser if open_browser is not None else should_open_browser():
        open_browser_when_healthy(port, url)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        # uvicorn re-raises the captured SIGINT after its graceful shutdown has completed, so by
        # the time we see this the worker has drained and the leases are released. A clean stop.
        log.info("kestrel serve stopped")
        if announce is not None:
            announce("Kestrel stopped.")
    finally:
        with contextlib.suppress(OSError):
            paths.runtime_json().unlink()


def wait_for_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


@contextlib.contextmanager
def running_server(*, worker: bool = False, log_level: str = "warning"):
    """Start the server on a background thread, yield its base URL, and stop it on exit. Used by the
    gate test and by any caller that needs a live server without blocking the main thread."""
    # ladder=False: this helper is used by tests and by callers that only need *a* server. Taking a
    # ladder rung here would collide with a real running Kestrel on the operator's machine.
    # Loopback pinned: this helper backs tests and short-lived in-process servers, which must never
    # open a LAN-visible socket just because the operator has bind_all on.
    sock, port = bind_free_port(ladder=False, host="127.0.0.1")
    server = _make_server(build_app(worker=worker), log_level=log_level)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        if not wait_for_health(port):
            raise RuntimeError("server health never came up")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        # The app engine was created lazily on the server thread's (now dead) loop; drop the cached
        # refs so a later caller in this process builds a fresh one on its own loop.
        from kestrel.db.engine import forget_engine

        forget_engine()
