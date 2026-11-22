"""Does the desktop window reach its pages under the Content-Security-Policy? (D-SEC-61)

The shell no longer uses pywebview's evaluate_js or js_api: both build code from strings in the
page, and WKWebView refuses them under the app's policy (owner's Mac, 2026-11-15). The pages
talk to the shell over HTTP instead (kestrel/desktop/channel.py). This checks both channels once
per launch, on the person's own machine:

- splash: the loading page asked the shell's own server for its updates (splash_server.py),
  recorded when the app page is told to load.
- window: the app page called window.bridge_check over /api/window/call (kestrel-ui.js, once it
  has the window's info). No call within API_WAIT_S of the app page loading records it failed.

The result is one log line (desktop_bridge_check, info when both are ok, warning otherwise) and
the desktop_bridge entry of the diagnostics summary. A failure is evidence only: it never
retries, refuses or changes anything else.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger()

API_WAIT_S = 30.0
_lock = threading.Lock()
_state: dict[str, Any] = {}
_started = False
_window_event = threading.Event()


def reset() -> None:
    global _started
    with _lock:
        _state.clear()
        _state.update(checked=False, splash="not checked", window="not checked",
                      evaluate_js="not used", at=None)
        _started = False
    _window_event.clear()


reset()


def summary() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def window_reached() -> None:
    """The app page called bridge_check through /api/window/call."""
    with _lock:
        _state["window"] = "ok"
    _window_event.set()


def start(splash_channel: Any = None, *, wait_s: float = API_WAIT_S) -> threading.Thread | None:
    """Called once the window is told to load the app page."""
    global _started
    with _lock:
        if _started:
            return None
        _started = True
        fetched = getattr(splash_channel, "last_fetch", None)
        _state["splash"] = ("ok" if fetched is not None else
                            "failed: the loading page never asked the shell for its updates")

    def run() -> None:
        reached = _window_event.wait(wait_s)
        with _lock:
            if not reached and _state["window"] != "ok":
                _state["window"] = (f"failed: the app page did not reach the window channel "
                                    f"within {int(wait_s)} s")
            _state.update(checked=True, at=datetime.now(UTC).isoformat(timespec="seconds"))
            snap = dict(_state)
        level = log.info if snap["splash"] == "ok" and snap["window"] == "ok" else log.warning
        level("desktop_bridge_check", splash=snap["splash"], window=snap["window"],
              evaluate_js=snap["evaluate_js"])

    t = threading.Thread(target=run, name="bridge-check", daemon=True)
    t.start()
    time.sleep(0)
    return t
