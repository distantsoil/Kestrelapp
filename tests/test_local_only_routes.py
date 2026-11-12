"""Issue #155: destructive, secret-bearing, data-exporting and data-routing routes are local-only.

The shared key lets a phone or a tunnel visitor browse and work. It must not let one take the data
away or point it somewhere else. One dependency (kestrel.web.auth.require_local) enforces that, and
this file enumerates the route table so a new route in a guarded class fails here until it carries
the guard (or is added to an allowlist below, with a reason).

Invariants, each pinned below:
- The operator at the keyboard (loopback, Host 127.0.0.1) is unaffected on every guarded route.
- A forged Host or forwarding header can only make a request count as remote, never local.
- A blocked action changes nothing and leaks nothing: the 403 body is one plain sentence.
- Nothing here sends data anywhere: a refused request opens no network connection.

All text is invented. Nothing reaches the network.
"""

from __future__ import annotations

import inspect
import re
import socket
import types

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.web import auth
from kestrel.web.auth import LOCAL_ONLY, LOCAL_ONLY_CHANGE, LOCAL_ONLY_DOWNLOAD
from tests.test_contracts import _reach, _walk

KEY = "s3cret-key-for-155"
TUNNEL = "k155.example.com"
LAN_PEER = "192.168.1.50"
SENTENCES = {LOCAL_ONLY_CHANGE, LOCAL_ONLY_DOWNLOAD}


# ------------------------------------------------------------------ what must be guarded
# A route is in a guarded class when "METHOD path" matches one of these. Every method counts:
# a page render under a guarded prefix is listed in OPEN_RENDERS with its reason, never implied.
GUARDED = [re.compile(p) for p in (
    r"^[A-Z]+ /settings/restore",
    r"^[A-Z]+ /onboarding/restart",
    r"^[A-Z]+ /backup/",                       # download, export-profile, import-profile
    r"^[A-Z]+ /settings/data/backup",
    r"^[A-Z]+ /settings/hosted",
    r"^[A-Z]+ /settings/lan/",
    r"^[A-Z]+ /settings/mcp",
    r"^[A-Z]+ /settings/privacy/(access|mcp)",
    r"^[A-Z]+ /settings/computer/(mode|key|free)",
    r"^[A-Z]+ /settings/(learning|scoring)/(export|reset|forget)",
    r"^[A-Z]+ /setup/mode",
    r"^[A-Z]+ /onboarding/models/(mode|connect)",
    r"^[A-Z]+ /models/(online|hosted|change|network)",
    r"^[A-Z]+ /sources/[^/]+/key",
    r"^[A-Z]+ /sources/remoterocketship/key",
    r"^[A-Z]+ /analytics/export/",
    r"^[A-Z]+ /about/(diagnostics|folder)",
    r"^[A-Z]+ /bench/(start|apply|undo|run|check$|promote)",
    r"^[A-Z]+ /api/quit",
    r"^[A-Z]+ /setup/(action|profile)",
    r"^[A-Z]+ /settings/computer/(action|profile|option)",
    r"^[A-Z]+ /onboarding/models/action",
    r"^[A-Z]+ /models/(download|health/fix)",
    r"^[A-Z]+ /announcements/model-upgrade",
    r"^[A-Z]+ /quarantine/\{[^}]+\}/model$",
    r"^POST /mock/\{[^}]+\}/route$",
    r"^POST /settings/(desktop|updates)",
    # Guarded by what they would write or pick, inside the handler (CONDITIONAL below).
    r"^POST /settings/undo$",
    r"^POST /settings$",
    r"^POST /settings/ui$",
    r"^POST /settings/privacy/confidential$",
    r"^POST /profile/confidential$",
    r"^POST /runs/now$",
    r"^POST /api/run/now$",
    r"^POST /runs/keep-awake$",
    r"^POST /rescore$",
    r"^POST /runs/start$",
    r"^POST /scan/start$",
    r"^POST /sources/\{[^}]+\}/run-now$",
)]

# Routes whose guard sits in the handler because only SOME requests need it: the same route is
# ordinary work with the configured setup and local-only for a local-only key or another model.
# Each is proven by a behavioral test below, not by the dependency.
CONDITIONAL = {
    "POST /settings/undo": "refused for any key in settings_io.needs_local; others stay open",
    "POST /settings": "turning confidential mode off is refused; concurrency and ages stay open",
    "POST /settings/ui": "interface keys are open; a key in settings_io.needs_local is refused",
    "POST /settings/privacy/confidential":
        "turning it ON stays open (stricter is always allowed); OFF is refused",
    "POST /profile/confidential":
        "the About you card: turning it ON stays open; OFF is refused (D-SEC-10)",
    "POST /runs/now": "Run now with the configured mode is ordinary; another mode is refused",
    "POST /api/run/now": "Run now with the configured mode is ordinary; another mode is refused",
    "POST /rescore": "Same model is ordinary scoring; picking another model is refused",
    "POST /runs/start": "Run now with the configured mode is ordinary; another mode is refused",
    "POST /scan/start": "Scan now with the usual models is ordinary; one other model is refused",
    "POST /sources/{source_id}/run-now":
        "Run now with the usual models is ordinary; one other model is refused",
}

# GET pages under a guarded prefix that stay open to a key holder: they only RENDER state, and
# the controls on them are disabled for a key holder (see the page tests below).
OPEN_RENDERS = {
    "GET /backup": "the Backup page: explains the buttons; its download and restore buttons are "
                   "disabled for a key holder",
    "GET /settings/hosted": "key screen: shows 'key saved', provider and model; controls disabled",
    "GET /settings/mcp": "the MCP switch card: shows the state; the switch is disabled",
    "GET /settings/privacy/access": "the Access card: shows the state; the switch is disabled",
    "GET /settings/privacy/mcp": "the MCP card: shows the state; the controls are disabled",
    "GET /settings/computer/mode": "run-mode card: shows the choice; the choices are disabled",
    "GET /models/online": "online models card: shows the saved key state; controls disabled",
    "GET /models/network": "servers on your network: shows the saved servers; controls disabled",
    "GET /models/hosted/overrides": "per-tier online model list: shows choices; saving disabled",
    "GET /models/change/{tier}": "the change-model form: a render only; saving is guarded",
    "GET /models/downloads": "the download progress card: a render; its buttons are disabled",
    "GET /mock/{opportunity_id}/route": "shows the configured route for a practice session",
}

# Routes that return a file by default and stay open to a key holder, with the reason.
OPEN_FILES = {
    "GET /opportunities/{opp_id}/role/cover-letter/{render_id}/download":
        "one role's cover letter, which a key holder browsing that role may download; not an "
        "export of the person's data",
    "GET /prep/{prep_pack_id}/download":
        "one role's prep pack PDF, browsed from that role; not an export of all data",
    "GET /renders/{render_id}/download":
        "one role's tailored resume PDF, browsed from that role; not an export of all data",
    "GET /profile/photo":
        "the avatar image shown in the top bar on every page; inline, not an export",
    "GET /settings/data/applied/sample":
        "a fixed invented sample CSV; no data of the person's",
    "GET /settings/data/contacts/sample":
        "a fixed invented sample CSV; no data of the person's",
}

