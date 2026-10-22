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


# ------------------------------------------------------------------ the join block (#28, #26)
def _join_block(monkeypatch) -> str:
    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    with _client() as c:
        r = c.post("/settings/lan/secret")
    assert r.status_code == 200
    return r.text


def test_join_block_puts_the_qr_on_a_fixed_light_ground(monkeypatch):
    """#28: the code is dark modules on a transparent SVG, so a dark page showed through it and
    phones could not read it. It now sits in .qr, whose ground is a token that is light in every
    scheme (never inverted: many scanners refuse an inverted code)."""
    body = _join_block(monkeypatch)
    assert '<div class="qr"' in body and "<svg" in body.split('<div class="qr"', 1)[1]


def test_join_block_secret_and_url_copy_with_one_click_and_are_not_links(monkeypatch):
    """#26: the secret and the join URL each have a Copy button. The URL is selectable text and
    never an anchor: a click would put the secret in the desktop browser's history."""
    body = _join_block(monkeypatch)
    assert body.count("navigator.clipboard.writeText($el.dataset.path)") == 2
    assert 'data-testid="copy-secret"' in body and 'data-testid="copy-join-url"' in body
    assert "<a " not in body and "href=" not in body
    url = body.split('data-testid="join-url">', 1)[1].split("</code>", 1)[0]
    assert url.startswith("http://192.168.1.20:") and "/auth?key=" in url
    secret = body.split('data-testid="lan-secret">', 1)[1].split("</code>", 1)[0]
    assert secret and url.endswith(secret)
    assert f'data-path="{secret}"' in body and f'data-path="{url}"' in body


def test_join_block_escapes_what_it_prints(monkeypatch):
    monkeypatch.setattr(auth, "generate_key", lambda: '"><script>x</script>')
    body = _join_block(monkeypatch)
    assert "<script>x</script>" not in body and "&lt;script&gt;" in body
    assert 'data-path=""><script' not in body


def test_join_block_without_a_lan_address_still_copies_the_secret(monkeypatch):
    monkeypatch.setattr(auth, "lan_host_ip", lambda: None)
    with _client() as c:
        body = c.post("/settings/lan/secret").text
    assert 'data-testid="copy-secret"' in body and 'class="qr"' not in body


def _secret_block(page: str) -> str:
    return page.split('id="secret">', 1)[1].split("<!-- /secret -->", 1)[0].removesuffix("</div>")


def test_settings_page_with_an_existing_secret_shows_the_same_block_as_generate(monkeypatch):
    """#26/#27: a key without a QR on launch was confusing. The page now renders the block the
    Generate/Rotate response renders, from one helper, so the two match."""
    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    with _client() as c:
        generated = c.post("/settings/lan/secret").text
        page = c.get("/settings").text
    block = _secret_block(page)
    assert '<div class="qr"' in block and "<svg" in block.split('<div class="qr"', 1)[1]
    assert 'data-testid="lan-secret"' in block and 'data-testid="copy-secret"' in block
    assert 'data-testid="join-url"' in block and 'data-testid="copy-join-url"' in block
    assert block.count("navigator.clipboard.writeText($el.dataset.path)") == 2
    assert "<a " not in block and "href=" not in block  # the join link is text, never an anchor
    assert " ".join(block.split()) == " ".join(generated.split())  # one shared renderer


def test_settings_page_without_a_secret_shows_no_block(monkeypatch):
    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    with _client() as c:
        block = _secret_block(c.get("/settings").text)
    assert "lan-secret" not in block and 'class="qr"' not in block


def test_lan_tab_is_short_and_keeps_every_safety_fact_inside_show_details():
    with _client() as c:
        page = c.get("/settings").text
    lan = page.split("<h2>LAN access</h2>", 1)[1].split("<h2", 1)[0]
    assert "—" not in lan and "\u2014" not in lan
    details = lan.split('<details data-testid="lan-details">', 1)[1].split("</details>", 1)[0]
    assert "<summary>Show details</summary>" in details
    for fact in ("no accounts", "plain HTTP", "What else the secret gates", "X-Kestrel-Key",
                 "browser extension", "restart", "port forwarder", "public"):
        assert fact in " ".join(details.split()), fact
    outside = lan.split('id="lan-bind"', 1)[0].replace(details, "")
    for long in ("What else the secret gates", "no accounts", "port forwarder", "bind_all"):
        assert long not in outside, long
    assert "plain HTTP" in outside and "shared password" in outside  # the short warning line
    assert len(outside.split()) < 260


# ------------------------------------------------- turning LAN access on from Settings (#27)
_HEADER_COMMENT = "# ---- Hosted, bring-your-own-key backends"


