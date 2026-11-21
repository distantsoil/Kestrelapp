"""The loading page's own server (D-UI-89, D-SEC-61, #212 round 4).

The loading window shows before the app server runs, and shows the problem card when the app
server could not start, so it cannot depend on that server. It also cannot use pywebview's
bridge: evaluate_js and js_api build code from strings, which the app's Content-Security-Policy
refuses (kestrel/desktop/channel.py). So the shell runs this: a small HTTP server on 127.0.0.1
with a random port and a per-launch path token, answering

  GET  /s/<token>/                 the loading page (static/splash.html)
  GET  /s/<token>/static/<file>    its stylesheets, script, mark and font (an allowlist)
  GET  /s/<token>/events?after=N   the shell's updates (Channel.after, long poll)
  POST /s/<token>/call/<name>      a button: runs that WindowApi method (channel.SPLASH_CALLS)

Every answer carries the app's exact policy (kestrel.web.csp.POLICY). A POST must come from the
page itself: its Origin, when sent, is this server's own, and Sec-Fetch-Site, when sent, is
same-origin; a Host other than this server's is refused. Everything else is a 404.
"""

from __future__ import annotations

import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog

from kestrel import paths
from kestrel.desktop.channel import SPLASH_CALLS, SPLASH_EVENTS, Channel, call
from kestrel.web.csp import POLICY

log = structlog.get_logger()

POLL_WAIT_S = 15.0
STATIC_FILES = {
    "css/tokens.css": "text/css; charset=utf-8",
    "css/splash.css": "text/css; charset=utf-8",
    "js/splash.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "fonts/manrope/manrope-latin-wght-normal.woff2": "font/woff2",
    "fonts/manrope/manrope-latin-ext-wght-normal.woff2": "font/woff2",
}


class SplashServer:
    """One per launch. `target` is the shell's WindowApi (or a test double)."""

    def __init__(self, target: Any, *, theme: str = "auto") -> None:
        self.target = target
        self.theme = theme if theme in ("light", "dark") else "auto"
        self.channel = Channel(SPLASH_EVENTS)
        self.token = secrets.token_urlsafe(18)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/s/{self.token}/"

    def start(self) -> SplashServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="splash-server",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:  # shutdown() waits for serve_forever, so only once started
            self._httpd.shutdown()
            self._thread = None
        self._httpd.server_close()

    def page(self) -> str:
        page = (Path(paths.web_static_dir()) / "splash.html").read_text(encoding="utf-8")
        if self.theme in ("light", "dark"):
            page = page.replace('<html lang="en">', f'<html lang="en" data-theme="{self.theme}">',
                                1)
        return page


def _handler_for(server: SplashServer) -> type[BaseHTTPRequestHandler]:
    prefix = f"/s/{server.token}/"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Kestrel"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: no request log on stderr
            return

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", POLICY)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _not_found(self) -> None:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") == f"127.0.0.1:{server.port}"

        def _rest(self) -> str | None:
            path = urlsplit(self.path).path
            return path[len(prefix):] if path.startswith(prefix) else None

        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            rest = self._rest()
            if rest is None or not self._host_ok():
                self._not_found()
                return
            if rest == "":
                self._send(HTTPStatus.OK, server.page().encode("utf-8"),
                           "text/html; charset=utf-8")
            elif rest.startswith("static/") and rest[len("static/"):] in STATIC_FILES:
                # The path read is a key of our own table, never the request's text.
                rel = next(k for k in STATIC_FILES if k == rest[len("static/"):])
                data = (Path(paths.web_static_dir()) / rel).read_bytes()
                self._send(HTTPStatus.OK, data, STATIC_FILES[rel])
            elif rest == "events":
                query = parse_qs(urlsplit(self.path).query)
                try:
                    after = int(query.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                seq, events = server.channel.after(after, POLL_WAIT_S)
                body = json.dumps({"seq": seq, "events": events}).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json")
            else:
                self._not_found()

        do_HEAD = do_GET  # noqa: N815

        def do_POST(self) -> None:  # noqa: N802
            rest = self._rest()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 4096)) if length else b""
            origin = self.headers.get("Origin")
            site = (self.headers.get("Sec-Fetch-Site") or "same-origin").strip().lower()
            if (rest is None or not rest.startswith("call/") or not self._host_ok()
                    or (origin is not None and origin != server.origin)
                    or site != "same-origin"):
                self._not_found()
                return
            name = rest[len("call/"):]
            try:
                args = json.loads(raw or b"{}").get("args", [])
            except (ValueError, AttributeError):
                args = []
            try:
                value = call(server.target, SPLASH_CALLS, name, args)
            except LookupError:
                self._not_found()
                return
            except Exception as exc:  # noqa: BLE001 (a button must never take the server down)
                log.warning("splash_call_failed", name=name, error_type=type(exc).__name__,
                            error=str(exc)[:200])
                value = None
            try:
                body = json.dumps({"value": value}).encode("utf-8")
            except (TypeError, ValueError):
                body = b'{"value": null}'
            self._send(HTTPStatus.OK, body, "application/json")

        def do_OTHER(self) -> None:  # noqa: N802
            self._not_found()

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_OTHER  # noqa: N815

    return Handler