# Routes under a guarded prefix that are ordinary work, not a destructive, secret or routing
# change. Each is intentional.
OPEN_WORK = {
    "GET /settings/data/applied": "the import-past-applications card; reads nothing sensitive",
    "POST /settings/data/applied/add": "adds the person's own past applications: normal work",
    "POST /settings/data/applied/read": "reads a pasted CSV into a preview: normal work",
    "POST /settings/data/applied/review": "reviews rows before import: normal work",
    "POST /settings/data/applied/undo/{token}": "undoes an import the same session made",
    "GET /settings/data/contacts": "the connections import card",
    "POST /settings/data/contacts/apply": "imports the person's own connections: normal work",
    "GET /settings/data/contacts/how": "static help text with nothing of the person data",
    "POST /settings/data/contacts/read": "reads a pasted CSV into a preview: normal work",
    "POST /settings/data/contacts/remove": "removes imported connections; an ordinary list edit",
}

# Browsing and working routes a key holder keeps (the third tier is "browse and work").
KEY_HOLDER_WORKS = (
    "GET /", "GET /opportunities", "GET /settings", "GET /analytics",
    "POST /calibrate/skip", "POST /today/quick/snooze",
)


def _guarded(route) -> bool:
    dep = getattr(route, "dependant", None)
    return bool(dep) and any(getattr(d.call, "kestrel_local_only", False)
                             for d in dep.dependencies)


def _returns_file(route) -> bool:
    own = inspect.getsource(route.endpoint)  # _reach skips non-kestrel modules, test routes too
    return bool(re.search(r"Content-Disposition|FileResponse\(|StreamingResponse\(",
                          own + _reach(route.endpoint, 2)))


def _table(routes) -> list[tuple[str, str, object]]:
    out = []
    for path, route in _walk(routes):
        for method in sorted(m for m in (getattr(route, "methods", None) or ()) if m != "HEAD"):
            out.append((method, path, route))
    return out


def unguarded(routes) -> list[str]:
    """Every route that is in a guarded class or returns a file, has no guard, and is not on an
    allowlist. This is the whole contract; a new route that trips it fails the test."""
    open_ok = {*OPEN_RENDERS, *OPEN_FILES, *OPEN_WORK, *CONDITIONAL}
    bad = []
    for method, path, route in _table(routes):
        key = f"{method} {path}"
        wanted = any(g.match(key) for g in GUARDED) or _returns_file(route)
        if wanted and not _guarded(route) and key not in open_ok:
            bad.append(key)
    return bad


@pytest.fixture(scope="module")
def route_table():
    from kestrel.web.app import build_app

    return _table(build_app(worker=False).routes)


@pytest.fixture(autouse=True)
def _reset_key():
    yield
    auth.reset_cache()


@pytest.fixture(autouse=True)
def _migrated_db():
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine

    migrate.run_migrations()
    yield
    forget_engine()


@pytest.fixture
def no_network(monkeypatch):
    """Refuse every outbound socket and count the attempts: a blocked request opens none.

    One exception: the event loop's own self-pipe. On Windows `socket.socketpair()` (what the
    ProactorEventLoop calls when it starts) is a real TCP connect to 127.0.0.1 under the hood,
    and refusing it leaves a half-built loop whose `__del__` raises. That connect is test
    harness plumbing, not a request the app opened, so it passes through uncounted; only the
    loopback self-pipe is exempt, so a handler connection is still refused and counted,
    loopback or not."""
    attempts: list[object] = []
    real = socket.socket.connect
    state = {"depth": 0}
    loopbacks = ("127.0.0.1", "::1")

    def _is_self_pipe(address: object) -> bool:
        return (state["depth"] > 0 and isinstance(address, tuple) and bool(address)
                and address[0] in loopbacks)

    def refuse(self, address):
        if _is_self_pipe(address):
            return real(self, address)
        if isinstance(address, tuple):
            attempts.append(address)
            raise OSError("network is off in this test")
        return real(self, address)

    real_pair = socket.socketpair

    def pair(*args, **kwargs):
        state["depth"] += 1
        try:
            return real_pair(*args, **kwargs)
        finally:
            state["depth"] -= 1

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "socketpair", pair)
    return attempts


def _app(monkeypatch=None, *, tunnel: bool = False):
    if tunnel and monkeypatch is not None:
        monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", f'["{TUNNEL}"]')
    from kestrel.web.app import build_app

    return build_app(worker=False)


def _client(app, peer: str) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False,
                      client=(peer, 12345))


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


REMOTE = [
    pytest.param(LAN_PEER, {}, id="lan-peer-with-key"),
    pytest.param("127.0.0.1", {"Host": TUNNEL}, id="tunnel-with-key"),
]


def _data_fingerprint() -> tuple:
    """Everything a refused request could have changed: the setting rows and the data folder."""
    import sqlite3

    rows: list = []
    con = sqlite3.connect(paths.db_path())
    try:
        rows = con.execute('select key, value from setting order by key').fetchall()
    finally:
        con.close()
    files = sorted(
        (str(p.relative_to(paths.data_home())), p.stat().st_size)
        for p in paths.data_home().rglob("*")
        if p.is_file() and p.suffix not in {".db-wal", ".db-shm"}
        and not p.name.startswith("kestrel.db")
    )
    return tuple(rows), tuple(files)


# ------------------------------------------------------------------ the route-table contract
def test_every_route_in_a_guarded_class_carries_the_guard(route_table):
    bad = []
    for method, path, route in route_table:
        key = f"{method} {path}"
        wanted = any(g.match(key) for g in GUARDED) or _returns_file(route)
        if wanted and not _guarded(route) and key not in {
                *OPEN_RENDERS, *OPEN_FILES, *OPEN_WORK, *CONDITIONAL}:
            bad.append(key)
    assert not bad, (
        "These routes change data, keys or destinations, or hand over a file, so they need "
        "dependencies=[LOCAL_ONLY] (or LOCAL_DOWNLOAD), or an entry with a reason in "
        "OPEN_RENDERS / OPEN_FILES / OPEN_WORK:\n  " + "\n  ".join(bad))


def test_the_check_fails_for_a_new_route_without_the_guard():
    """The contract is not vacuous: a route added under a guarded prefix with no guard is
    reported, one with the guard is not, and a new file route is reported by what it returns."""
    from fastapi.responses import FileResponse

    router = APIRouter()

    @router.post("/settings/hosted/brand-new")
    async def new_unguarded():
        return "x"

    @router.post("/settings/hosted/brand-new-ok", dependencies=[LOCAL_ONLY])
    async def new_guarded():
        return "x"

    @router.get("/somewhere/else/export.zip")
    async def new_file():
        return FileResponse("/nonexistent")

    @router.get("/backup/new-download")
    async def new_backup():
        return "x"

    app = FastAPI()
    app.include_router(router)
    assert sorted(unguarded(app.routes)) == [
        "GET /backup/new-download",
        "GET /somewhere/else/export.zip",
        "POST /settings/hosted/brand-new",
    ]


def test_allowlists_name_real_routes_and_say_why(route_table):
    live = {f"{m} {p}" for m, p, _ in route_table}
    for table in (OPEN_RENDERS, OPEN_FILES, OPEN_WORK, CONDITIONAL):
        for key, reason in table.items():
            assert key in live, f"{key} is on an allowlist but is not a route any more"
            assert len(reason) > 20, f"{key} needs a real reason"
    guarded_and_listed = [k for k in {*OPEN_RENDERS, *OPEN_FILES, *OPEN_WORK, *CONDITIONAL}
                          if any(f"{m} {p}" == k and _guarded(r) for m, p, r in route_table)]
    assert not guarded_and_listed, f"{guarded_and_listed} are guarded: drop them from the list"


