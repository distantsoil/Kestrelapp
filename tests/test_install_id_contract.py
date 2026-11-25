"""The install ID contract (D-INST-2), enumerating rather than sampling.

Invariant: the install ID never leaves the computer except when the person copies it. It is in
no request Kestrel makes (update check, model calls local or online, source fetches, alerts, the
balance check, anything on `requests` such as JobSpy), it is in no page served to another
device, and `ids.salt` is never used for it.

Four parts:
1. Readers. Only the install ID module reads its row; every other settings read goes through a
   builder that excludes the key (kestrel/db/setting_access.py, D-INST-10). SQLite's authorizer
   enforces it on every statement that reads setting.value (tests/sql_row_guard.py).
2. Bulk reads. A whole-table read is a settings read like any other: it goes through a builder,
   or it is the restore's named whole-table copy, allowed only for its own SQL.
3. Requests. With a known ID stored and every httpx transport, `requests` adapter, curl_cffi
   session, socket and name lookup recording, the update check, local and online model calls,
   source and SEC fetches, alerts and the balance check each build a real request; the full,
   short and dashless forms of the ID appear in no URL, header or body. Every route in every
   method, every MCP tool and every process start are driven from the route table in
   tests/test_install_id_egress.py (route_driver), so a new route is covered the day it exists.
4. Pages to another device. Every route, requested as a key holder on the LAN, carries no form
   of the ID.

All values are invented. Nothing reaches the network: the transports answer locally."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from kestrel.db.models import Notification, Setting
from kestrel.services import install_id
from tests import route_driver, sql_row_guard

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "kestrel"

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# An invented ID, planted before every driver runs.
PLANTED = "5c1e8a2f-3b4d-4e6f-9a0b-c1d2e3f4a5b6"

# Who may touch the install ID's table (round 6, D-INST-15, D-INST-16): the install ID module,
# by call site, enforced by SQLite's authorizer on every connection (tests/sql_row_guard.py),
# never a scan of values or code. The restore's whole-table copy is allowed only for its own SQL
# (it does not read this table: install_identity is never imported, D-INST-17).
ROW_READERS = {
    "kestrel/services/backup.py::_import_rows": "restore: copies the backup's tables as rows",
    "kestrel/services/backup.py::_strip_secrets": (
        "backup: VACUUM rewrites the snapshot the person keeps; the export carries the table "
        "on purpose (D-INST-2)"),
}


def _sweep_and_restore(tmp_path) -> None:
    """Start the app, request every route in every method from this computer (route_driver,
    the route table decides), save a diagnostics file, then back up and restore."""
    from starlette.testclient import TestClient

    from kestrel.config import get_settings
    from kestrel.db.engine import reset_engine
    from kestrel.services import diagnostics
    from kestrel.services.backup import create_backup, restore_backup
    from kestrel.web.app import build_app

    asyncio.run(reset_engine())
    route_driver.seed()
    app = build_app(worker=False)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        route_driver.drive(c, app)
    assert diagnostics.summary(get_settings(), lambda s: s)["install_id"] == PLANTED
    restore_backup(create_backup(tmp_path))


# Sweeps every route (O(routes)): 64.9 s and 72.4 s alone on the owner's Windows PC, both at or
# over pyproject's 60 s default, whose thread method there stops the xdist worker with no
# traceback ("worker gw2 crashed"). The sweep's size, not a wait, is the cost (D-TEST-797).
@pytest.mark.timeout(240)
def test_only_the_accessor_touches_the_identity_table(monkeypatch, tmp_path):
    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        _sweep_and_restore(tmp_path)
    assert seen.violations == [], sorted(set(seen.violations))
    assert seen.unguarded == [], seen.unguarded
    # The guard was on (not vacuous): engines connected under it and the accessor was seen.
    assert seen.engine_connections > 0
    assert sql_row_guard.ACCESSOR in seen.allowed_reads


def test_a_middleware_frame_passes_through_only_when_it_is_not_the_caller(monkeypatch):
    """D-INST-30 (#204 with #212): the __call__ of a middleware the app registers is skipped in
    the chain, found from the app's own stack; as the direct caller of the install ID module it
    is kept, so a middleware that reads the ID itself is a stranger."""
    import traceback

    from kestrel.web.csp import SecurityHeaders

    root = sql_row_guard.ROOT
    lines = sql_row_guard._middleware_lines(SecurityHeaders(app=None))
    assert set(lines) == {"kestrel/web/csp.py"}
    monkeypatch.setattr(sql_row_guard, "_pass_through", lines)
    line = min(lines["kestrel/web/csp.py"])

    def frame(rel: str, name: str, lineno: int = 1):
        return traceback.FrameSummary(str(root / rel), lineno, name)

    mw = frame("kestrel/web/csp.py", "__call__", line)
    page = frame("kestrel/web/pages/about.py", "page")
    acc = frame(sql_row_guard.ACCESSOR, "read_record")
    assert sql_row_guard.chain_of([mw, page, acc]) == [
        "kestrel/web/pages/about.py::page", f"{sql_row_guard.ACCESSOR}::read_record"]
    assert "kestrel/web/csp.py::__call__" in sql_row_guard.chain_of([mw, acc])
    assert "kestrel/web/csp.py::__call__" in sql_row_guard.chain_of([page, mw])
    # Another line of the same file is not the middleware's __call__.
    other = frame("kestrel/web/csp.py", "frame_css", max(lines["kestrel/web/csp.py"]) + 50)
    assert "kestrel/web/csp.py::frame_css" in sql_row_guard.chain_of([other, page, acc])
    monkeypatch.setattr(sql_row_guard, "_pass_through", {})
    assert "kestrel/web/csp.py::__call__" in sql_row_guard.chain_of([mw, page, acc])


def _raw_read(sql: str, params: tuple = ()) -> list:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("sql", [
    "SELECT install_id FROM install_identity",
    "SELECT upper(install_id) FROM install_identity",
    "SELECT hex(install_id) FROM install_identity WHERE slot = 1",
    "SELECT substr(install_id, 10, 12) FROM install_identity",
    "SELECT key FROM setting WHERE key IN (SELECT install_id FROM install_identity)"
    " OR 1 = 1",
    "SELECT a.install_id FROM setting AS s JOIN install_identity AS a ON 1 = 1",
    "SELECT (SELECT replace(install_id, '-', '') FROM install_identity)",
    "UPDATE install_identity SET version = 'x'",
    "DELETE FROM install_identity WHERE slot = 2",
], ids=["plain", "upper", "hex", "substr-middle", "subquery", "alias-join", "scalar-subquery",
        "update", "delete"])
def test_any_raw_touch_of_the_table_outside_the_accessor_is_caught(monkeypatch, sql):
    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        _raw_read(sql)
    assert seen.violations, sql


def test_a_sync_engine_core_read_is_caught(monkeypatch):
    """The round 6 finding: SQLAlchemy's sync engine opens its connections through
    sqlite3.dbapi2.connect, which the guard now covers, and its pool is checked too."""
    from sqlalchemy import create_engine, func, select

    from kestrel import paths
    from kestrel.db.models import InstallIdentity

    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        engine = create_engine(f"sqlite:///{paths.db_path().as_posix()}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(select(func.upper(InstallIdentity.install_id))).all()
        finally:
            engine.dispose()
    assert rows == [(PLANTED.upper(),)]
    assert seen.violations and seen.unguarded == [] and seen.engine_connections >= 1


def test_an_async_engine_orm_read_is_caught(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from kestrel.db.engine import get_sessionmaker, reset_engine
    from kestrel.db.models import InstallIdentity

    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        asyncio.run(reset_engine())
        alias = aliased(InstallIdentity)

        async def go():
            async with get_sessionmaker()() as s:
                return (await s.execute(
                    select(alias.install_id).join(Setting, Setting.key != alias.install_id)
                    .union_all(select(InstallIdentity.install_id)))).all()

        rows = asyncio.run(go())
        asyncio.run(reset_engine())
    assert (PLANTED,) in rows
    assert seen.violations and seen.unguarded == [] and seen.engine_connections >= 1


def test_an_engine_that_opens_connections_another_way_is_reported(monkeypatch):
    """The pool check is a hatch of its own (AGENT-RULES 8a.6): a connection made around the
    patched connect is recorded as unguarded."""
    import sqlite3

    from sqlalchemy import create_engine, text

    real = sqlite3.connect
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        engine = create_engine("sqlite://", creator=lambda: real(":memory:"))
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    assert seen.unguarded == ["Connection"]


def test_reads_of_other_tables_are_not_flagged(monkeypatch):
    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        rows = _raw_read("SELECT key, value FROM setting")
        names = _raw_read("SELECT name FROM sqlite_master WHERE name = 'install_identity'")
    assert names and isinstance(rows, list)
    assert seen.violations == []


def test_code_a_named_copy_calls_gets_no_allowance_for_its_own_sql(monkeypatch, tmp_path):
    """Round 5, item 4, kept: the named copy is allowed only for the SQL it runs itself. Code it
    calls (its identifier quoting helper, swapped for one that reads the table) is caught,
    though the copy is on the stack."""
    import kestrel.services.backup as bk

    _fresh_db_with_planted_id()
    archive = bk.create_backup(tmp_path)
    real_q = bk._q
    read: list = []

    def probing_q(name):
        if not read and sys._getframe(1).f_code is bk._import_rows.__code__:
            read.extend(_raw_read("SELECT hex(install_id) FROM install_identity"))
        return real_q(name)

    monkeypatch.setattr(bk, "_q", probing_q)
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        bk.restore_backup(archive)
    assert read and seen.violations
    assert any("probing_q" in v for v in seen.violations), seen.violations


def test_the_accessors_own_sql_run_by_another_caller_is_caught(monkeypatch):
    """Statement caching is off under the guard: the accessor's prepared SQL, run again on the
    same connection by other code, is authorized again and caught."""
    import sqlite3

    from kestrel import paths

    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        con = sqlite3.connect(str(paths.db_path()))
        try:
            assert install_id.read_record_sync(con)["id"] == PLANTED
            assert seen.violations == []
            row = con.execute(install_id._SELECT_SYNC).fetchone()
        finally:
            con.close()
    assert row is not None and seen.violations


def test_the_one_time_move_may_write_the_table_and_nothing_else_may(monkeypatch, tmp_path):
    """Migration 0041 writes the record it moves: allowed by call site, as the accessor is."""
    import sqlite3

    from alembic import command

    from kestrel import paths
    from kestrel.db import migrate

    db = tmp_path / "k.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        command.upgrade(cfg, "0034")
        con = sqlite3.connect(str(db))
        con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, 'u')",
                    (install_id.KEY, json.dumps({"id": PLANTED})))
        con.commit()
        con.close()
        with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
            command.upgrade(cfg, "0041")
    assert seen.violations == [] and sql_row_guard.MOVE in seen.allowed_reads


@pytest.mark.parametrize("call", ["read_record_sync", "ensure"])
def test_a_caller_outside_the_allowlist_is_caught_though_the_read_is_the_accessors(
        monkeypatch, call):
    """Round 7: a new caller of the install ID module (here code compiled as an MCP tool) gets
    the ID through the accessor's own read; the guard reports the caller, not the read."""
    from kestrel import paths

    source = (
        "import asyncio, sqlite3\n"
        "from kestrel.services import install_id\n"
        "def leak(db):\n"
        "    if MODE == 'read_record_sync':\n"
        "        con = sqlite3.connect(db)\n"
        "        try:\n"
        "            return install_id.read_record_sync(con)['id']\n"
        "        finally:\n"
        "            con.close()\n"
        "    from kestrel.db.engine import get_sessionmaker\n"
        "    async def go():\n"
        "        async with get_sessionmaker()() as s:\n"
        "            return (await install_id.ensure(s))['id']\n"
        "    return asyncio.run(go())\n"
    )
    scope: dict = {"MODE": call}
    exec(compile(source, str(ROOT / "kestrel" / "mcp" / "server.py"), "exec"), scope)  # noqa: S102 - test: fixed text
    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        from kestrel.db.engine import reset_engine

        asyncio.run(reset_engine())
        got = scope["leak"](str(paths.db_path()))
        asyncio.run(reset_engine())
    assert got == PLANTED
    assert any(v.startswith("kestrel/mcp/server.py::") for v in seen.violations), seen.violations


def test_a_new_caller_of_an_allowed_function_is_caught(monkeypatch):
    """Round 8: an allowed function does not lend its permission. Code compiled as an MCP tool that
    calls diagnostics.install_id_value (itself in CALLERS) is a stranger in the chain."""
    from kestrel import paths

    source = (
        "import sqlite3\n"
        "from kestrel.services import diagnostics\n"
        "def tool(db):\n"
        "    con = sqlite3.connect(db)\n"
        "    try:\n"
        "        return diagnostics.install_id_value(con)\n"
        "    finally:\n"
        "        con.close()\n"
    )
    scope: dict = {}
    exec(compile(source, str(ROOT / "kestrel" / "mcp" / "server.py"), "exec"), scope)  # noqa: S102 - test: fixed text
    _fresh_db_with_planted_id()
    with sql_row_guard.guard(monkeypatch, ROW_READERS) as seen:
        got = scope["tool"](str(paths.db_path()))
    assert got == PLANTED
    assert any("kestrel/mcp/server.py::tool" in v for v in seen.violations), seen.violations


# ------------------------------------------------------------------------------ requests
def _forms(full: str) -> list[str]:
    bare = full.replace("-", "")
    short = install_id.short_form(full)
    return [full, full.upper(), bare, bare.upper(), short, short.lower(), bare[:8],
            bare[:8].upper()]


class Recorder:
    """Every request any httpx client makes; each is answered here with an invented reply."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sockets: list[str] = []  # connections that bypassed every recorded client
        self.lookups: list[str] = []  # name lookups that bypassed them
        self.udp_connects: list[str] = []  # route lookups only: nothing is sent

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        request.read()
        path = request.url.path
        if path.endswith("/chat/completions"):
            body = {"choices": [{"message": {"content": '{"x":"ok"}'}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            return httpx.Response(200, json=body, request=request)
        if path.endswith("/models") or path.endswith("/api/tags"):
            return httpx.Response(200, json={"data": [], "models": []}, request=request)
        if "credits" in path or "key" in path:
            return httpx.Response(200, json={"data": {}}, request=request)
        return httpx.Response(200, text="<html><body>invented</body></html>", request=request)

    def leaks(self, full: str) -> list[str]:
        out = [f"lookup {h} carries {f!r}" for h in self.lookups for f in _forms(full)
               if f.lower() in h.lower()]
        for req in self.requests:
            parts = [str(req.url), json.dumps(dict(req.headers)),
                     req.content.decode("utf-8", "replace")]
            blob = "\n".join(parts)
            for form in _forms(full):
                if form in blob:
                    out.append(f"{req.method} {req.url} carries {form!r}")
        return out


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()

    async def ahandle(self, request):
        return rec.answer(request)

    def handle(self, request):
        return rec.answer(request)

    # The real transports only; TestClient talks to the app through its own.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)

    # Anything on `requests`: recorded the same way, then refused.
    import requests

    def rsend(self, request, *a, **kw):
        body = request.body or b""
        rec.requests.append(httpx.Request(request.method, request.url,
                                          headers=dict(request.headers),
                                          content=body.encode() if isinstance(body, str) else body))
        raise requests.ConnectionError("test: the network is closed")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", rsend)

    # JobSpy's TLS sessions use curl_cffi (libcurl in C), below every Python socket: recorded at
    # its own request method, then refused.
    from curl_cffi import requests as curl_requests

    def curl_request(self, method, url, *a, **kw):
        body = kw.get("data") or kw.get("json") or b""
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, default=str)
        hdrs = {**dict(getattr(self, "headers", {}) or {}), **dict(kw.get("headers") or {})}
        rec.requests.append(httpx.Request(method, str(url), headers={
            str(k): str(v) for k, v in hdrs.items()}, params=kw.get("params") or None,
            content=body.encode() if isinstance(body, str) else body))
        raise curl_requests.RequestsError("test: the network is closed")

    monkeypatch.setattr(curl_requests.Session, "request", curl_request)

    async def public(host, port):
        return ["93.184.216.34"]

    from kestrel.ingest import http as ingest_http

    monkeypatch.setattr(ingest_http, "_resolve", public)

    # Below every client: a network connection or a name lookup that did not go through a
    # recorded client is itself recorded (and refused), so a new client path cannot slip past by
    # using another library. The host name of a lookup is checked for the ID like any URL.
    import socket

    def connect(self, address, *a, **kw):
        inet = self.family in (socket.AF_INET, socket.AF_INET6)
        if inet and not getattr(_in_socketpair, "on", False):
            if self.type == socket.SOCK_DGRAM:
                # A UDP connect only picks a route (auth._routed_ip): no packet leaves. Anything
                # sent on the socket afterwards is recorded and refused below.
                rec.udp_connects.append(repr(address))
                return real_connect(self, address, *a, **kw)
            rec.sockets.append(repr(address))
            raise ConnectionRefusedError("test: the network is closed")
        return real_connect(self, address, *a, **kw)

    def send(self, data, *a):
        if self.family in (socket.AF_INET, socket.AF_INET6) and self.type == socket.SOCK_DGRAM:
            rec.sockets.append(f"udp send {bytes(data)[:64]!r}")
            rec.lookups.append(bytes(data).decode("latin-1"))
            raise ConnectionRefusedError("test: the network is closed")
        return real_send(self, data, *a)

    def connect_ex(self, address, *a, **kw):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            rec.sockets.append(repr(address))
            return 111
        return real_connect_ex(self, address, *a, **kw)

    def getaddrinfo(host, *a, **kw):
        rec.lookups.append(str(host))
        raise socket.gaierror("test: no name lookups")

    def lookup(host, *a, **kw):
        rec.lookups.append(str(host))
        raise socket.gaierror("test: no name lookups")

    def sendto(self, data, *args):
        rec.sockets.append(f"udp {args[-1]!r}")
        rec.lookups.append(bytes(data).decode("latin-1"))  # a DNS query carries the name
        raise ConnectionRefusedError("test: the network is closed")

    # On Windows socket.socketpair() is built from two loopback TCP sockets (the event loop makes
    # one at start-up). Only that call's own connect is let through; every other connect,
    # loopback included, is still recorded and refused.
    import threading

    _in_socketpair = threading.local()
    real_socketpair = socket.socketpair

    def socketpair(*a, **kw):
        _in_socketpair.on = True
        try:
            return real_socketpair(*a, **kw)
        finally:
            _in_socketpair.on = False

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_send = socket.socket.send
    monkeypatch.setattr(socket.socket, "send", send)
    monkeypatch.setattr(socket.socket, "sendall", send)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for name in ("gethostbyname", "gethostbyname_ex", "getnameinfo"):
        monkeypatch.setattr(socket, name, lookup)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    return rec


def _fresh_db_with_planted_id() -> None:
    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import get_sessionmaker, reset_engine

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())

    import sqlite3

    con = sqlite3.connect(str(paths.db_path()))
    con.execute("INSERT INTO install_identity (slot, install_id, created_at, version, updated_at)"
                " VALUES (1, ?, '2026-01-02T03:04:05+00:00', '0.0.1', 'u')", (PLANTED,))
    con.commit()
    con.close()

    async def check():
        async with get_sessionmaker()() as s:
            assert (await install_id.ensure(s))["id"] == PLANTED

    asyncio.run(check())


def _settings():
    from kestrel.config import get_settings

    return get_settings()


async def _drive_update_check():
    from kestrel.services import updater

    await updater.fetch_feed("https://updates.example/latest.json", None)


async def _drive_model_calls(monkeypatch):
    from kestrel.config import BackendConfig
    from kestrel.llm.client import LLMClient

    msgs = [{"role": "system", "content": "invented"}, {"role": "user", "content": "invented"}]
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    # Local (Ollama) first: whatever tier the shipped config names.
    s = _settings()
    async with httpx.AsyncClient() as http:
        tier = next(iter(s.llm.tiers))
        await LLMClient(s, http=http).complete(tier=tier, messages=msgs, schema=schema)
    # Online, with an invented key.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    s = _settings()
    s.llm.backends["frontier"] = BackendConfig(
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        json_mode="json_schema", thinking_control="none", external=True, provider="openrouter",
    )
    s.llm.tiers["frontier"].backend = "frontier"
    s.llm.tiers["frontier"].model = "invented/model"
    async with httpx.AsyncClient() as http:
        await LLMClient(s, http=http).complete(
            tier="frontier", messages=msgs, schema=schema, allow_external=True)


async def _drive_source_fetch():
    from kestrel.ingest.http import HttpClient

    ua = _settings().ingest.user_agent
    async with HttpClient(user_agent=ua, respect_robots=False, max_attempts=1) as client:
        await client.get_text("https://boards.example/jobs")
    # SEC EDGAR goes through the same client with the descriptive User-Agent.
    async with HttpClient(user_agent=ua, respect_robots=False, max_attempts=1) as client:
        await client.get_text("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent")


async def _drive_balance_check(monkeypatch):
    from kestrel.config import BackendConfig
    from kestrel.services import hosted_balance

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    await hosted_balance._fetch(BackendConfig(
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        external=True, provider="openrouter"))


async def _drive_jobspy():
    """The real JobSpy adapter, with a search that names nothing about the person."""
    import contextlib

    from kestrel.ingest.adapters.jobspy import JobSpyAdapter
    from kestrel.ingest.base import AdapterError

    # Every request is refused, so the scrape fails; the requests it built are what is checked.
    with contextlib.suppress(AdapterError):
        await JobSpyAdapter().fetch({"sites": ["indeed"], "search_term": "invented role",
                                     "location": "Invented City", "results_wanted": 1})


def _drive_requests_library():
    import requests

    with pytest.raises(requests.ConnectionError):
        requests.get("https://jobs.example/search", headers={"User-Agent": "invented"}, timeout=1)


async def _drive_model_catalog(monkeypatch):
    from kestrel.config import BackendConfig
    from kestrel.llm import providers

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-invented")
    await providers.list_models(BackendConfig(
        base_url="https://catalog.example/api/v1", api_key_env="OPENROUTER_API_KEY",
        external=True, provider="openrouter"))


async def _drive_alerts():
    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.delivery import deliver_pending

    s = _settings()
    s.notify.enabled = True
    s.notify.toast = False
    s.notify.ntfy_topic = "invented-topic"
    s.notify.webhook_url = "https://hooks.example/kestrel"
    s.notify.events = {}
    s.notify.quiet_hours = {"enabled": False, "start": "21:00", "end": "07:00"}
    async with get_sessionmaker()() as session:
        session.add(Notification(kind="run_finished", title="Run finished", severity="info",
                                 delivered_channels=[], created_at=utcnow_iso()))
        await session.commit()
        await deliver_pending(session, s)


def test_no_outbound_client_sends_the_install_id(recorder, monkeypatch):
    _fresh_db_with_planted_id()

    async def go():
        await _drive_update_check()
        await _drive_model_calls(monkeypatch)
        await _drive_source_fetch()
        await _drive_alerts()
        await _drive_balance_check(monkeypatch)
        await _drive_model_catalog(monkeypatch)
        await _drive_jobspy()

    asyncio.run(go())
    _drive_requests_library()
    hosts = {r.url.host for r in recorder.requests}
    # The drivers really sent: each client reached the recorder.
    for host in ("updates.example", "openrouter.ai", "boards.example", "www.sec.gov",
                 "ntfy.sh", "hooks.example", "jobs.example", "catalog.example"):
        assert host in hosts, (host, sorted(hosts))
    assert any(r.url.path.endswith("/key") for r in recorder.requests)  # the balance check
    # JobSpy reached its board through curl_cffi, and was recorded.
    assert any("indeed" in (h or "") for h in hosts), sorted(hosts)
    # The local model call too (Ollama on this computer counts: the rule is every request).
    assert hosts & {"localhost", "127.0.0.1"}, sorted(hosts)
    assert recorder.leaks(PLANTED) == []
    assert recorder.sockets == [] and recorder.lookups == [], (recorder.sockets, recorder.lookups)


def test_the_recorder_would_see_a_leak(recorder):
    """The check above can fail: a request that carries the ID is reported in every form."""
    async def leak():
        async with httpx.AsyncClient() as http:
            short = install_id.short_form(PLANTED)
            await http.get("https://updates.example/x", headers={"X-Id": short})
            await http.post("https://hooks.example/y", content=PLANTED.replace("-", ""))

    asyncio.run(leak())
    assert len(recorder.leaks(PLANTED)) >= 2


def test_the_guard_sees_a_client_that_bypasses_the_recorded_ones(recorder):
    """urllib, a raw socket and a host name built from the ID: each is recorded and refused."""
    import socket
    import urllib.error
    import urllib.request

    host = f"{PLANTED.replace('-', '')[:8]}.leak.example"
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://{host}/", timeout=1)  # noqa: S310 - test: refused
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(("203.0.113.9", 80))
    finally:
        s.close()
    with pytest.raises(socket.gaierror):
        socket.gethostbyname(f"b.{host}")
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            u.sendto(f"q {host}".encode(), ("203.0.113.53", 53))
    finally:
        u.close()
    assert host in recorder.lookups and f"b.{host}" in recorder.lookups
    assert any(s.startswith("udp") for s in recorder.sockets)
    assert recorder.leaks(PLANTED)


# Drives every route in every method as a key holder on another device: 26.7 s alone on the
# owner's Windows PC (measured 2026-11-24, -n 8, --timeout=0), and under the gate's -n 8 load
# that passes pyproject's 60 s default, whose thread timeout on Windows stops the worker. The
# sweep's size, not a wait, is the cost; same sizing as the marked sweep above (D-TEST-860).
@pytest.mark.timeout(240)
def test_no_page_served_to_another_device_carries_the_install_id(recorder):
    """Every route in every method (route_driver: the route table decides), requested by a key
    holder on another device: the ID is in none of the responses (it is shown on this computer
    only)."""
    _fresh_db_with_planted_id()
    route_driver.seed()
    from starlette.testclient import TestClient

    from kestrel.web import auth
    from kestrel.web.app import build_app

    app = build_app(worker=False)
    auth.set_cached_key("k204-sweep")
    try:
        with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False,
                        client=("192.168.1.50", 1)) as c:
            driven = route_driver.drive(c, app, headers={"X-Kestrel-Key": "k204-sweep"})
            about = c.get("/about", headers={"X-Kestrel-Key": "k204-sweep"}).text
    finally:
        auth.reset_cache()
    for d in driven:
        for form in _forms(PLANTED):
            assert form not in d.response.text, (d.method, d.path, form)
    served = sum(d.served for d in driven)
    assert "Shown on this computer only." in about and served > 20


# A loopback peer is this computer only with a local Host and no proxy header (auth.is_loopback,
# #151, #155). A tunnel connects from 127.0.0.1, so each of these is another device.
TUNNEL_CASES = [
    # An unlisted public name is refused outright (400), before any page is drawn.
    pytest.param({"Host": "kestrel.example.org"}, False, id="public-host"),
    pytest.param({"Host": "kestrel.example.org"}, True, id="listed-public-host"),
    pytest.param({"X-Forwarded-For": "203.0.113.9"}, False, id="x-forwarded-for"),
    pytest.param({"Forwarded": "for=203.0.113.9"}, False, id="forwarded"),
    pytest.param({"CF-Connecting-IP": "203.0.113.9"}, False, id="cf-connecting-ip"),
    pytest.param({"X-Real-IP": "203.0.113.9"}, False, id="x-real-ip"),
]


@pytest.mark.parametrize(("headers", "listed"), TUNNEL_CASES)
@pytest.mark.parametrize("path", ["/about", "/settings/section/data"])
def test_a_loopback_peer_that_came_through_a_tunnel_never_gets_the_id(
        path, headers, listed, monkeypatch):
    _fresh_db_with_planted_id()
    if listed:
        monkeypatch.setenv("KESTREL_SERVER__PUBLIC_HOSTNAMES", '["kestrel.example.org"]')
    from starlette.testclient import TestClient

    from kestrel.web import auth
    from kestrel.web.app import build_app

    auth.set_cached_key("k204-tunnel")
    try:
        with TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                        raise_server_exceptions=False, client=("127.0.0.1", 1)) as c:
            r = c.get(path, headers={"X-Kestrel-Key": "k204-tunnel", **headers})
            local = c.get(path).text  # the same page from this computer does show it
    finally:
        auth.reset_cache()
    unlisted = "Host" in headers and not listed
    assert r.status_code == (400 if unlisted else 200), (path, r.status_code)
    # Served: the page says where the ID is; refused: no page at all.
    assert ("Shown on this computer only." in r.text) is (not unlisted), path
    for form in _forms(PLANTED):
        assert form not in r.text, (path, form)
    assert install_id.short_form(PLANTED) in local


# ---------------------------------------------------- the guards' own edges (AGENT-RULES 8a.6)
def test_the_socket_guard_records_the_network_and_lets_a_local_unix_socket_through(recorder):
    import shutil
    import socket
    import tempfile

    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("this platform has no Unix sockets (Windows)")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert s.connect_ex(("198.51.100.7", 443)) != 0
    finally:
        s.close()
    assert recorder.sockets == ["('198.51.100.7', 443)"]
    # A Unix socket path is capped near 104 bytes on macOS, and pytest's tmp_path there is longer
    # (/private/var/folders/...), so the missing socket lives in a short directory (D-CI-600).
    short = tempfile.mkdtemp(prefix="ks", dir="/tmp")
    try:
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # a local socket: not the network
        try:
            with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
                u.connect(short + "/no-such.sock")
        finally:
            u.close()
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert recorder.sockets == ["('198.51.100.7', 443)"]


@pytest.fixture
def loopback_socketpair(monkeypatch):
    """socket.socketpair as Windows builds it: a listener and a connect over 127.0.0.1."""
    import socket

    def pair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        lsock = socket.socket(family, type, proto)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen()
            csock = socket.socket(family, type, proto)
            csock.connect(lsock.getsockname())
            ssock, _ = lsock.accept()
        finally:
            lsock.close()
        return ssock, csock

    monkeypatch.setattr(socket, "socketpair", pair)


def test_an_event_loop_starts_where_socketpair_uses_loopback(loopback_socketpair, recorder):
    """Windows' event loop makes a loopback socketpair at start-up; the guard lets that one
    connect through and records nothing."""
    import asyncio

    asyncio.run(asyncio.sleep(0))
    assert recorder.sockets == []


def test_a_loopback_connect_outside_socketpair_is_still_refused(recorder):
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
    assert recorder.sockets == ["('127.0.0.1', 9)"]


def test_a_udp_connect_is_let_through_but_anything_sent_on_it_is_caught(recorder):
    """The UDP-connect allowance is a hatch (AGENT-RULES 8a.6): the route lookup passes, a send
    or sendall on that socket is recorded and refused, with what it carried."""
    import socket

    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            u.connect(("192.0.2.1", 9))
        except OSError:
            pass  # no route in this sandbox: still nothing was sent
        with pytest.raises(ConnectionRefusedError):
            u.send(f"q {PLANTED}".encode())
        with pytest.raises(ConnectionRefusedError):
            u.sendall(b"invented")
    finally:
        u.close()
    assert recorder.udp_connects == ["('192.0.2.1', 9)"]
    assert len([s for s in recorder.sockets if s.startswith("udp send")]) == 2
    assert recorder.leaks(PLANTED)
