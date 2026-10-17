"""Extension port discovery (AFTERACTION 3.9), cloud-safe: real sockets on loopback, no browser.

The browser extension cannot read runtime.json, because an MV3 service worker has no filesystem
access. Its only way to find a running Kestrel is to probe a known ladder of ports and confirm the
thing that answers identifies itself as Kestrel. Both halves of that contract are asserted here:
the app must actually claim a ladder port, and /health must carry the identity marker.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from kestrel.web import serve

# _isolate_data_home is autouse in conftest.py, so it is not restated here.


@pytest.fixture(autouse=True)
def _reset_key():
    """The LAN key is process-cached; the extension-origin tests set it and must not leak it."""
    from kestrel.web import auth

    yield
    auth.reset_cache()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _occupy(port: int) -> socket.socket | None:
    """Hold a port so the ladder must step over it. None if it is already taken."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


def test_health_carries_the_kestrel_identity_marker():
    # The extension POSTs the operator's page text, so it must positively identify Kestrel before
    # sending anything. Without this marker it cannot tell Kestrel from any other local server.
    with _client() as c:
        body = c.get("/health").json()
    assert body["service"] == "kestrel"
    assert body["status"] == "ok"


def _synthetic_ladder(n: int) -> tuple[list[int], list[socket.socket]]:
    """Reserve n consecutive-ish free ports this test owns, then release them.

    Tests assert against THIS ladder rather than the real 8765 one, so they neither depend on what
    else is listening on the operator's machine nor transiently deny a real rung to a Kestrel that
    happens to be launching. Returns the ports and the still-open holders of the ports it did not
    hand back, so the caller can decide what to occupy.
    """
    holders: list[socket.socket] = []
    ports: list[int] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    base = probe.getsockname()[1]
    probe.close()
    # Stay inside the valid port range: bind() raises OverflowError (NOT an OSError) above 65535.
    for candidate in range(base, min(base + 60, 65536)):
        held = _occupy(candidate)
        if held is not None:
            holders.append(held)
            ports.append(candidate)
        if len(ports) == n:
            break
    return ports, holders


def test_app_claims_a_ladder_port_so_the_extension_can_find_it(monkeypatch):
    # Asserted against a ladder the test owns, so a regression to ephemeral-only FAILS here instead
    # of skipping because the machine happened to be busy.
    ladder, holders = _synthetic_ladder(2)
    if len(ladder) < 2:
        pytest.skip("could not reserve a synthetic ladder")
    for h in holders:
        h.close()  # free them so the binder can take one
    monkeypatch.setattr(serve, "PORT_LADDER", tuple(ladder))
    sock, port = serve.bind_free_port()
    try:
        assert port == ladder[0], "must claim the first free rung, not an ephemeral port"
    finally:
        sock.close()


def test_ladder_steps_over_a_taken_rung(monkeypatch):
    ladder, holders = _synthetic_ladder(2)
    if len(ladder) < 2:
        pytest.skip("could not reserve a synthetic ladder")
    holders[1].close()  # rung 1 free, rung 0 still held by this test
    try:
        monkeypatch.setattr(serve, "PORT_LADDER", tuple(ladder))
        sock, port = serve.bind_free_port()
        try:
            # Exactly the next rung: proves it stepped over, not fell through to ephemeral.
            assert port == ladder[1]
        finally:
            sock.close()
    finally:
        holders[0].close()


def test_falls_back_to_ephemeral_when_every_rung_is_taken(monkeypatch):
    ladder, holders = _synthetic_ladder(2)
    if len(ladder) < 2:
        pytest.skip("could not reserve a synthetic ladder")
    try:
        # Every rung stays held, so the binder cannot take one.
        monkeypatch.setattr(serve, "PORT_LADDER", tuple(ladder))
        sock, port = serve.bind_free_port()
        try:
            # It must still come up rather than fail to serve, and must not have stolen a rung
            # another socket is holding. No ordering assumption about the ephemeral port itself:
            # the OS may hand back anything, and asserting a range is pure flake.
            assert port not in ladder
            assert sock.getsockname()[1] == port
        finally:
            sock.close()
    finally:
        for h in holders:
            h.close()


def test_shipped_head_revision_is_readable_without_a_database():
    # doctor compares this against the operator's applied revision. If it ever returns None on a
    # healthy tree, doctor silently stops reporting a behind database and the false PASS is back.
    from kestrel.db import migrate

    assert migrate.head_revision() is not None