def test_the_expected_classes_are_all_discovered(route_table):
    live = {f"{m} {p}": r for m, p, r in route_table}
    must = [
        "POST /settings/restore", "POST /onboarding/restart", "POST /backup/import-profile",
        "GET /backup/download", "GET /settings/data/backup", "GET /backup/export-profile",
        "GET /about/diagnostics", "GET /settings/scoring/export", "GET /settings/learning/export",
        "GET /analytics/export/applications.csv", "GET /analytics/export/numbers.csv",
        "GET /analytics/export/summary.pdf", "GET /analytics/export/work-search-log.csv",
        "POST /settings/hosted/key",
        "POST /settings/hosted/keybox", "POST /settings/hosted/connect",
        "POST /settings/hosted/mode", "POST /settings/hosted/switch",
        "POST /settings/hosted/tiers", "POST /settings/hosted/model", "POST /settings/hosted/test",
        "POST /settings/hosted/balance", "POST /settings/mcp", "POST /settings/lan/bind",
        "POST /settings/lan/secret", "POST /settings/privacy/access",
        "POST /settings/privacy/access/rotate", "POST /settings/learning/reset",
        "POST /settings/scoring/forget", "POST /models/online/key", "POST /models/online/connect",
        "POST /models/change/{tier}", "POST /models/hosted/overrides",
        "POST /settings/computer/key", "POST /settings/computer/mode",
        "POST /onboarding/models/connect", "POST /onboarding/models/mode",
        "POST /sources/adzuna/key", "POST /sources/usajobs/key",
        "POST /models/network/test", "POST /models/network/save", "POST /models/network/assign",
        "POST /models/network/reset", "POST /models/network/remove",
    ]
    for key in must:
        assert key in live, f"{key} is not a route; update this list"
        assert _guarded(live[key]), f"{key} must carry the local-only guard"


def test_browsing_and_work_routes_are_not_guarded(route_table):
    live = {f"{m} {p}": r for m, p, r in route_table}
    for key in KEY_HOLDER_WORKS:
        assert key in live, key
        assert not _guarded(live[key]), f"{key} is browsing or work and must stay open"


# ------------------------------------------------------------------ a key holder is refused
def _guarded_requests(route_table):
    return [(m, p) for m, p, r in route_table if _guarded(r)]


@pytest.mark.parametrize(("peer", "headers"), REMOTE)
def test_every_guarded_route_refuses_a_key_holder_and_changes_nothing(
        peer, headers, route_table, monkeypatch, no_network):
    """The invariant: a valid key is not enough. 403, one plain sentence, no change, no network."""
    auth.set_cached_key(KEY)
    app = _app(monkeypatch, tunnel=True)
    before = _data_fingerprint()
    checked = 0
    with _client(app, peer) as c:
        for method, path in _guarded_requests(route_table):
            r = c.request(method, _fill(path), headers={"X-Kestrel-Key": KEY, **headers})
            assert r.status_code == 403, f"{method} {path} answered {r.status_code}"
            assert r.text in SENTENCES, f"{method} {path} body is not a plain sentence: {r.text!r}"
            assert KEY not in r.text and str(paths.data_home()) not in r.text
            checked += 1
    assert checked >= 40, "the guarded route set shrank: the discovery is broken"
    assert _data_fingerprint() == before, "a refused request changed something"
    assert no_network == [], "a refused request opened a network connection"


def test_downloads_say_download_and_changes_say_change(monkeypatch):
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        h = {"X-Kestrel-Key": KEY}
        assert c.get("/backup/download", headers=h).text == "Only this computer can download this."
        assert c.get("/analytics/export/numbers.csv", headers=h).text == LOCAL_ONLY_DOWNLOAD
        assert c.post("/settings/restore", headers=h).text == "Only this computer can change this."
        assert c.post("/onboarding/restart", headers=h).text == LOCAL_ONLY_CHANGE


@pytest.mark.parametrize(("peer", "headers"), REMOTE)
def test_a_key_holder_still_browses_and_works(peer, headers, monkeypatch):
    auth.set_cached_key(KEY)
    h = {"X-Kestrel-Key": KEY, **headers}
    with _client(_app(monkeypatch, tunnel=True), peer) as c:
        for path in ("/opportunities", "/settings", "/analytics", "/settings/hosted", "/backup",
                     "/settings/privacy/access", "/models/online"):
            r = c.get(path, headers=h)
            assert r.status_code == 200, f"GET {path} answered {r.status_code} for a key holder"
        r = c.post("/today/quick/snooze", headers=h)
        assert r.text not in SENTENCES, "ordinary work must not be refused by the guard"


def test_a_request_without_the_key_is_still_401_not_403(monkeypatch):
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch), LAN_PEER) as c:
        assert c.post("/settings/restore").status_code == 401
        assert c.get("/backup/download").status_code == 401


# ------------------------------------------------------------------ this computer is unaffected
def test_the_operator_at_the_keyboard_is_never_refused_by_the_guard(
        route_table, monkeypatch, no_network):
    """Loopback and Host 127.0.0.1 or localhost, no key at all: not one guarded route answers with
    the guard's sentence. (Handlers may refuse an empty form for their own reasons.)"""
    auth.set_cached_key(None)
    # Real effects on this machine (a wipe, a restore, a quit, a download, a folder or a file): the
    # guard on these is proven for every route by test_the_guard_passes_this_computer_...
    skip = {"POST /onboarding/restart", "POST /settings/restore", "POST /api/quit",
            "POST /about/diagnostics/save", "POST /about/diagnostics/reveal",
            "POST /about/folder/open", "POST /announcements/model-upgrade/download"}
    skip |= {f"POST {p}" for p in ("/models/download", "/models/download/tune",
                                   "/models/download/pause", "/models/health/fix")}
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        for host in ("127.0.0.1", "localhost"):
            for method, path in _guarded_requests(route_table):
                if f"{method} {path}" in skip:
                    continue
                r = c.request(method, _fill(path), headers={"Host": host})
                assert r.text != LOCAL_ONLY_CHANGE and r.text != LOCAL_ONLY_DOWNLOAD, (
                    f"{method} {path} was refused for the operator on {host}")


@pytest.mark.parametrize("path", [
    "/backup/download", "/settings/data/backup",
    "/settings/scoring/export", "/settings/learning/export",
    "/analytics/export/applications.csv", "/analytics/export/numbers.csv",
    "/analytics/export/work-search-log.csv",
])
def test_downloads_work_for_the_operator(path, monkeypatch):
    auth.set_cached_key(None)
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        r = c.get(path)
    assert r.status_code == 200, f"{path} answered {r.status_code}"
    assert "attachment" in r.headers.get("content-disposition", "")


def test_the_profile_export_is_not_refused_for_the_operator(monkeypatch):
    """With no profile.yml saved the handler answers 404 for its own reason, never the guard's."""
    auth.set_cached_key(None)
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        r = c.get("/backup/export-profile")
    assert r.text != LOCAL_ONLY_DOWNLOAD and r.status_code != 403


def test_restart_and_restore_reach_their_handlers_for_the_operator(monkeypatch):
    auth.set_cached_key(None)
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        r = c.post("/settings/restore")  # no file: the handler's own plain message
        assert r.text != LOCAL_ONLY_CHANGE
        assert r.status_code != 403


