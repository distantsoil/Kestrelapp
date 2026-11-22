"""The Content-Security-Policy (D-INST-8, D-SEC-56): one exact policy on every response.

The route crawler (tests/test_route_crawl.py) holds every route in the route table to the header.
These tests cover the answers no route makes: Starlette's own 500, TrustedHostMiddleware's 400,
the static mount, a 404, a 405, a redirect, a streamed body, a file download, and a response that
tried to send a policy of its own. Invented text only; no network.
"""

from __future__ import annotations

import pytest
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient

from kestrel.web.csp import POLICY

# Written out, not built from kestrel.web.csp.DIRECTIVES: a change to the policy has to change
# this line too (the issue's policy, #212).
EXPECTED = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)


def _app(tmp_path):
    from kestrel.web.app import build_app

    app = build_app(worker=False)
    target = tmp_path / "invented.txt"
    target.write_text("Invented download", encoding="utf-8")

    @app.get("/__csp/boom")
    async def boom():
        raise RuntimeError("invented failure")

    @app.get("/__csp/redirect")
    async def redirect():
        return RedirectResponse("/", status_code=303)

    @app.get("/__csp/own-policy")
    async def own_policy():
        return HTMLResponse("<p>Invented</p>", headers={
            "Content-Security-Policy": "default-src *; script-src 'unsafe-inline'"})

    @app.get("/__csp/stream")
    async def stream():
        async def body():
            yield b"Invented "
            yield b"stream"
        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/__csp/download")
    async def download():
        return FileResponse(target, filename="invented.txt")

    return app


def _policies(r) -> list[str]:
    return r.headers.get_list("content-security-policy")


def test_the_policy_is_the_issues_policy():
    assert POLICY == EXPECTED
    for banned in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "*", "http:", "https:"):
        assert banned not in POLICY, banned


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/__csp/boom", {"Accept": "text/html"}, 500),  # the calm error page
        ("GET", "/__csp/boom", {"Accept": "application/json"}, 500),  # Starlette's own answer
        ("GET", "/__csp/redirect", {}, 303),
        ("GET", "/__csp/own-policy", {}, 200),  # replaced, never combined
        ("GET", "/__csp/stream", {}, 200),
        ("GET", "/__csp/download", {}, 200),
        ("GET", "/no-such-page", {"Accept": "text/html"}, 404),
        ("GET", "/static/css/components.css", {}, 200),
        ("GET", "/static/js/htmx.min.js", {}, 200),
        ("GET", "/static/no-such-file.css", {}, 404),
        ("HEAD", "/static/css/tokens.css", {}, 200),
        ("PUT", "/static/css/tokens.css", {}, 405),
        ("GET", "/", {"Host": "evil.example"}, 400),  # TrustedHostMiddleware's answer
        ("POST", "/api/quit", {"Origin": "http://evil.example"}, 403),  # crawl allowlist route
        ("POST", "/api/focus", {"Origin": "http://evil.example"}, 403),  # crawl allowlist route
    ],
)
def test_answers_no_route_makes_carry_exactly_the_policy(method, path, headers, status,
                                                         tmp_path):
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        r = client.request(method, path, headers=headers, follow_redirects=False)
    assert r.status_code == status, (method, path, r.status_code, r.text[:200])
    assert _policies(r) == [EXPECTED], (method, path, _policies(r))


def test_htmx_partials_carry_the_policy(tmp_path):
    from kestrel.db import migrate

    migrate.run_migrations()
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        r = client.get("/status/rail", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert _policies(r) == [EXPECTED]


def test_print_stylesheets_shown_in_the_app_become_same_origin_links(tmp_path):
    """D-SEC-59: a srcdoc frame inherits the page's policy, so a print document's <style> is
    served as /frame-css/<sha256>.css, the same text, and only text this process rendered."""
    import hashlib

    from kestrel.web.csp import frame_safe

    css = "body { color: #111; } .invented { margin: 0; }"
    page = f"<html><head><style>{css}</style><style>p {{ margin: 0; }}</style></head>" \
           "<body><p>Invented &lt;style&gt; text</p></body></html>"
    out = frame_safe(page)
    assert "<style" not in out and "&lt;style&gt;" in out
    digest = hashlib.sha256(css.encode()).hexdigest()
    assert f'<link rel="stylesheet" href="/frame-css/{digest}.css">' in out
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as client:
        r = client.get(f"/frame-css/{digest}.css")
        assert r.status_code == 200 and r.text == css
        assert r.headers["content-type"].startswith("text/css")
        assert _policies(r) == [EXPECTED]
        unknown = client.get("/frame-css/" + "0" * 64 + ".css")
        assert unknown.status_code == 404 and _policies(unknown) == [EXPECTED]
        assert client.get(f"/frame-css/{digest}.txt").status_code == 404
        assert client.get("/frame-css/..%2F..%2Fsecret.css").status_code == 404


def _splash_server(target=None):
    from kestrel.desktop.splash_server import SplashServer

    return SplashServer(target).start()


def _get(url, headers=None):
    import urllib.request

    req = urllib.request.Request(url, headers=headers or {})  # noqa: S310 (loopback test server)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 (loopback test server)
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def _post(url, body=b'{"args": []}', headers=None):
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})  # noqa: S310 (loopback test server)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 (loopback test server)
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class _Shell:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def splash_retry(self) -> None:
        self.calls.append("splash_retry")

    def window_info(self) -> dict:
        self.calls.append("window_info")
        return {"os": "win", "maximized": False}

    def _private(self) -> None:
        self.calls.append("_private")