def test_head_revision_reports_none_instead_of_raising_on_a_broken_tree(monkeypatch, tmp_path):
    # Missing migrations is precisely the frozen-packaging failure doctor exists to diagnose, so
    # doctor must report it rather than traceback. alembic raises CommandError here, which does not
    # derive from OSError/RuntimeError/ValueError and so has to be caught by name.
    import contextlib

    from kestrel.db import migrate

    missing = tmp_path / "no-migrations-here"

    @contextlib.contextmanager
    def _missing(_arg):
        yield str(missing)

    monkeypatch.setattr(migrate.paths, "resource_path", _missing)
    assert migrate.head_revision() is None


class TestCrossOriginGuard:
    """A third-party page must not be able to drive the app now that its port is predictable.

    These assert the MIDDLEWARE decision, so they use raise_server_exceptions=False: what matters
    is whether the request was refused at the door (403) or handed to the route, not whether the
    route then succeeded against a database this test never seeded.
    """

    @staticmethod
    def _c() -> TestClient:
        from kestrel.web.app import build_app

        return TestClient(build_app(worker=False), raise_server_exceptions=False)

    def test_cross_origin_unsafe_request_is_refused(self):
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": "https://evil.example"})
        # Without this the page could trigger real outbound scrapes from the operator's IP.
        assert r.status_code == 403
        assert "cross-origin" in r.json()["detail"]

    def test_same_origin_ui_still_works(self):
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": "http://127.0.0.1:8765"})
        assert r.status_code != 403

    def test_any_ladder_rung_counts_as_same_origin(self):
        # The app moves along the ladder, so the UI's own Origin carries whichever rung it took.
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": "http://localhost:8771"})
        assert r.status_code != 403

    def test_extension_origin_with_the_shared_key_works(self):
        # The operator installed the extension deliberately and it is the documented capture
        # path, but the SCHEME names every extension with a localhost permission, not that one.
        # The Kestrel extension identifies itself with the shared key.
        from kestrel.web import auth

        auth.set_cached_key("ext-key")
        with self._c() as c:
            r = c.post(
                "/api/scan/now",
                headers={"Origin": "chrome-extension://abcdef", "X-Kestrel-Key": "ext-key"},
            )
        assert r.status_code != 403

    def test_extension_origin_without_the_key_is_refused(self):
        from kestrel.web import auth

        auth.set_cached_key("ext-key")
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": "chrome-extension://abcdef"})
            wrong = c.post(
                "/api/scan/now",
                headers={"Origin": "chrome-extension://abcdef", "X-Kestrel-Key": "not-it"},
            )
        assert r.status_code == 403 and wrong.status_code == 403
        assert "X-Kestrel-Key" in r.json()["detail"]

    def test_extension_origin_with_no_secret_generated_is_refused_with_instructions(self):
        # Fails closed, and the body tells the operator what to do rather than a bare 403.
        from kestrel.web import auth

        auth.set_cached_key(None)
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": "moz-extension://abcdef"})
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert "Settings > LAN" in detail and "options" in detail

    def test_extension_key_does_not_ride_on_the_cookie(self):
        # A hostile extension could carry the browser's cookie jar; only the header counts.
        from kestrel.web import auth

        auth.set_cached_key("ext-key")
        with self._c() as c:
            c.cookies.set(auth.COOKIE_NAME, "ext-key")
            r = c.post("/api/scan/now", headers={"Origin": "chrome-extension://abcdef"})
        assert r.status_code == 403

    def test_no_origin_header_still_works(self):
        # curl, the MCP server, and these tests are not browser-driven and carry no ambient cookie.
        with self._c() as c:
            r = c.post("/api/scan/now")
        assert r.status_code != 403

    def test_cross_origin_get_is_untouched(self):
        # Reads are not blocked by ORIGIN; confidentiality of reads is the Host guard's job below.
        with self._c() as c:
            r = c.get("/health", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "origin",
        ["null", "file:///C:/x", "data:text/html,<h1>x", "blob:http://evil.com/x", "about:blank"],
    )
    def test_opaque_and_non_extension_schemes_are_refused(self, origin):
        # An earlier cut trusted ANY non-http(s) scheme, so file:/data:/blob:/about: were all
        # allowed. Browsers serialize those as "null" today, which made it unexploitable by luck
        # rather than by design. These pin the intent: only real extension schemes are trusted.
        with self._c() as c:
            r = c.post("/api/scan/now", headers={"Origin": origin})
        assert r.status_code == 403