# ------------------------------------------------------------------ forged headers
@pytest.mark.parametrize("forged", [
    {"X-Forwarded-For": "127.0.0.1"},
    {"Forwarded": "for=127.0.0.1;host=127.0.0.1"},
    {"X-Real-IP": "127.0.0.1"},
    {"X-Forwarded-Host": "127.0.0.1"},
    {"Host": "localhost"},
    {"Host": "127.0.0.1"},
])
def test_a_forged_header_cannot_make_a_remote_request_local(forged, monkeypatch):
    """The peer address decides. Every header an outsider controls can only add to the evidence
    that a request is remote; none of them can turn a LAN peer into this computer."""
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post("/settings/restore", headers={"X-Kestrel-Key": KEY, **forged})
    assert r.status_code == 403 and r.text == LOCAL_ONLY_CHANGE


def test_a_tunnel_host_from_a_loopback_peer_is_remote_even_with_other_headers(monkeypatch):
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), "127.0.0.1") as c:
        r = c.get("/backup/download", headers={
            "X-Kestrel-Key": KEY, "Host": TUNNEL, "X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 403 and r.text == LOCAL_ONLY_DOWNLOAD


def test_a_forwarding_header_makes_a_loopback_request_remote(monkeypatch):
    """A proxy header means a proxy or tunnel forwarded the request, so it is not this computer
    even with a plain localhost Host. (It is only ever evidence of remote, never of local.)"""
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        r = c.get("/analytics/export/numbers.csv", headers={"X-Forwarded-For": "203.0.113.9",
                                                            "X-Kestrel-Key": KEY})
    assert r.status_code == 403 and r.text == LOCAL_ONLY_DOWNLOAD


# ------------------------------------------------------------------ the read-only page
PAGES = ("/settings/hosted", "/backup", "/settings/section/computer", "/settings/section/scoring",
         "/settings/section/privacy", "/settings/section/data", "/models", "/analytics",
         "/analytics?view=table", "/about", "/settings/learning")
DOWNLOAD_PATHS = ("/backup/download", "/backup/export-profile", "/settings/data/backup",
                  "/settings/scoring/export", "/settings/learning/export", "/about/diagnostics",
                  "/analytics/export/")


def test_a_key_holder_sees_the_pages_read_only_with_nothing_leaked(monkeypatch):
    """The pages stay visible. Paste, Replace, Test, Balance and Switch controls, and the
    destructive and download buttons, are disabled with the one sentence; no key value, no path,
    and no working link to a download."""
    auth.set_cached_key(KEY)
    planted = "sk-test-0000-never-shown-1234"  # invented; must never reach a page
    (paths.data_home() / ".env").write_text(f"OPENROUTER_API_KEY={planted}\n", encoding="utf-8")
    h = {"X-Kestrel-Key": KEY}
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        pages = {p: c.get(p, headers=h) for p in PAGES}
    for path, r in pages.items():
        assert r.status_code == 200, path
        assert planted not in r.text, f"{path} leaked the saved key"
        assert KEY not in r.text, f"{path} leaked the shared key"
        assert str(paths.data_home()) not in r.text, f"{path} leaked the data folder"
        for d in DOWNLOAD_PATHS:
            assert f'href="{d}' not in r.text, f"{path} still links to {d} for a key holder"
    assert "Only this computer can change this" in pages["/settings/hosted"].text
    assert "Only this computer can change this" in pages["/backup"].text
    assert "Only this computer can download this" in pages["/backup"].text
    assert "Only this computer can download this" in pages["/settings/section/scoring"].text
    assert "Only this computer can change this" in pages["/settings/section/data"].text
    assert "Only this computer can change this" in pages["/settings/section/computer"].text
    assert "Only this computer can download this" in pages["/analytics"].text
    assert "Only this computer can download this" in pages["/settings/learning"].text


def test_this_computer_sees_every_control_enabled_and_no_sentence(monkeypatch):
    auth.set_cached_key(None)
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        pages = {p: c.get(p) for p in PAGES}
    for path, r in pages.items():
        assert r.status_code == 200, path
        assert "Only this computer can change this" not in r.text, f"{path} shows it here"
        assert "Only this computer can download this" not in r.text, f"{path} shows it here"
        assert 'data-testid="local-only-fence"' not in r.text
    assert 'href="/backup/download"' in pages["/backup"].text


def test_a_key_holder_cannot_make_the_app_use_the_saved_key_by_viewing_a_page(
        monkeypatch, no_network):
    """Opening the model picker fetches the provider's list WITH the saved key. For a key holder
    the page renders closed and the provider is never asked."""
    from kestrel.services import hosted_models as hm

    asked: list[str] = []

    async def fetch(*a, **kw):
        asked.append("asked")
        return []

    monkeypatch.setattr(hm, "fetch_models", fetch)
    env = paths.data_home() / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-test-1234\n", encoding="utf-8")
    auth.set_cached_key(KEY)
    h = {"X-Kestrel-Key": KEY}
    urls = ("/models/hosted/overrides?open=1&refresh=1",
            "/models/change/tier1?where=online&refresh=1")
    app = _app(monkeypatch, tunnel=True)
    # The control: this computer asking for the same pages DOES reach the provider's list, so
    # the empty result below is the guard working, not a setup that never gets that far.
    with _client(app, "127.0.0.1") as c:
        for path in urls:
            assert c.get(path).status_code == 200
    assert asked, "setup error: the local request never asked for the model list"
    asked.clear()
    with _client(app, LAN_PEER) as c:
        for path in urls:
            assert c.get(path, headers=h).status_code == 200
    off_machine = [a for a in no_network if a[0] not in ("127.0.0.1", "::1")]
    assert asked == [] and off_machine == []  # the page may probe the local model server


def test_the_dependency_alone_refuses_a_remote_and_passes_a_local():
    """The guard in isolation, on a throwaway app, so a change to the real routes cannot hide a
    broken guard: remote 403, loopback 200, and the handler never runs for a remote."""
    ran: list[int] = []
    app = FastAPI()
    auth.install_lan_auth(app)

    @app.post("/x", dependencies=[Depends(auth.require_local)])
    async def x():
        ran.append(1)
        return "ok"

    auth.set_cached_key(KEY)
    with _client(app, LAN_PEER) as c:
        r = c.post("/x", headers={"X-Kestrel-Key": KEY})
    assert (r.status_code, r.text, ran) == (403, LOCAL_ONLY_CHANGE, [])
    with _client(app, "127.0.0.1") as c:
        r = c.post("/x")
    assert (r.status_code, ran) == (200, [1])


# ================================================================== review round (#155)
# The principle: a key holder browses and does ordinary work with the CONFIGURED setup. Only this
# computer changes what leaves it or where it goes, spends credit beyond ordinary scoring, starts
# or installs anything on the host, or sees host paths, balances or provider data.
from starlette.requests import Request as _Req  # noqa: E402


def _scope_request(peer: str, host: str, extra: list[tuple[bytes, bytes]] | None = None):
    headers = [(b"host", host.encode()), *(extra or [])]
    state = types.SimpleNamespace()
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers,
             "client": (peer, 5555), "query_string": b"", "server": ("127.0.0.1", 8765),
             "scheme": "http", "app": types.SimpleNamespace(state=state)}
    return _Req(scope)


@pytest.mark.parametrize(("peer", "host", "extra", "local"), [
    ("127.0.0.1", "localhost:8765", [], True),
    ("127.0.0.1", "127.0.0.1", [], True),
    ("127.0.0.1", "[::1]:8765", [], True),
    ("::1", "LOCALHOST", [], True),
    ("127.0.0.1", "localhost.", [], True),  # a trailing dot is still this computer's own name
    ("127.0.0.1", "trailing.example.com.", [], False),  # a LISTED host with a trailing dot
    ("127.0.0.1", "k155.example.com", [], False),
    ("127.0.0.1", "quick-tunnel-1234.trycloudflare.com", [], False),  # unlisted: fail closed
    ("127.0.0.1", "abc.ngrok.app", [], False),
    ("127.0.0.1", "machine.tailnet.ts.net", [], False),
    ("127.0.0.1", "", [], False),
    ("127.0.0.1", "127.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")], False),
    ("127.0.0.1", "localhost", [(b"forwarded", b"for=203.0.113.9")], False),
    ("127.0.0.1", "localhost", [(b"cf-connecting-ip", b"203.0.113.9")], False),
    ("127.0.0.1", "localhost", [(b"x-real-ip", b"203.0.113.9")], False),
    ("192.168.1.50", "localhost", [], False),
    ("192.168.1.50", "127.0.0.1", [], False),
])
def test_is_loopback_is_an_allowlist_that_fails_closed(peer, host, extra, local, monkeypatch):
    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES",
                       '["k155.example.com", "trailing.example.com"]')
    assert auth.is_loopback(_scope_request(peer, host, extra)) is local


