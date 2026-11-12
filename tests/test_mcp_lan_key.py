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
    # D-SEC-10: a foreign Host from a loopback peer is remote, so it needs the key (401 without);
    # with the key the Host guard still refuses it (421).
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Host": "evil.example"},
        )
        keyed = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Host": "evil.example", "X-Kestrel-Key": "mcp-secret"},
        )
    assert r.status_code == 401
    assert keyed.status_code == 421


def test_rebound_host_is_refused_on_the_loopback_bind_too():
    # fastmcp 4.x's SSE transport ignores http_app's allowed_hosts/host_origin_protection, so the
    # loopback-only port had no Host guard at all until the guard was installed explicitly.
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(), "127.0.0.1") as c:
        r = c.post(
            "/messages/?session_id=abc",
            json={"jsonrpc": "2.0", "method": "ping"},
            headers={"Host": "evil.example", "X-Kestrel-Key": "mcp-secret"},
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


# ---- D-SEC-10: the MCP loopback exemption is the web app's decision, not a copy of it
@pytest.mark.parametrize("header", [
    {"X-Forwarded-For": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"},
    {"Cf-Connecting-Ip": "203.0.113.9"}, {"X-Real-Ip": "203.0.113.9"},
])
def test_a_loopback_peer_with_a_proxy_header_is_a_remote_visitor_and_needs_the_key(header):
    """A tunnel or reverse proxy on this machine connects from 127.0.0.1 and forwards the real
    client in a header. It is remote, so the key is required, like on the web app."""
    auth.set_cached_key("mcp-secret")
    with _client(sse_app(bind_all=True), "127.0.0.1") as c:
        r = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"},
                   headers=header)
        ok = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"},
                    headers={**header, "X-Kestrel-Key": "mcp-secret"})
    assert r.status_code == 401
    assert ok.status_code != 401


def test_the_mcp_server_and_the_web_app_share_one_loopback_decision():
    assert auth.peer_is_loopback("127.0.0.1", {"host": "localhost:9110"}) is True
    assert auth.peer_is_loopback("127.0.0.1", {"host": "tunnel.invalid.example"}) is False
    proxied = {"host": "127.0.0.1", "x-real-ip": "1.2.3.4"}
    assert auth.peer_is_loopback("127.0.0.1", proxied) is False
    assert auth.peer_is_loopback("192.168.1.9", {"host": "127.0.0.1"}) is False
    assert auth.peer_is_loopback("127.0.0.1", {"host": "localhost"}, ("localhost",)) is False


def test_the_mcp_guard_and_the_web_guard_read_one_hostname_source_live(monkeypatch):
    """A settings change reaches both guards the same way, with no restart: the web app used to
    keep a copy taken when it was built, the MCP server read the settings on every request."""
    from starlette.requests import Request

    def web_says_local(host: str) -> bool:
        scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
                 "headers": [(b"host", host.encode())], "client": ("127.0.0.1", 5), "server": None,
                 "scheme": "http"}
        return auth.is_loopback(Request(scope))

    def mcp_says_local(host: str) -> bool:
        auth.set_cached_key("mcp-secret")
        with _client(sse_app(bind_all=True), "127.0.0.1") as c:
            r = c.post("/messages/?session_id=abc", json={"jsonrpc": "2.0", "method": "ping"},
                       headers={"Host": host})
        return r.status_code != 401

    # A declared public name is never this computer, even when it is a name this computer answers
    # to (a tunnel that forwards "localhost" is the case the list exists for).
    assert auth.public_hostnames() == ()
    assert web_says_local("localhost") and mcp_says_local("localhost")
    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["Localhost"]')
    assert auth.public_hostnames() == ("localhost",)
    assert not web_says_local("localhost") and not mcp_says_local("localhost")
    # The list changes: both follow it at once, with no restart.
    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["tunnel-b.example.com"]')
    assert web_says_local("localhost") and mcp_says_local("localhost")