def _seed_settings(text: str | None = None):
    """The user's settings.yml in the isolated data home: the packaged copy, or `text`."""
    from kestrel import paths

    paths.ensure_dirs()
    target = paths.config_dir() / "settings.yml"
    if text is None:
        text = (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8")
    target.write_text(text, encoding="utf-8")
    return target


def _server(text: str) -> dict:
    import yaml

    return (yaml.safe_load(text) or {}).get("server") or {}


def test_set_bind_all_changes_one_key_and_keeps_every_comment(_isolate_data_home):
    import yaml

    from kestrel.config import get_settings
    from kestrel.services import lan_access

    target = _seed_settings()
    before_text = target.read_text(encoding="utf-8")
    before = yaml.safe_load(before_text)
    assert lan_access.set_bind_all(True) == target
    after_text = target.read_text(encoding="utf-8")
    assert _HEADER_COMMENT in after_text
    assert after_text.count("#") == before_text.count("#")  # not one comment lost
    after = yaml.safe_load(after_text)
    assert after["server"]["bind_all"] is True
    before["server"]["bind_all"] = True
    assert after == before  # nothing else changed
    assert get_settings().server.bind_all is True  # the loader sees it, no restart of the loader
    lan_access.set_bind_all(False)
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == yaml.safe_load(before_text)
    assert get_settings().server.bind_all is False


@pytest.mark.parametrize("text", [
    "llm:\n  mode: local\nserver:\n  host: \"127.0.0.1\"  # the address\n",  # no bind_all line
    "llm:\n  mode: local\n",  # no server block at all
    "llm:\n  mode: local\nserver:\n  bind_all: false   # keep this comment\n  host: x\n",
    "server: {host: x, bind_all: false}\nllm:\n  mode: local\n",  # flow style
])
def test_set_bind_all_handles_every_shape_of_the_file(_isolate_data_home, text):
    import yaml

    from kestrel.services import lan_access

    target = _seed_settings(text)
    lan_access.set_bind_all(True)
    out = target.read_text(encoding="utf-8")
    assert _server(out)["bind_all"] is True
    expected = yaml.safe_load(text)
    expected.setdefault("server", {})["bind_all"] = True
    assert yaml.safe_load(out) == expected
    for comment in ("# the address", "# keep this comment"):
        assert (comment in out) == (comment in text)


def test_a_missing_user_file_is_seeded_from_the_packaged_copy(_isolate_data_home):
    from kestrel import paths
    from kestrel.services import lan_access

    target = paths.config_dir() / "settings.yml"
    target.unlink(missing_ok=True)
    lan_access.set_bind_all(True)
    text = target.read_text(encoding="utf-8")
    assert _server(text)["bind_all"] is True and "backends:" in text and "tiers:" in text


def test_a_failed_write_leaves_the_file_as_it_was(_isolate_data_home, monkeypatch):
    import os

    from kestrel import paths
    from kestrel.services import lan_access

    target = _seed_settings()
    before = target.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(lan_access.LanAccessError):
        lan_access.set_bind_all(True)
    assert target.read_bytes() == before
    assert [p.name for p in paths.config_dir().iterdir() if "editing" in p.name] == []


def test_a_file_that_is_not_a_mapping_is_refused_not_overwritten(_isolate_data_home):
    from kestrel.services import lan_access

    target = _seed_settings("- not\n- a mapping\n")
    with pytest.raises(lan_access.LanAccessError):
        lan_access.set_bind_all(True)
    assert target.read_text(encoding="utf-8") == "- not\n- a mapping\n"


def _post(c, **data):
    return c.post("/settings/lan/bind", data=data)


def _on(c):
    c.post("/settings/lan/secret")
    return _post(c, bind_all="on", ack="on")


def test_enabling_needs_a_secret_and_the_acknowledgement(_isolate_data_home):
    target = _seed_settings()
    before = target.read_text(encoding="utf-8")
    with _client() as c:
        no_secret = _post(c, bind_all="on", ack="on")
        assert no_secret.status_code == 200
        assert "Generate the shared secret first" in no_secret.text
        assert target.read_text(encoding="utf-8") == before
        c.post("/settings/lan/secret")
        no_ack = _post(c, bind_all="on")
        assert "Tick the box" in no_ack.text
        assert target.read_text(encoding="utf-8") == before
        ok = _post(c, bind_all="on", ack="on")
    assert "Takes effect after restart" in ok.text
    assert _server(target.read_text(encoding="utf-8"))["bind_all"] is True


def test_only_this_machine_may_change_it_even_with_the_key(_isolate_data_home):
    target = _seed_settings()
    before = target.read_text(encoding="utf-8")
    with _client() as c:
        c.post("/settings/lan/secret")
        key = auth.cached_key()
    with _client("192.168.1.50") as phone:
        r = phone.post("/settings/lan/bind", data={"bind_all": "on", "ack": "on"},
                       headers={"X-Kestrel-Key": key})
    assert r.status_code == 403
    assert target.read_text(encoding="utf-8") == before


def test_turning_it_off_needs_no_acknowledgement_and_writes_the_file(_isolate_data_home):
    target = _seed_settings()
    with _client() as c:
        _on(c)
        off = _post(c)  # an unticked box posts nothing
    assert "Takes effect after restart" in off.text
    assert _server(target.read_text(encoding="utf-8"))["bind_all"] is False


def test_the_environment_variable_wins_and_the_page_says_so(_isolate_data_home, monkeypatch):
    from kestrel.config import get_settings

    target = _seed_settings()
    monkeypatch.setenv("KESTREL_SERVER__BIND_ALL", "false")
    with _client() as c:
        page = c.get("/settings").text
        assert "KESTREL_SERVER__BIND_ALL" in page.split('data-testid="lan-bind"')[1]
        r = _on(c)
    assert "KESTREL_SERVER__BIND_ALL" in r.text and "wins" in r.text
    assert _server(target.read_text(encoding="utf-8"))["bind_all"] is True  # the file is recorded
    assert get_settings().server.bind_all is False  # but the environment still decides


def test_the_settings_page_shows_the_control_and_the_state(_isolate_data_home):
    _seed_settings()
    with _client() as c:
        page = c.get("/settings").text
        assert 'data-testid="lan-bind"' in page and 'name="ack"' in page
        assert "not on this page" not in page
        assert "Off" in page.split('data-testid="lan-bind-state"')[1][:200]
        c.post("/settings/lan/secret")
        _post(c, bind_all="on", ack="on")
        after = c.get("/settings").text
    state = after.split('data-testid="lan-bind-state"')[1][:600]
    assert "On" in state and "restart" in state


def test_bind_host_and_both_allowlists_agree_after_the_write(_isolate_data_home, monkeypatch):
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    from kestrel.web import app as app_module
    from kestrel.web import serve

    monkeypatch.setattr(app_module, "local_addresses", lambda: ["127.0.0.1", "192.168.1.20"])
    _seed_settings()

    def hosts() -> list[str]:
        app = app_module.build_app(worker=False)
        mw = next(m for m in app.user_middleware if m.cls is TrustedHostMiddleware)
        options = getattr(mw, "kwargs", None) or mw.options
        return list(options["allowed_hosts"])

    assert serve.bind_host() == "127.0.0.1" and "192.168.1.20" not in hosts()
    assert app_module._is_own_origin("http://192.168.1.20:8765", bind_all=False) is False
    with _client() as c:
        _on(c)
    # After the write every reader of bind_all, with no code path left on the old value:
    from kestrel.config import get_settings

    assert get_settings().server.bind_all is True
    assert serve.bind_host() == "0.0.0.0"
    assert "192.168.1.20" in hosts()
    assert app_module._is_own_origin(
        "http://192.168.1.20:8765", bind_all=get_settings().server.bind_all) is True
    assert app_module._is_own_origin("http://evil.example", bind_all=True) is False
    with _client() as c:
        _post(c)  # off again
    assert serve.bind_host() == "127.0.0.1" and "192.168.1.20" not in hosts()


def test_help_no_longer_says_to_edit_the_file_by_hand(_isolate_data_home):
    with _client() as c:
        help_text = c.get("/help").text
    lan = help_text.split('id="lan"')[1].split("</div>")[0]
    assert "Settings" in lan and "setting" in lan.lower()
    assert "settings.yml" not in lan.split("Turning it on")[1].split("</p>")[0]


def test_a_hostile_page_cannot_flip_the_switch(_isolate_data_home):
    """The new write sits behind the same Origin and Host guards as every other POST."""
    target = _seed_settings()
    before = target.read_text(encoding="utf-8")
    with _client() as c:
        c.post("/settings/lan/secret")
        data = {"bind_all": "on", "ack": "on"}
        cross = c.post("/settings/lan/bind", data=data, headers={"Origin": "https://evil.example"})
        rebound = c.post("/settings/lan/bind", data=data, headers={"Host": "evil.example"})
    assert cross.status_code == 403 and rebound.status_code == 400
    assert target.read_text(encoding="utf-8") == before