def test_every_local_caller_of_this_app_sends_a_local_host():
    """The desktop shell, the CLI, the server helper and the focus call all build their URLs
    from 127.0.0.1, which the allowlist treats as this computer."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "kestrel"
    for rel in ("cli.py", "desktop/shell.py", "web/serve.py"):
        text = (root / rel).read_text(encoding="utf-8")
        hosts = set(re.findall(r'f?"http://([^:/"{]+)[:/{]', text))
        assert hosts <= {"127.0.0.1", "localhost"}, (rel, hosts)
    assert auth.is_loopback(_scope_request("127.0.0.1", "127.0.0.1:41999"))


def test_the_guard_passes_this_computer_and_stops_a_key_holder_on_every_guarded_route(
        route_table, monkeypatch):
    """The dependency of EVERY guarded route, called directly: this computer passes, a key holder
    does not. Covers the routes whose handler has a real effect on this machine (a quit, a
    download, a folder) without running them."""
    monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["k155.example.com"]')
    n = 0
    for _method, _path, route in route_table:
        if not _guarded(route):
            continue
        for dep in route.dependant.dependencies:
            if not getattr(dep.call, "kestrel_local_only", False):
                continue
            dep.call(_scope_request("127.0.0.1", "127.0.0.1:8765"))
            for peer, host in (("192.168.1.50", "127.0.0.1"), ("127.0.0.1", "k155.example.com")):
                with pytest.raises(auth.LocalOnlyRefused):
                    dep.call(_scope_request(peer, host))
            n += 1
    assert n >= 60


# ---- 1 and 8: the generic writers consult the same table as the dedicated routes
KEY_H = {"X-Kestrel-Key": KEY}


def _setting(key):
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        row = con.execute("select value from setting where key = ?", (key,)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


def _seed(key, value):
    import json
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        con.execute("delete from setting where key = ?", (key,))
        con.execute("insert into setting (key, value, updated_at) values (?, ?, '2026-01-01')",
                    (key, json.dumps(value)))
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("key", ["mcp.enabled", "mcp.port", "ui.autostart", "keep_awake",
                                 "updates.enabled", "ui.close_to_tray"])
def test_undo_cannot_write_a_key_whose_own_route_is_local_only(key, monkeypatch):
    """The bypass: POST /settings/undo wrote mcp.enabled and mcp.port for a LAN peer."""
    from kestrel.web import settings_io as io

    assert io.needs_local(key, True) and io.undoable(key)
    auth.set_cached_key(KEY)
    now, prev = ("9999", "7777") if key == "mcp.port" else ("false", "true")
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        _seed(key, int(now) if key == "mcp.port" else False)
        before = _setting(key)
        r = c.post(f"/settings/undo?key={key}&prev={prev}&now={now}", headers=KEY_H)
    assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE)
    assert _setting(key) == before


def test_undo_still_works_for_this_computer_and_for_ordinary_keys(monkeypatch):
    auth.set_cached_key(KEY)
    _seed("mcp.enabled", False)
    with _client(_app(monkeypatch, tunnel=True), "127.0.0.1") as c:
        r = c.post("/settings/undo?key=mcp.enabled&prev=true&now=false")
    assert r.status_code == 200 and _setting("mcp.enabled") in ("true", True, 1)
    _seed("ui.scheme", "teal")
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post("/settings/undo?key=ui.scheme&prev=%22indigo%22&now=%22teal%22", headers=KEY_H)
    assert r.status_code == 200 and r.text != LOCAL_ONLY_CHANGE


def test_every_local_only_setting_is_refused_by_every_generic_writer(monkeypatch):
    from kestrel.web import settings_io as io

    auth.set_cached_key(KEY)
    keys = sorted(k for k in io.LOCAL_ONLY_EXACT if io.undoable(k))
    assert {"mcp.enabled", "mcp.port", "keep_awake"} <= set(keys)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        for key in keys:
            r = c.post(f"/settings/undo?key={key}&prev=1&now=absent", headers=KEY_H)
            assert r.status_code == 403, key


def test_confidential_mode_off_is_local_only_and_on_is_open(monkeypatch):
    auth.set_cached_key(KEY)
    app = _app(monkeypatch, tunnel=True)
    _seed("confidential_mode", True)
    with _client(app, LAN_PEER) as c:
        off_form = c.post("/settings/privacy/confidential", headers=KEY_H,
                          data={"changed": "mode", "conf_mode": "0"})
        off_undo = c.post("/settings/undo?key=confidential_mode&prev=false&now=true",
                          headers=KEY_H)
        off_settings = c.post("/settings", headers=KEY_H, data={"concurrency.tier1": "2"})
    for r in (off_form, off_undo, off_settings):
        assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE)
    assert _setting("confidential_mode") in ("true", True, 1)
    _seed("confidential_mode", False)
    with _client(app, LAN_PEER) as c:
        on = c.post("/settings/undo?key=confidential_mode&prev=true&now=false", headers=KEY_H)
        ordinary = c.post("/settings", headers=KEY_H, data={"concurrency.tier1": "2",
                                                            "confidential_mode": "on"})
    assert on.status_code == 200 and ordinary.status_code == 200
    assert _setting("confidential_mode") in ("true", True, 1)


def test_ordinary_settings_save_stays_open_to_a_key_holder(monkeypatch):
    auth.set_cached_key(KEY)
    _seed("confidential_mode", False)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post("/settings", headers=KEY_H, data={"max_posting_age_days": "30"})
    assert r.status_code == 200 and r.text.startswith("Saved")


# ---- 7: a model or route other than the configured one is local-only
def test_picking_another_model_is_local_only_but_the_configured_route_is_open(monkeypatch):
    auth.set_cached_key(KEY)
    app = _app(monkeypatch, tunnel=True)
    with _client(app, LAN_PEER) as c:
        for path, data in (
                ("/rescore", {"opp_ids": "1", "model_choice": "other", "model": "kestrel-x"}),
                ("/rescore", {"opp_ids": "1", "model": "hosted:some/model"}),
                ("/runs/start", {"mode": "hosted", "confirmed": "1"}),
                ("/scan/start", {"model_choice": "other", "model": "kestrel-x"}),
                ("/sources/1/run-now", {"model_choice": "other", "model": "kestrel-x"}),
                ("/quarantine/1/model", {"model": "kestrel-x"}),
                ("/mock/1/route", {"model": "hosted:x", "confirmed": "1"})):
            r = c.post(path, headers=KEY_H, data=data)
            assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE), path
        same = c.post("/rescore", headers=KEY_H, data={"opp_ids": "1", "model": "same"})
    assert same.text != LOCAL_ONLY_CHANGE and same.status_code != 403, "configured route is open"


# ---- 9: no balance and no provider call for a key holder
def test_a_key_holder_view_never_shows_a_balance_or_asks_a_provider(monkeypatch, no_network):
    from kestrel.services import hosted_balance as hb

    calls: list[str] = []

    async def reading(*a, **kw):
        calls.append("reading")
        return hb.Reading(ok=True, remaining=4.2, usage=1.0)

    monkeypatch.setattr(hb, "eligible_backend", lambda *a, **kw: (
        "or", types.SimpleNamespace(api_key="k", api_key_env="K", base_url="u")))
    monkeypatch.setattr(hb, "_reading", reading)
    monkeypatch.setattr(hb, "_fresh", lambda key: None)
    monkeypatch.setattr(hb, "_start_background", lambda *a, **kw: calls.append("background"))
    auth.set_cached_key(KEY)
    urls = ("/status/rail", "/api/status", "/runs", "/runs/start/choose", "/rescore/choose",
            "/mock/1/online", "/quarantine/1/online")
    app = _app(monkeypatch, tunnel=True)
    with _client(app, "127.0.0.1") as c:
        for path in ("/status/rail", "/runs"):
            c.get(path)
    assert calls, "setup error: this computer's views should reach the balance code"
    calls.clear()
    with _client(app, LAN_PEER) as c:
        bodies = [c.get(p, headers=KEY_H).text for p in urls]
    assert calls == [], "a key holder's page view asked the provider"
    for body in bodies:
        assert "left on your key" not in body and "This month" not in body


def test_the_services_answer_nothing_to_a_key_holder():
    import asyncio

    from kestrel.services import hosted_balance as hb
    from kestrel.services import hosted_models as hm
    from kestrel.services import viewer

    token = viewer.set_remote(True)
    try:
        assert asyncio.run(hb.local_spend(None)) is None
        assert asyncio.run(hb.rail_row(None, None)) is None
        assert asyncio.run(hb.rail_row_nowait(None, None)) is None
        assert asyncio.run(hb.credit_left(None, None)) is None
        with pytest.raises(hm.ModelListError):
            asyncio.run(hm.fetch_models("x", object()))
    finally:
        viewer.reset(token)


# ---- 6 and 11: a runtime walk of every GET route as a key holder
SKIP_WALK = {"GET /auth", "GET /docs", "GET /docs/oauth2-redirect", "GET /redoc",
             "GET /openapi.json", "GET /favicon.ico"}
ALLOWED_TYPES = ("text/html", "application/json", "text/plain", "text/event-stream",
                 "image/", "text/css", "text/javascript", "application/javascript")


def leaky(client, table) -> list[str]:
    """Every unguarded GET route that, called as a key holder, answers with a file: an attachment,
    or a binary or data media type (zip, pdf, csv, octet-stream), or the host's folder path."""
    out = []
    root = str(paths.data_home())
    for method, path, route in table:
        key = f"{method} {path}"
        if method != "GET" or key in SKIP_WALK or _guarded(route) or key in OPEN_FILES:
            continue
        r = client.get(_fill(path), headers=KEY_H)
        kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
        attach = "attachment" in r.headers.get("content-disposition", "").lower()
        if attach or (kind and not kind.startswith(ALLOWED_TYPES)) or root in r.text:
            out.append(f"{key} -> {r.status_code} {kind or '-'}{' attachment' if attach else ''}"
                       f"{' PATH' if root in r.text else ''}")
    return out


