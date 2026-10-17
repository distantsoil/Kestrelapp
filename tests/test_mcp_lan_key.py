"""The MCP port carries the same three request-path controls as the web app.

Under `server.bind_all`, the first cut of sse_app passed allowed_hosts=["*"] and
allowed_origins=["*"], bound 0.0.0.0, and checked no credential: any LAN device (and a DNS-rebound
page in the operator's own browser) got every tool, including trigger_scan and the action tools.
These pin the fix: Host/Origin are pinned to local_addresses() as app.py does, and a non-loopback
peer must present the LAN shared key in X-Kestrel-Key. Loopback stays exempt, like the web app.

The SSE stream itself is not driven here (it never ends); the checks use POST /messages/, which
passes through the same middleware stack and answers without streaming.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kestrel.mcp.server import RequireLanKey, sse_app
from kestrel.web import auth


@pytest.fixture(autouse=True)
def _reset_key():
    yield
    auth.reset_cache()


def _client(app, peer: str) -> TestClient:
    return TestClient(
        app, base_url="http://127.0.0.1", client=(peer, 40000), raise_server_exceptions=False
    )


def test_sse_app_is_wrapped_in_the_key_middleware_even_on_loopback_bind():
    # Installed unconditionally so that flipping bind_all can never expose an unkeyed MCP port.
    assert isinstance(sse_app(), RequireLanKey)
    assert isinstance(sse_app(bind_all=True), RequireLanKey)


def test_lan_peer_without_the_key_is_refused():
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "192.168.1.50") as c:
        r = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"})
    assert r.status_code == 401
    assert "shared secret" in r.json()["detail"]


def test_lan_peer_with_a_wrong_key_is_refused():
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "192.168.1.50") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"X-Kestrel-Key": "wrong"},
        )
    assert r.status_code == 401


def test_lan_peer_with_the_key_reaches_the_transport():
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "192.168.1.50") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"X-Kestrel-Key": "mcp-secret"},
        )
    # Past the key check: whatever the transport says about an unknown session, it is not 401.
    assert r.status_code != 401


def test_lan_peer_is_refused_when_no_secret_exists_yet():
    # Fails closed: an exposed MCP port with no key generated is refused, not left open.
    auth.set_cached_key(None)
    with _client(sse_app(bind_all=True), "192.168.1.50") as c:
        r = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"})
    assert r.status_code == 401
    assert "no shared secret has been generated" in r.json()["detail"]


def test_loopback_peer_needs_no_key():
    # A local MCP client (Claude Desktop, the CLI) works with no key, exactly like the web UI.
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        r = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"})
    assert r.status_code != 401


def test_rebound_host_is_refused_under_bind_all():
    # A DNS-rebound request arrives on loopback, where the key is exempt, so the Host pin is the
    # control that has to hold. allowed_hosts=["*"] switched it off.
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Host": "evil.example"},
        )
    assert r.status_code == 421


def test_rebound_host_is_refused_on_the_loopback_bind_too():
    # fastmcp 4.x's SSE transport ignores http_app's allowed_hosts/host_origin_protection, so the
    # loopback-only port had no Host guard at all until the guard was installed explicitly.
    with _client(sse_app(), "127.0.0.1") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Host": "evil.example"},
        )
    assert r.status_code == 421


def test_foreign_origin_is_refused_under_bind_all():
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Origin": "http://evil.example"},
        )
    assert r.status_code == 403


def test_own_address_and_own_origin_are_accepted_under_bind_all():
    # The point of LAN mode: the machine's own address, on any port, still works.
    own = [a for a in auth.local_addresses() if a.count(".") == 3][:1] or ["127.0.0.1"]
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        for host in ("127.0.0.1:9110", f"{own[0]}:9110"):
            r = c.post(
                "/messages/?session_id=abc",
                json={"jsonrpc": "2.0", "method": "ping"},
                headers={"Host": host, "Origin": f"http://{host}"},
            )
            assert r.status_code not in (403, 421), f"own address {host} was refused"
