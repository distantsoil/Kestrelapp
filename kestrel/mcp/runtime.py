"""Start and stop the MCP server inside the running app (D-MCP-1), so the Settings switch applies
at once with no restart.

The server itself is unchanged: `kestrel.mcp.server.serve_mcp` with `sse_app`, which pins the
Host and Origin allowlists (DNS rebinding) and requires the LAN shared secret of every
non-loopback peer when `server.bind_all` is on. This module only owns the task. It lives on
uvicorn's loop, like the worker (CLAUDE.md: one loop), and is started from the FastAPI lifespan
when `mcp.enabled` is on and by the Settings switch. A UI-only process (no worker, tests) never
opens the port: `live` stays False and the switch says the change applies at the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from typing import Any

import structlog

log = structlog.get_logger()

SSE_PATH = "/sse"
_state: dict[str, Any] = {
    "live": False, "task": None, "stop": None, "port": None, "bind_all": False, "reason": None,
    "recorded": None,  # the task whose end is already in "reason" (recorded once, then kept)
}


def set_live(live: bool) -> None:
    """The lifespan of a worker app says it can host the server (the desktop app, kestrel
    serve); a UI-only app cannot."""
    _state["live"] = live


def is_live() -> bool:
    return bool(_state["live"])


def running() -> bool:
    """Serving now. serve_mcp ends when its inner uvicorn server ends (review finding 7), so a
    dead server reads as not running."""
    task = _state["task"]
    return task is not None and not task.done()


def stopped_reason() -> str | None:
    """Why the server stopped without the operator asking, in plain words; None while it runs,
    or after the operator stopped it.

    Read from the finished task itself when its done-callback has not run yet: asyncio schedules
    that callback with call_soon after the task finishes, so a caller can see task.done() (and
    running() False) one loop iteration before `_done` records the reason. Recording is
    idempotent, so whichever of the two gets there first wins and the other changes nothing."""
    if running():
        return None
    task = _state["task"]
    if task is not None:
        _record(task)
    return _state["reason"]


def port_free(port: int, bind_all: bool = False) -> bool:
    """Whether the port can be bound, checked before uvicorn tries (a failed bind inside
    uvicorn exits the process). On Linux and macOS SO_REUSEADDR is set, as uvicorn sets it, so
    a socket left in TIME_WAIT by a server just stopped is not a false "in use" (finding 6).
    Never on Windows: there SO_REUSEADDR lets a second socket steal a port already bound."""
    host = "0.0.0.0" if bind_all else "127.0.0.1"  # noqa: S104 - LAN bind is opt-in (bind_all)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def address(port: int, bind_all: bool = False) -> dict[str, str | None]:
    """What to paste into an MCP client: the loopback address, and the LAN one when bind_all."""
    lan = None
    if bind_all:
        from kestrel.web.auth import lan_host_ip

        ip = lan_host_ip()
        lan = f"http://{ip}:{port}{SSE_PATH}" if ip else None
    return {"local": f"http://127.0.0.1:{port}{SSE_PATH}", "lan": lan}


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _record(task: asyncio.Task) -> None:
    """Record once why the current, finished task ended, unless the operator stopped it.
    Retrieving the exception here also keeps asyncio from logging "exception was never
    retrieved". A second call for the same task (the done-callback after stopped_reason, or
    the reverse) changes nothing and logs nothing."""
    if task is not _state["task"] or not task.done() or _state["recorded"] is task:
        return
    _state["recorded"] = task
    ev = _state["stop"]
    if task.cancelled():
        if ev is None or not ev.is_set():
            _state["reason"] = "it was cancelled"
        return
    exc = task.exception()
    if exc is not None:
        _state["reason"] = _reason(exc)
        log.error("mcp_server_stopped", error=f"{type(exc).__name__}: {exc}")
    elif ev is None or not ev.is_set():
        # serve_mcp raises when its inner server ends unasked; this covers a clean return.
        _state["reason"] = "it stopped on its own"
        log.error("mcp_server_stopped", error="the server ended without being asked to")


def _done(task: asyncio.Task) -> None:
    """The task's done-callback: record why it ended (see stopped_reason for the timing)."""
    _record(task)


async def start(port: int, *, bind_all: bool = False) -> str | None:
    """Start the server; None on success (or already running), else why it could not."""
    if running():
        return None
    if not port_free(port, bind_all):
        return (f"Port {port} is in use by another program. Change mcp.port in settings, or "
                "close the program using it.")
    from kestrel.mcp.server import serve_mcp

    stop_ev = asyncio.Event()
    task = asyncio.create_task(serve_mcp(stop_ev, port=port, bind_all=bind_all), name="mcp")
    _state.update(task=task, stop=stop_ev, port=port, bind_all=bind_all, reason=None,
                  recorded=None)
    task.add_done_callback(_done)
    log.info("mcp_server_started", port=port, bind_all=bind_all)
    return None


async def stop() -> None:
    """Stop the server. Never raises (review finding 4): an exception the task stored, a slow
    stop, or a failed cancel is logged with context, and the state is always cleared, so the
    app's shutdown carries on to the worker's graceful stop."""
    task, ev = _state["task"], _state["stop"]
    if task is None:
        return
    try:
        if ev is not None:
            ev.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except TimeoutError:
                log.warning("mcp_server_stop_slow", port=_state["port"])
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if task.done() and not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.warning("mcp_server_ended_with_error", port=_state["port"],
                        error=f"{type(exc).__name__}: {exc}")
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # the caller itself is being cancelled; the finally still clears the state
        log.warning("mcp_server_stop_cancelled", port=_state["port"])
    except Exception as exc:  # never let a stop take the app's shutdown down with it
        log.warning("mcp_server_stop_failed", port=_state["port"],
                    error=f"{type(exc).__name__}: {exc}")
    finally:
        _state.update(task=None, stop=None, reason=None, recorded=None)
    log.info("mcp_server_stopped_by_operator")