def test_no_unguarded_get_route_hands_a_key_holder_a_file_or_a_host_path(
        route_table, monkeypatch, no_network):
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        found = leaky(c, route_table)
    assert found == [], "\n  " + "\n  ".join(found)


def test_the_runtime_walk_catches_the_three_probe_cases(monkeypatch):
    """What the source scan misses: a zip returned as a plain Response, a CSV from a helper reached
    through a module attribute, and a path in an error reply."""
    from fastapi.responses import PlainTextResponse, Response

    helper = types.SimpleNamespace(
        build=lambda: Response(b"PK\x03\x04", media_type="application/zip"))
    router = APIRouter()

    @router.get("/probe/zip")
    async def probe_zip():
        return Response(b"PK", media_type="application/zip")

    @router.get("/probe/csv")
    async def probe_csv():
        return helper.build()

    @router.get("/probe/csv2")
    async def probe_csv2():
        return Response("a,b\n1,2\n", media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=x.csv"})

    @router.get("/probe/path")
    async def probe_path():
        return PlainTextResponse(f"Could not open it. It is at {paths.data_home()}")

    @router.get("/probe/fine")
    async def probe_fine():
        return PlainTextResponse("hello")

    app = FastAPI()
    auth.install_lan_auth(app)
    app.include_router(router)
    auth.set_cached_key(KEY)
    with _client(app, LAN_PEER) as c:
        found = leaky(c, _table(app.routes))
    assert sorted(f.split(" ->")[0] for f in found) == [
        "GET /probe/csv", "GET /probe/csv2", "GET /probe/path", "GET /probe/zip"]


def test_a_key_holder_sees_the_host_action_buttons_disabled_with_the_sentence(monkeypatch):
    """Setup, downloads, the model check and Diagnostics act on this machine; the pages stay
    visible and say who can press them."""
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        pages = {p: c.get(p, headers=KEY_H) for p in (
            "/models", "/about", "/settings/computer/checklist", "/models/downloads",
            "/models/health", "/bench/panel")}
    for path, r in pages.items():
        assert r.status_code == 200, path
        # About draws the download link outside the desktop shell, the save button inside it.
        want = "Only this computer can " + ("download this" if path == "/about" else "change this")
        assert want in r.text, path


# ---- D-SEC-10: the Run now routes, the About you privacy card, keep-awake
def _hold_credit():
    from kestrel.llm import credit

    credit.clear()
    credit.mark_out_of_credit("hosted-x", "Invented Provider", "invented-key")
    return credit


@pytest.mark.parametrize(("path", "data", "query"), [
    ("/runs/now", {"mode": "hosted", "confirmed": "1"}, ""),
    ("/api/run/now", {"confirmed": "1"}, "?mode=hosted"),
])
def test_run_now_in_another_mode_is_local_only_and_never_releases_the_credit_hold(
        path, data, query, monkeypatch):
    auth.set_cached_key(KEY)
    credit = _hold_credit()
    before = _data_fingerprint()
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post(path + query, headers=KEY_H, data=data)
    assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE), path
    assert credit.any_held(), "a key holder released the hold on the online key"
    assert _data_fingerprint() == before
    credit.clear()


