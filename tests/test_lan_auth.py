"""LAN shared-key access control (AFTERACTION 3.3, SPEC-INTERFACES 2.3).

Before this, Settings rendered a "Generate shared secret" button whose copy claimed the secret
gated the non-loopback bind. Nothing read the secret, no middleware existed, and the server bound
127.0.0.1 unconditionally: a fictional feature. These tests pin the real behavior.

The four properties CONTINUE.md requires: a non-loopback request without the key is refused,
/health is exempt, the extension's header path works, and loopback still works with no key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.web import auth


@pytest.fixture(autouse=True)
def _reset_key():
    """The key is process-cached, so a test must not leak its key into the next one.

    Restores via reset_cache() rather than set_cached_key(previous): the latter forces loaded=True
    process-wide even if the cache started unloaded, which would leak cache STATE into later test
    modules and mask a lazy-load bug there.
    """
    yield
    auth.reset_cache()


@pytest.fixture(autouse=True)
def _migrated_db():
    """Give the app a real database.

    Without this every allowed request 500s on an empty schema, and an assertion of `!= 401` is
    then satisfied by the server error. That made the ENTIRE allow-path half of this file vacuous:
    all 24 tests passed with the middleware rewritten to refuse a VALID key, because none of them
    could tell 200 from 500. Every allow-path assertion below now demands 200.
    """
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine

    migrate.run_migrations()
    yield
    forget_engine()


def _client(client_host: str = "127.0.0.1") -> TestClient:
    """A TestClient whose requests appear to come from `client_host`.

    Starlette's TestClient reports 'testclient' as the peer by default, which is neither loopback
    nor a LAN address, so the loopback exemption has to be driven explicitly to test either side.
    """
    from kestrel.web.app import build_app

    return TestClient(
        build_app(worker=False),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        client=(client_host, 12345),
    )


# ------------------------------------------------------------------ the four required properties


def test_non_loopback_without_the_key_is_refused():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/opportunities")
    assert r.status_code == 401
    assert "shared secret" in r.json()["detail"]


def test_health_is_exempt_so_discovery_and_readiness_still_work():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "kestrel"


def test_the_extension_header_path_works():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/opportunities", headers={"X-Kestrel-Key": "s3cret-key"})
    assert r.status_code == 200, "a VALID key must be let through, not merely not-401"


def test_loopback_still_works_with_no_key_at_all():
    # The operator at the machine is already past every boundary this key could enforce, and
    # requiring it here would break the desktop shell's own webview, the CLI, and the MCP server.
    auth.set_cached_key(None)
    with _client("127.0.0.1") as c:
        r = c.get("/opportunities")
    assert r.status_code == 200, "loopback must still be served, not merely not-401"


# --------------------------------------------------------------------------- failing closed


def test_non_loopback_is_refused_when_no_secret_exists_yet():
    """Fails CLOSED. If bind_all is on but no secret has been generated, the alternative is an
    unprotected LAN-exposed app that looks protected."""
    auth.set_cached_key(None)
    with _client("192.168.1.50") as c:
        r = c.get("/opportunities")
    assert r.status_code == 401
    assert "no shared secret has been generated" in r.json()["detail"]


def test_a_wrong_key_is_refused():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/opportunities", headers={"X-Kestrel-Key": "not-the-key"})
    assert r.status_code == 401


def test_writes_are_refused_too_not_only_reads():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.post("/api/scan/now")
    assert r.status_code == 401


# ------------------------------------------------------------------------------ the /auth join


def test_auth_with_a_good_key_sets_a_cookie_and_redirects():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/auth?key=s3cret-key", follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers.get("set-cookie", "")
    assert auth.COOKIE_NAME in cookie
    assert "HttpOnly" in cookie, "the key must not be readable by page JavaScript"
    assert "lax" in cookie.lower(), "a cross-site POST must not ride on this cookie"


def test_the_cookie_from_auth_then_authorizes_later_requests():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        c.get("/auth?key=s3cret-key", follow_redirects=False)
        r = c.get("/opportunities")  # no header this time: the cookie carries it
    assert r.status_code == 200, "the cookie set by /auth must actually authorize"


def test_auth_with_a_bad_key_does_not_set_a_cookie():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        r = c.get("/auth?key=wrong", follow_redirects=False)
    assert r.status_code == 401
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_auth_refuses_to_redirect_off_site():
    """An open redirect here would let a crafted link bounce the operator elsewhere while looking
    like a Kestrel URL, and it would do so from a page that just set their key cookie."""
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        for hostile in ("https://evil.example", "//evil.example"):
            r = c.get(f"/auth?key=s3cret-key&next={hostile}", follow_redirects=False)
            assert r.headers["location"] == "/"


# ------------------------------------------------------------------------------- key handling


def test_key_comparison_is_constant_time_and_rejects_empties():
    assert auth.key_is_valid("abc", "abc") is True
    assert auth.key_is_valid("abc", "abd") is False
    assert auth.key_is_valid(None, "abc") is False
    assert auth.key_is_valid("abc", None) is False
    assert auth.key_is_valid("", "") is False  # an unset secret must never authorize anything


def test_generated_keys_are_long_and_unique():
    keys = {auth.generate_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) >= 32 for k in keys)


def test_rotating_the_key_locks_out_the_old_one():
    # The only revocation a single shared credential can offer, so it has to actually work.
    auth.set_cached_key("old-key")
    with _client("192.168.1.50") as c:
        assert c.get("/opportunities", headers={"X-Kestrel-Key": "old-key"}).status_code == 200
        auth.set_cached_key("new-key")
        assert c.get("/opportunities", headers={"X-Kestrel-Key": "old-key"}).status_code == 401
        assert c.get("/opportunities", headers={"X-Kestrel-Key": "new-key"}).status_code == 200


# ------------------------------------------------------------------------------ bind + QR bits


def test_bind_host_is_loopback_unless_bind_all_is_set(monkeypatch):
    from kestrel.web import serve

    assert serve.bind_host() == "127.0.0.1"

    class _S:
        server = type("x", (), {"bind_all": True})()

    monkeypatch.setattr("kestrel.config.get_settings", lambda: _S())
    assert serve.bind_host() == "0.0.0.0"  # noqa: S104 - the point of the test


def test_running_server_pins_loopback_even_under_bind_all(monkeypatch):
    """running_server backs tests and short-lived in-process servers. On a machine with bind_all
    on it must NOT open a LAN-visible socket. Asserted by capturing what running_server actually
    asks for, rather than by passing the answer in, which would test nothing."""
    from kestrel.web import serve

    captured: dict = {}
    real = serve.bind_free_port

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **{**kwargs, "host": "127.0.0.1"})

    monkeypatch.setattr(serve, "bind_free_port", _spy)
    with serve.running_server(worker=False):
        pass
    assert captured.get("host") == "127.0.0.1", f"running_server did not pin loopback: {captured}"
    assert captured.get("ladder") is False, "running_server must not consume a ladder rung"


def test_join_url_encodes_the_key():
    url = auth.join_url("192.168.1.10", 8765, "a+b/c=")
    assert url.startswith("http://192.168.1.10:8765/auth?key=")
    assert "+" not in url.split("key=")[1] and "/" not in url.split("key=")[1]


def test_qr_svg_renders_or_degrades_quietly():
    svg = auth.qr_svg("http://192.168.1.10:8765/auth?key=abc")
    # A missing QR must degrade to "type the URL", never break the Settings page.
    assert svg == "" or svg.lstrip().startswith("<svg")


class TestBindAllDoesNotDisableTheOtherControls:
    """The regression that made LAN mode dangerous.

    The first cut widened TrustedHostMiddleware to allowed_hosts=["*"] and made any http(s) origin
    count as our own whenever bind_all was set. Since a DNS-rebound request also arrives on
    loopback, where the shared key is exempt by design, turning on LAN access switched off all
    three controls at once: any page the operator visited could rebind to 127.0.0.1, read /settings
    (which renders the shared secret in clear text), and drive state-changing POSTs.
    """

    @staticmethod
    def _lan_app(monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("KESTREL_SERVER__BIND_ALL", "true")
        from kestrel.config import get_settings
        from kestrel.web.app import build_app

        get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
        assert get_settings().server.bind_all is True, "bind_all did not take effect"
        return TestClient(
            build_app(worker=False),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 1),
            raise_server_exceptions=False,
        )

    def test_rebinding_is_still_refused_with_bind_all_on(self, monkeypatch):
        with self._lan_app(monkeypatch) as c:
            r = c.get("/settings", headers={"Host": "evil.example"})
        assert r.status_code == 400, "DNS rebinding reads /settings, which shows the shared secret"

    def test_cross_origin_writes_are_still_refused_with_bind_all_on(self, monkeypatch):
        with self._lan_app(monkeypatch) as c:
            r = c.post("/api/scan/now", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_the_machines_own_address_is_still_accepted(self, monkeypatch):
        # The point of LAN mode: the phone must still reach it by this machine's address.
        own = auth.local_addresses()
        with self._lan_app(monkeypatch) as c:
            for host in ("127.0.0.1", *[a for a in own if a.count(".") == 3][:1]):
                r = c.get("/health", headers={"Host": host})
                assert r.status_code == 200, f"own address {host} was refused"


def test_rotation_wins_over_an_in_flight_load():
    """Rotation is the ONLY revocation a single shared credential offers, so a stale read must
    never resurrect a revoked key.

    This drives the REAL race: _load_key_once is entered, and the rotation lands while it is
    awaiting the database read. An earlier version of this test only called reset_cache() then
    set_cached_key() and asserted the value it had just set, so deleting the generation guard
    entirely left it green. It now actually awaits the loader with a rotation injected mid-flight.
    """
    import asyncio

    from kestrel.db import migrate
    from kestrel.db.models import Setting
    from kestrel.web import auth as auth_mod

    async def _run():
        migrate.run_migrations()
        auth_mod.reset_cache()

        real_get_sessionmaker = None

        class _SlowSession:
            """Yields the OLD stored key, but only after a rotation has landed."""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, model, key):
                # The rotation happens DURING the await, which is the whole point.
                auth_mod.set_cached_key("rotated-new-key")
                await asyncio.sleep(0)
                row = Setting(key=key, value="stale-old-key")
                return row

        import kestrel.db.engine as engine_mod

        real_get_sessionmaker = engine_mod.get_sessionmaker
        engine_mod.get_sessionmaker = lambda: (lambda: _SlowSession())
        try:
            return await auth_mod._load_key_once()
        finally:
            engine_mod.get_sessionmaker = real_get_sessionmaker

    result = asyncio.run(_run())
    assert result == "rotated-new-key", "a stale read overwrote a rotation and revived a dead key"
    assert auth.cached_key() == "rotated-new-key"


def test_proxy_headers_are_disabled_so_a_header_cannot_forge_loopback():
    """uvicorn defaults proxy_headers=True and trusts FORWARDED_ALLOW_IPS to rewrite
    request.client from X-Forwarded-For. Since the loopback exemption keys off request.client, an
    ambient env var could otherwise switch this whole control off."""
    from kestrel.web.serve import _make_server

    server = _make_server(object(), log_level="warning")
    assert server.config.proxy_headers is False
    # The QR join is GET /auth?key=<secret>, and uvicorn's access log writes the query verbatim to
    # stderr, which the packaged build redirects to a file. No secrets in logs.
    assert server.config.access_log is False


def test_x_forwarded_for_cannot_forge_loopback():
    auth.set_cached_key("s3cret-key")
    with _client("192.168.1.50") as c:
        for header in ("X-Forwarded-For", "X-Real-IP"):
            r = c.get("/opportunities", headers={header: "127.0.0.1"})
            assert r.status_code == 401, f"{header} forged the loopback exemption"
