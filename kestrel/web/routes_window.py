"""The app page's channel to the desktop window (D-SEC-61, #212 round 4).

pywebview's js_api and evaluate_js build code from strings in the page, which the app's
Content-Security-Policy refuses (kestrel/desktop/channel.py). App pages reach the window here
instead:

  POST /api/window/call/<name>     run that WindowApi method (channel.WINDOW_CALLS), args as JSON
  GET  /api/window/events?after=N  the shell's events for the page (state, closing, open_update)

Only while the desktop shell has registered its window, and only for a request from this
computer (desktop_bridge.desktop_window); otherwise a 404. A cross-origin POST is refused by the
app's origin guard before it gets here.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from kestrel.desktop.channel import WINDOW_CALLS, call
from kestrel.web import desktop_bridge

log = structlog.get_logger()
router = APIRouter()

EVENTS_WAIT_S = 20.0


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "not found"}, status_code=404)


@router.post("/api/window/call/{name}")
async def window_call(name: str, request: Request) -> JSONResponse:
    api = desktop_bridge.window_api()
    if api is None or not desktop_bridge.desktop_window(request):
        return _not_found()
    try:
        body = await request.json()
    except ValueError:
        body = {}
    args = body.get("args", []) if isinstance(body, dict) else []
    try:
        value = await asyncio.to_thread(call, api, WINDOW_CALLS, name, args)
    except LookupError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001 (a window call must never 500 the page)
        log.warning("window_call_failed", name=name, error_type=type(exc).__name__,
                    error=str(exc)[:200])
        value = None
    try:
        return JSONResponse({"value": value})
    except (TypeError, ValueError):
        return JSONResponse({"value": None})


@router.get("/api/window/events")
async def window_events(request: Request, after: int = -1) -> JSONResponse:
    channel = desktop_bridge.window_channel()
    if channel is None or not desktop_bridge.desktop_window(request):
        return _not_found()
    seq, events = await asyncio.to_thread(channel.after, after, EVENTS_WAIT_S)
    return JSONResponse({"seq": seq, "events": events, "ending": channel.ending})