@pytest.mark.parametrize(("path", "query"), [("/runs/now", ""), ("/api/run/now", "")])
def test_run_now_with_the_configured_mode_is_open_but_a_key_holder_keeps_the_hold(
        path, query, monkeypatch):
    auth.set_cached_key(KEY)
    credit = _hold_credit()
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post(path + query, headers=KEY_H, data={"mode": "local"})
    assert r.status_code != 403, path
    assert credit.any_held(), "a key holder released the hold on the online key"
    credit.clear()


@pytest.mark.parametrize("path", ["/runs/now", "/api/run/now"])
def test_this_computer_run_now_releases_the_credit_hold(path, monkeypatch):
    credit = _hold_credit()
    with _client(_app(monkeypatch), "127.0.0.1") as c:
        r = c.post(path, data={"mode": "local"})
    assert r.status_code != 403
    assert not credit.any_held()


def test_the_about_you_card_cannot_turn_confidential_mode_off_for_a_key_holder(monkeypatch):
    auth.set_cached_key(KEY)
    app = _app(monkeypatch, tunnel=True)
    _seed("confidential_mode", True)
    with _client(app, LAN_PEER) as c:
        off = c.post("/profile/confidential", headers=KEY_H, data={})
    assert (off.status_code, off.text) == (403, LOCAL_ONLY_CHANGE)
    assert _setting("confidential_mode") in ("true", True, 1)
    _seed("confidential_mode", False)
    with _client(app, LAN_PEER) as c:
        on = c.post("/profile/confidential", headers=KEY_H, data={"conf_mode": "on"})
    assert on.status_code == 200
    assert _setting("confidential_mode") in ("true", True, 1)
    with _client(app, "127.0.0.1") as c:  # this computer may turn it off
        done = c.post("/profile/confidential", data={})
    assert done.status_code == 200
    assert _setting("confidential_mode") in ("false", False, 0)


def test_keep_awake_toggle_is_refused_for_a_key_holder_and_changes_nothing(monkeypatch):
    auth.set_cached_key(KEY)
    _seed("keep_awake", False)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post("/runs/keep-awake", headers=KEY_H, data={"on": "on"})
    assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE)
    assert _setting("keep_awake") in ("false", False, 0)


# Calls that write a local-only setting without naming its key (the key lives inside the helper).
LOCAL_ONLY_WRITER_CALLS = re.compile(
    r"\b(?:ka|keep_awake)\.(?:save|apply)\(|\bautostart\.set_enabled\(|\blan_access\.\w+\("
)


_LITERAL_KEY = re.compile(r"""["']([A-Za-z_]+(?:\.[A-Za-z_.]*)?)["']""")
# A key built at run time: f"llm.tiers.{tier}.model", "llm.tiers." + tier, f"hosted{x}". The part
# before the first hole is what the table's prefixes are matched against.
_BUILT_KEY = re.compile(
    r"""\bf["']([A-Za-z_]+(?:\.[A-Za-z_]+)*\.?)\{|["']([A-Za-z_]+(?:\.[A-Za-z_]+)*\.)["']\s*\+"""
)
_IN_HANDLER = re.compile(r"refuse_unless_local|refuse_remote|request_is_local")


def local_only_names(source: str) -> set[str]:
    """The local-only setting keys SOURCE names: quoted keys, and the literal head of a key built
    from an f-string or a concatenation (D-SEC-10)."""
    from kestrel.web.settings_io import LOCAL_ONLY_EXACT, needs_local

    names = {m for m in _LITERAL_KEY.findall(source)
             if (m in LOCAL_ONLY_EXACT and m != "profile")
             or ("." in m and not m.endswith(".") and needs_local(m))}
    for fstr, concat in _BUILT_KEY.findall(source):
        head = fstr or concat
        probe = head if head.endswith(".") else head + "."
        if head in ("profile", "profile.") or not ("." in head or head.startswith("hosted")):
            continue
        if needs_local(probe + "x") or needs_local(head):
            names.add(head + "{...}")
    return names


def unguarded_writers(route_table) -> list[str]:
    bad = []
    for method, path, route in route_table:
        if method in ("GET", "OPTIONS"):
            continue
        key = f"{method} {path}"
        own = inspect.getsource(route.endpoint)
        reached = own + _reach(route.endpoint, 2)
        names = local_only_names(reached)
        if not (names or LOCAL_ONLY_WRITER_CALLS.search(reached)):
            continue
        if _guarded(route) or (key in CONDITIONAL and _IN_HANDLER.search(own)):
            continue
        bad.append(f"{key} {sorted(names)}")
    return bad


def test_every_route_that_writes_a_local_only_key_is_guarded(route_table):
    """Every non-GET route whose handler (or a helper it calls) names a key of
    settings_io's local-only table (quoted, or built from an f-string or a concatenation), or
    calls a helper that writes one, either carries the dependency or decides in the handler (the
    CONDITIONAL routes, each proved behaviorally)."""
    bad = unguarded_writers(route_table)
    assert not bad, "writes a local-only setting without the guard: " + "; ".join(bad)


def test_the_writer_check_catches_a_built_key_and_a_quoted_one():
    """The check is not vacuous: a new route writing f"llm.tiers.{x}.model" (or the same by
    concatenation, or a quoted key) without the guard is reported; one with it is not."""
    router = APIRouter()

    @router.post("/brand/new-built")
    async def built(request, tier: str = "tier1"):
        key = f"llm.tiers.{tier}.model"
        return key

    @router.post("/brand/new-concat")
    async def concat(request, tier: str = "tier1"):
        return "llm.tiers." + tier + ".backend"

    @router.post("/brand/new-quoted")
    async def quoted(request):
        return "notify.webhook_url"

    @router.post("/brand/new-ok", dependencies=[LOCAL_ONLY])
    async def ok(request, tier: str = "tier1"):
        return f"llm.tiers.{tier}.model"

    @router.post("/brand/new-plain")
    async def plain(request, tier: str = "tier1"):
        return f"ui.{tier}"

    app = FastAPI()
    app.include_router(router)
    got = unguarded_writers(_table(app.routes))
    assert sorted(g.split(" [")[0] for g in got) == [
        "POST /brand/new-built", "POST /brand/new-concat", "POST /brand/new-quoted",
    ]


# ---- the runtime backstop: no request from a key holder writes a local-only setting row
PLAUSIBLE = {
    "tier": "tier1", "tier_name": "tier2", "backend": "frontier", "model": "vendor/model-a",
    "mode": "hosted", "run_mode": "hosted", "provider": "openrouter", "enabled": "on",
    "value": "1", "on": "1", "key": "llm.tiers.tier1.backend", "api_key": "sk-invented-key",
    "base_url": "http://127.0.0.1:9/v1", "url": "http://127.0.0.1:9/", "topic": "invented",
    "webhook_url": "http://127.0.0.1:9/hook", "port": "9111", "confirmed": "1", "ack": "1",
    "acknowledge": "1", "bind_all": "on", "autostart": "1", "close_to_tray": "0",
    "keep_awake": "1", "name": "invented", "id": "1", "text": "invented", "q": "invented",
    "llm.tiers.tier1.backend": "frontier", "llm.tiers.tier2.model": "vendor/model-a",
    "llm.mode": "hosted", "hosted_mode": "on", "mcp.enabled": "true", "server.bind_all": "true",
    "updates.enabled": "true", "ui.autostart": "true", "keep_awake_on": "on",
    "notify.webhook_url": "http://127.0.0.1:9/hook", "research.searx_url": "http://127.0.0.1:9/",
    "ingest.user_agent": "Invented/1.0", "confidential_mode": "on",
}
# Routes the walk does not drive: they would stop the test process or wait on the outside world.
WALK_SKIP = ("/quit", "/shutdown", "/restart", "/updates/install")