class TestHostGuard:
    """DNS rebinding is what makes a guessable port dangerous on the READ side.

    The origin guard stops a hostile page writing, but it cannot stop it reading: rebinding makes
    the attacker's own document same-origin, so responses become readable. /settings renders the
    LAN shared secret in cleartext and the download routes return the operator's resume PDFs.
    Pinning Host is the control that breaks rebinding, because the attacker cannot forge it.
    """

    @staticmethod
    def _c() -> TestClient:
        from kestrel.web.app import build_app

        return TestClient(build_app(worker=False), raise_server_exceptions=False)

    def test_rebound_host_is_refused(self):
        with self._c() as c:
            r = c.get("/health", headers={"Host": "evil.example"})
        assert r.status_code == 400

    @pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8770", "127.0.0.1"])
    def test_loopback_hosts_are_accepted(self, host):
        with self._c() as c:
            r = c.get("/health", headers={"Host": host})
        assert r.status_code == 200


def test_test_helper_never_consumes_a_ladder_rung():
    # running_server() uses ladder=False. If it took a rung it could collide with the operator's
    # actual running Kestrel, or two concurrent tests would fight over the same port.
    sock, port = serve.bind_free_port(ladder=False)
    try:
        assert port not in serve.PORT_LADDER
    finally:
        sock.close()


class TestStateChangingGetIsGuarded:
    """GET /renders/new creates a DocRender row and launches Edge through Playwright to write a
    PDF. Under a method-only guard a hostile page could drive it with a bare <img src=...> loop:
    unbounded headless-browser spawns and disk writes on the operator's machine."""

    @staticmethod
    def _c() -> TestClient:
        from kestrel.web.app import build_app

        return TestClient(build_app(worker=False), raise_server_exceptions=False)

    def test_cross_origin_state_changing_get_is_refused(self):
        with self._c() as c:
            r = c.get("/renders/new?master_id=1", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_same_origin_state_changing_get_still_works(self):
        with self._c() as c:
            r = c.get("/renders/new?master_id=1", headers={"Origin": "http://127.0.0.1:8765"})
        assert r.status_code != 403

    def test_ordinary_reads_are_still_exempt(self):
        # The guard must not become "block all cross-origin GETs": reads are not the threat.
        with self._c() as c:
            r = c.get("/health", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    @pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
    def test_no_cors_get_without_origin_is_refused_by_sec_fetch_site(self, site):
        # Browsers OMIT Origin on a no-cors GET (<img src>, <link>, a navigation) and send only
        # Sec-Fetch-Site. An Origin-only guard let <img src="http://127.0.0.1:8765/renders/new">
        # straight into the render path from a loopback peer.
        with self._c() as c:
            r = c.get("/renders/new?master_id=1", headers={"Sec-Fetch-Site": site})
        assert r.status_code == 403

    @pytest.mark.parametrize("site", ["same-origin", "none"])
    def test_operator_initiated_get_is_allowed(self, site):
        # Our own links and a typed URL or bookmark are the operator acting deliberately.
        with self._c() as c:
            r = c.get("/renders/new?master_id=1", headers={"Sec-Fetch-Site": site})
        assert r.status_code != 403

    def test_non_browser_get_with_neither_header_is_allowed(self):
        # curl and tests send neither Origin nor Sec-Fetch-Site; they are not a drive-by.
        with self._c() as c:
            r = c.get("/renders/new?master_id=1")
        assert r.status_code != 403

    def test_sec_fetch_site_does_not_block_ordinary_reads(self):
        with self._c() as c:
            r = c.get("/health", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 200


class TestOutOfRangeIdsAreNotServerErrors:
    """FastAPI parses {id} as an unbounded Python int; SQLite INTEGER is 64-bit. session.get with a
    huge id raised OverflowError deep in the driver and surfaced as a 500 on 37 route/id pairs. The
    per-route "never 500" handlers could not help, because their error path re-renders the page and
    re-runs the same failing lookup outside the guard."""

    _BIG = "99999999999999999999"

    @staticmethod
    def _c() -> TestClient:
        from kestrel.db import migrate
        from kestrel.web.app import build_app

        migrate.run_migrations()
        return TestClient(build_app(worker=False), raise_server_exceptions=False)

    def test_a_huge_id_is_not_found_not_a_server_error(self):
        with self._c() as c:
            for path in (
                f"/opportunities/{self._BIG}/archive",
                f"/opportunities/{self._BIG}",
                f"/api/opportunities/{self._BIG}",
            ):
                r = c.post(path) if path.endswith("archive") else c.get(path)
                assert r.status_code != 500, f"{path} -> 500"

    def test_a_huge_page_number_is_not_a_server_error(self):
        # Reachable with no id at all: the paginator's OFFSET overflows the same way.
        with self._c() as c:
            assert c.get(f"/api/opportunities?page={self._BIG}").status_code != 500
            assert c.get(f"/opportunities?page={self._BIG}").status_code != 500
