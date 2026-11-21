"""The desktop window's channel to its pages, without eval (D-SEC-61, #212 round 4).

pywebview's own bridge builds code from strings in the page: evaluate_js wraps every script in
eval() (webview/window.py:463 and :477), the js_api functions are made with new Function
(webview/js/api.js:75), and every js_api answer comes back through evaluate_js
(webview/util.py:264, js_bridge_call). Under the app's Content-Security-Policy WKWebView refuses
all three (owner's Mac, 2026-11-15: EvalError on the eval wrapper; window.pywebview.api never
appeared), so Kestrel does not use either.

Instead the shell talks to a page over HTTP that the page itself makes, under script-src 'self':

- shell to page: Channel.post(name, *args) appends an event; the page long-polls
  GET .../events?after=N and runs each event whose name is on its own allowlist.
- page to shell: POST .../call/<name> runs that method of the shell's WindowApi
  (kestrel/desktop/frame.py), if the name is on the caller's allowlist.

The loading page reaches this through the shell's own small server (splash_server.py), since the
app server may not be running; the app pages reach it through /api/window/* on the app server.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

EVENTS_KEPT = 200


class Channel:
    """Events for one kind of page, in order, numbered from 1. Thread-safe."""

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self._events: deque[tuple[int, str, list[Any]]] = deque(maxlen=EVENTS_KEPT)
        self._seq = 0
        self._cond = threading.Condition()
        self.last_fetch: float | None = None  # monotonic time a page last asked for events

    def post(self, name: str, *args: Any) -> int:
        if name not in self.names:
            raise ValueError(f"not a page event: {name}")
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, name, list(args)))
            self._cond.notify_all()
            return self._seq

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def after(self, after: int, timeout: float) -> tuple[int, list[list[Any]]]:
        """Events numbered above `after`, waiting up to `timeout` seconds for one. `after` below
        zero means "from now": the current number and no events, at once (a page that loads late
        must not replay a closing screen or an old state)."""
        with self._cond:
            self.last_fetch = time.monotonic()
            if after < 0:
                return self._seq, []
            if self._seq <= after:
                self._cond.wait_for(lambda: self._seq > after, timeout=timeout)
            self.last_fetch = time.monotonic()
            return self._seq, [[name, args] for seq, name, args in self._events if seq > after]


# What each page may be told, and what it may ask the shell to do (WindowApi method names).
SPLASH_EVENTS = frozenset({"say", "fail", "ready", "note", "cancelBtn"})
APP_EVENTS = frozenset({"state", "closing", "open_update"})
WINDOW_CALLS = frozenset({
    "window_info", "window_minimize", "window_toggle_maximize", "window_close_intent",
    "window_close", "window_set_theme", "window_set_frame_color", "window_start_drag",
    "window_start_resize", "bridge_check",
})
SPLASH_CALLS = WINDOW_CALLS | frozenset({
    "splash_retry", "splash_safe_start", "splash_download", "splash_open_logs", "splash_quit",
    "splash_cancel",
})


def call(target: Any, allowed: frozenset[str], name: str, args: Any) -> Any:
    """Run `name` on the shell's WindowApi with `args` (a JSON list), if `allowed` names it.
    Raises LookupError for a name that is not allowed or not there."""
    if name not in allowed or target is None:
        raise LookupError(name)
    fn = getattr(target, name, None)
    if not callable(fn):
        raise LookupError(name)
    if not isinstance(args, list):
        args = []
    return fn(*args[:4])