class _SettingWrites:
    """Every key an executed statement writes to the setting table, whatever code issued it."""

    def __init__(self):
        self.keys: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        text = statement.lower()
        if not re.search(r"\b(insert into|update|delete from)\s+\"?setting\"?\b", text):
            return
        rows = parameters if executemany else [parameters]
        for row in rows:
            values = row.values() if isinstance(row, dict) else (row or ())
            self.keys += [v for v in values if isinstance(v, str)]


def local_only_writes(route_table, client) -> list[str]:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from kestrel.web.settings_io import needs_local

    rec = _SettingWrites()
    event.listen(Engine, "before_cursor_execute", rec)
    out: set[str] = set()
    try:
        for method, path, _route in route_table:
            if method in ("GET", "OPTIONS", "HEAD") or path.endswith(WALK_SKIP):
                continue
            for kw in ({"data": PLAUSIBLE}, {"json": PLAUSIBLE}):
                rec.keys.clear()
                try:
                    client.request(method, _fill(path), headers=KEY_H, **kw)
                except Exception:  # noqa: BLE001 - a route that blows up wrote nothing we need
                    continue
                out |= {f"{method} {path} wrote {k}" for k in rec.keys
                        if ("." in k or k in ("keep_awake", "profile")) and needs_local(k, True)}
    finally:
        event.remove(Engine, "before_cursor_execute", rec)
    return sorted(out)


def test_no_request_from_a_key_holder_writes_a_local_only_setting(
        route_table, monkeypatch, no_network):
    """The runtime check the source scan cannot make: drive every non-GET route as a key holder
    with plausible form and JSON data and watch the statements that reach the setting table."""
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        wrote = local_only_writes(route_table, c)
    assert not wrote, f"a key holder wrote a local-only setting: {wrote}"


def test_the_runtime_check_sees_an_unguarded_writer(monkeypatch):
    """Not vacuous: a synthetic route writing f"llm.tiers.{x}.model" with no guard is caught."""
    from kestrel.db.models import Setting
    from kestrel.web.deps import sessionmaker

    router = APIRouter()

    @router.post("/brand/new/{tier}")
    async def sneaky(tier: str):
        async with sessionmaker()() as s:
            s.add(Setting(key=f"llm.tiers.{tier}.model", value="evil"))
            await s.commit()
        return "ok"

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        got = local_only_writes(_table(app.routes), c)
    assert got == ["POST /brand/new/{tier} wrote llm.tiers.1.model"]


def test_the_generated_api_pages_are_not_served_to_anyone(monkeypatch):
    """/docs, /redoc and /openapi.json list every route; nothing in Kestrel reads them, so they
    are off for a key holder and for this computer alike (D-SEC-10)."""
    auth.set_cached_key(KEY)
    app = _app(monkeypatch, tunnel=True)
    for peer in (LAN_PEER, "127.0.0.1"):
        with _client(app, peer) as c:
            for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
                r = c.get(path, headers=KEY_H)
                assert r.status_code == 404, (peer, path)
                assert r.json() == {"detail": "Not Found"}, (peer, path)


# ---- outbound destinations and outbound behavior are local-only (D-SEC-10)
OUTBOUND_KEYS = ("notify.webhook_url", "notify.ntfy_topic", "research.searx_url",
                 "ingest.user_agent", "ingest.respect_robots", "updates.feed_url")


@pytest.mark.parametrize("key", OUTBOUND_KEYS)
def test_a_key_holder_cannot_set_an_outbound_destination_by_undo(key, monkeypatch):
    """/settings/undo writes any notify.* row: the webhook and ntfy topic were reachable by it."""
    from kestrel.web import settings_io as io

    assert io.needs_local(key, "x")
    auth.set_cached_key(KEY)
    _seed(key, "http://127.0.0.1:1/old")
    before = _setting(key)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post(f"/settings/undo?key={key}&prev=%22http%3A%2F%2Fevil.example%2F%22&now=absent",
                   headers=KEY_H)
    assert _setting(key) == before
    assert r.status_code == (403 if io.undoable(key) else 200), key  # refused, not a no-op


# D-SEC-16: a key holder can never write a local-only setting, whatever key shape they send: a
# key INSIDE a local-only one is refused like the key itself.
@pytest.mark.parametrize("key", ["notify.webhook_url.x", "ui.autostart.extra",
                                 "notify.ntfy_topic.y"])
def test_undo_refuses_a_key_inside_a_local_only_key(key, monkeypatch):
    from kestrel.web import settings_io as io

    assert io.undoable(key) and io.needs_local(key, True)
    auth.set_cached_key(KEY)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        before = _data_fingerprint()
        r = c.post(f"/settings/undo?key={key}&prev=true&now=absent", headers=KEY_H)
    assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE)
    assert _data_fingerprint() == before


def _yaml_confidential_on() -> None:
    import yaml

    paths.seed_config()
    yml = paths.config_dir() / "settings.yml"
    tree = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    tree["confidential_mode"] = True
    yml.write_text(yaml.safe_dump(tree), encoding="utf-8")


def _drop(key):
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        con.execute("delete from setting where key = ?", (key,))
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("where", ["yaml", "db"])
def test_settings_form_cannot_turn_off_confidential_mode_set_anywhere(where, monkeypatch):
    """D-SEC-16: the EFFECTIVE value decides. On in settings.yml with no row, a key holder's
    POST /settings without the field used to write False."""
    auth.set_cached_key(KEY)
    _drop("confidential_mode")
    if where == "yaml":
        _yaml_confidential_on()
    else:
        _seed("confidential_mode", True)
    app = _app(monkeypatch, tunnel=True)
    for peer, headers in ((LAN_PEER, KEY_H), ("127.0.0.1", {**KEY_H, "Host": TUNNEL})):
        with _client(app, peer) as c:
            r = c.post("/settings", headers=headers, data={"concurrency.tier1": "2"})
        assert (r.status_code, r.text) == (403, LOCAL_ONLY_CHANGE)
        assert _setting("confidential_mode") in ((None,) if where == "yaml" else ("true", 1))
    # This computer may turn it off, wherever it was set.
    with _client(app, "127.0.0.1") as c:
        r = c.post("/settings", data={"concurrency.tier1": "2"})
    assert r.status_code == 200 and _setting("confidential_mode") in ("false", 0)


def test_a_key_holder_cannot_lower_redaction_of_online_sends(monkeypatch):
    """D-SEC-17: the only generic writer that takes a caller's key is Undo; it refuses this key."""
    from kestrel.web import settings_io as io

    key = "llm.hosted_mode.redact_confidential"
    assert io.needs_local(key, False) and io.needs_local("llm.hosted_mode", {})
    auth.set_cached_key(KEY)
    _seed(key, True)
    with _client(_app(monkeypatch, tunnel=True), LAN_PEER) as c:
        r = c.post(f"/settings/undo?key={key}&prev=false&now=true", headers=KEY_H)
    assert r.status_code in (200, 403)
    assert _setting(key) in ("true", 1)