def test_the_loading_page_server_answers_with_exactly_the_app_policy():
    """D-SEC-61 (round 4): the loading page, its files and its channel all carry the app's exact
    policy; anything else on that server is a 404 that carries it too."""
    import json

    srv = _splash_server(_Shell())
    try:
        for path in ("", "static/css/tokens.css", "static/css/splash.css", "static/js/splash.js",
                     "static/icon.svg", "static/fonts/manrope/manrope-latin-wght-normal.woff2",
                     "events?after=-1"):
            status, headers, _ = _get(srv.url + path)
            assert status == 200, path
            assert headers["Content-Security-Policy"] == EXPECTED, path
        status, headers, body = _get(srv.url + "events?after=-1")
        assert json.loads(body) == {"seq": 0, "events": []}
        for path in ("static/css/components.css", "static/../splash.html", "nope"):
            status, headers, _ = _get(srv.url + path)
            assert status == 404 and headers["Content-Security-Policy"] == EXPECTED, path
        status, _, _ = _get(srv.origin + "/s/not-the-token/")
        assert status == 404
    finally:
        srv.stop()


def test_the_loading_page_server_runs_only_allowed_calls_from_the_page_itself():
    import json

    shell = _Shell()
    srv = _splash_server(shell)
    try:
        status, body = _post(srv.url + "call/splash_retry",
                             headers={"Origin": srv.origin, "Sec-Fetch-Site": "same-origin"})
        assert status == 200 and json.loads(body) == {"value": None}
        status, body = _post(srv.url + "call/window_info")
        assert json.loads(body)["value"] == {"os": "win", "maximized": False}
        assert shell.calls == ["splash_retry", "window_info"]
        for name in ("_private", "window_close_x", "__init__"):
            assert _post(srv.url + f"call/{name}")[0] == 404, name
        # Another site's page, a cross-site fetch, or the wrong Host never reach the shell.
        assert _post(srv.url + "call/splash_retry", headers={"Origin": "http://evil.example"})[0] \
            == 404
        assert _post(srv.url + "call/splash_retry", headers={"Sec-Fetch-Site": "cross-site"})[0] \
            == 404
        assert _post(srv.url + "call/splash_retry", headers={"Host": "evil.example"})[0] == 404
        assert _post(srv.origin + "/s/wrong/call/splash_retry")[0] == 404
        assert shell.calls == ["splash_retry", "window_info"]
    finally:
        srv.stop()


def test_the_loading_page_gets_every_update_in_order_and_waits_for_the_next():
    import json
    import threading
    import time

    srv = _splash_server(_Shell())
    try:
        srv.channel.post("say", "Opening your data", 30)
        srv.channel.post("fail", "Stopped.", "Step: x", "migrations_failed", "STA-004")
        _, _, body = _get(srv.url + "events?after=0")
        got = json.loads(body)
        assert got["seq"] == 2 and [e[0] for e in got["events"]] == ["say", "fail"]
        assert got["events"][1][1] == ["Stopped.", "Step: x", "migrations_failed", "STA-004"]
        threading.Timer(0.3, lambda: srv.channel.post("ready")).start()
        started = time.monotonic()
        _, _, body = _get(srv.url + "events?after=2")
        assert json.loads(body)["events"] == [["ready", []]]
        assert 0.2 < time.monotonic() - started < 5
        with pytest.raises(ValueError):
            srv.channel.post("eval", "anything")  # only the page's own names
    finally:
        srv.stop()
