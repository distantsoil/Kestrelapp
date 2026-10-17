"""MCP action tools (AFTERACTION 3.1) and the hard-rule-1 boundary they must respect.

There are five action tools: ingest_opportunity, trigger_scan, trigger_evaluation,
set_pursuit_status, add_note. Until this file existed, nothing anywhere asserted that invoking one
of them does not reach the network. That matters more here than for the read tools, because an MCP
client is an LLM agent: these are exactly the entry points an agent would reach for if it decided,
wrongly, to "just apply" on the operator's behalf. Hard rule 1 says the system never acts externally
for the operator, so each test asserts BOTH halves: the local effect happened, and nothing left the
machine.

The outbound guard is enforced at the socket layer rather than by mocking a client, so it catches an
outbound call made through any library, including one added later.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import utcnow_iso
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Org, Pursuit, Source, StatusEvent, Task
from kestrel.mcp.server import build_mcp

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class OutboundCallAttempted(AssertionError):
    """Raised the moment anything tries to open a non-loopback connection."""


@pytest.fixture
def no_outbound(monkeypatch):
    """Fail loudly if any code under test opens a connection off this machine.

    The synchronous socket entry points are NOT sufficient on their own, and an earlier version of
    this fixture that patched only those was worthless. On Windows the default ProactorEventLoop
    reaches the network through an overlapped ConnectEx on the raw handle, so
    `socket.socket.connect` is never called. Every network path in Kestrel is async httpx, so the
    one call an action tool
    would actually make was the one call the guard could not see: a real SYN left the machine and
    the assertion stayed silent.

    So both layers are patched: `asyncio.BaseEventLoop.create_connection`/`sock_connect` (which
    httpx, anyio, and anything else async bottom out in) and the sync socket calls (requests,
    JobSpy). `getaddrinfo` is also covered so a hostname lookup for a third party trips even if the
    connection itself is made some way not anticipated here. Loopback stays allowed: the database,
    the in-process test server, and asyncio's own self-pipe are legitimately local.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo
    real_loop_create = asyncio.base_events.BaseEventLoop.create_connection
    real_sock_connect = asyncio.base_events.BaseEventLoop.sock_connect

    def _fail(host: object) -> None:
        raise OutboundCallAttempted(
            f"an MCP action tool tried to reach {host!r}; hard rule 1 forbids the system acting "
            f"externally on the operator's behalf"
        )

    def _check_host(host: object) -> None:
        if host is not None and host not in _LOOPBACK:
            _fail(host)

    def _check_address(address: object) -> None:
        if isinstance(address, tuple) and address:
            _check_host(address[0])

    def _connect(self, address):
        _check_address(address)
        return real_connect(self, address)

    def _connect_ex(self, address):
        _check_address(address)
        return real_connect_ex(self, address)

    def _create_connection(address, *args, **kwargs):
        _check_address(address)
        return real_create(address, *args, **kwargs)

    def _getaddrinfo(host, *args, **kwargs):
        _check_host(host)
        return real_getaddrinfo(host, *args, **kwargs)

    async def _loop_create_connection(self, protocol_factory, host=None, port=None, **kwargs):
        _check_host(host)
        return await real_loop_create(self, protocol_factory, host, port, **kwargs)

    async def _sock_connect(self, sock, address):
        _check_address(address)
        return await real_sock_connect(self, sock, address)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    monkeypatch.setattr(socket, "create_connection", _create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop, "create_connection", _loop_create_connection
    )
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "sock_connect", _sock_connect)
    return None


async def _seed() -> dict[str, int]:
    """A source, an opportunity, and a pursuit: the rows the five action tools operate on."""
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme", size_band="mid")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        manual = Source(track="employment", kind="manual", name="pasted", enabled=True)
        s.add_all([org, src, manual])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of IT", org_name="Acme", url="https://x/1", stage="t2_done",
            first_seen_at=utcnow_iso(),
        )
        s.add(opp)
        await s.flush()
        pursuit = Pursuit(opportunity_id=opp.id, track="employment", status="shortlisted")
        s.add(pursuit)
        await s.flush()
        ids = {
            "opp": opp.id, "pursuit": pursuit.id, "source": src.id, "manual_source": manual.id,
        }
        await s.commit()
    await engine.dispose()
    await reset_engine()
    return ids


async def _read(fn):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            return await fn(s)
    finally:
        await engine.dispose()
        await reset_engine()


# --------------------------------------------------------------- ingest_opportunity (reference)


def test_ingest_opportunity_stores_locally_and_never_fetches_the_url(no_outbound):
    """The reference case, and the one most likely to be got wrong.

    A URL is handed over, not crawled. The tool must record it and stop; fetching the page here
    would be the system reaching out on the operator's behalf, and would also scrape without the
    politeness controls the real ingest path applies.
    """
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "ingest_opportunity",
                {"url": "https://boards.example.com/jobs/42", "title": "Head of IT",
                 "org_name": "Northwind", "track": "employment"},
            )).data

    result = asyncio.run(_run())
    assert "error" not in result
    assert result["track"] == "employment"

    async def _check(s):
        return (
            await s.execute(select(Opportunity).where(Opportunity.title == "Head of IT"))
        ).scalar_one_or_none()

    stored = asyncio.run(_read(_check))
    assert stored is not None, "the opportunity was not persisted"
    assert stored.url == "https://boards.example.com/jobs/42"


def test_ingest_opportunity_accepts_pasted_text_with_no_url(no_outbound):
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "ingest_opportunity",
                {"text": "Fractional CTO wanted, 2 days a week.", "title": "Fractional CTO",
                 "track": "consulting"},
            )).data

    result = asyncio.run(_run())
    assert "error" not in result and result["track"] == "consulting"


def test_ingest_opportunity_with_neither_url_nor_text_reports_an_error(no_outbound):
    # Surfaced as an error, not silently swallowed and not a crash.
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("ingest_opportunity", {})).data

    assert "error" in asyncio.run(_run())


# ------------------------------------------------------------------------------- trigger_scan


def test_trigger_scan_only_enqueues_and_does_not_scrape(no_outbound):
    """The tool must enqueue a task, never fetch. The worker does the fetching later, politely."""
    from fastmcp import Client

    async def _run():
        ids = await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("trigger_scan", {"source_name": "board"})).data, ids

    result, ids = asyncio.run(_run())
    assert result["enqueued"] is True and result["source_id"] == ids["source"]

    async def _check(s):
        return (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()

    tasks = asyncio.run(_read(_check))
    assert len(tasks) == 1, "expected exactly one queued scrape task"


def test_trigger_scan_on_an_unknown_source_reports_an_error(no_outbound):
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("trigger_scan", {"source_name": "nope"})).data

    assert "error" in asyncio.run(_run())


def test_trigger_scan_refuses_a_manual_source(no_outbound):
    # A manual source has no scrape adapter; enqueuing would churn a task that can never succeed.
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("trigger_scan", {"source_name": "pasted"})).data

    assert "error" in asyncio.run(_run())


# ------------------------------------------------------------------------- trigger_evaluation


def test_trigger_evaluation_requeues_locally(no_outbound):
    """Resets the stage so Tier 0 sweeps it again. No model call happens in the tool itself."""
    from fastmcp import Client

    async def _run():
        ids = await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "trigger_evaluation", {"opportunity_id": ids["opp"]}
            )).data, ids

    result, ids = asyncio.run(_run())
    assert result["requeued"] is True

    async def _check(s):
        return (await s.get(Opportunity, ids["opp"])).stage

    assert asyncio.run(_read(_check)) == "new"


def test_trigger_evaluation_on_a_missing_opportunity_reports_an_error(no_outbound):
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("trigger_evaluation", {"opportunity_id": 999999})).data

    assert "error" in asyncio.run(_run())


# ------------------------------------------------------------------------ set_pursuit_status


def test_set_pursuit_status_writes_a_status_event(no_outbound):
    """Status changes are auditable: the pursuit moves AND a status_event records the transition."""
    from fastmcp import Client

    async def _run():
        ids = await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "set_pursuit_status",
                {"pursuit_id": ids["pursuit"], "status": "applied", "note": "sent by hand"},
            )).data, ids

    result, ids = asyncio.run(_run())
    assert result["status"] == "applied"

    async def _check(s):
        p = await s.get(Pursuit, ids["pursuit"])
        events = (
            await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == ids["pursuit"]))
        ).scalars().all()
        return p.status, events

    status, events = asyncio.run(_read(_check))
    assert status == "applied"
    assert len(events) == 1
    assert (events[0].from_status, events[0].to_status) == ("shortlisted", "applied")
    # Marking a pursuit "applied" records what the OPERATOR did. It must never mean the system
    # submitted anything, which is why no_outbound is asserted on this tool above all others.


def test_set_pursuit_status_rejects_a_status_from_the_wrong_track(no_outbound):
    from fastmcp import Client

    async def _run():
        ids = await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "set_pursuit_status", {"pursuit_id": ids["pursuit"], "status": "proposal_sent"}
            )).data, ids

    result, ids = asyncio.run(_run())
    assert "error" in result

    async def _check(s):
        return (await s.get(Pursuit, ids["pursuit"])).status

    assert asyncio.run(_read(_check)) == "shortlisted", "a rejected status must not be applied"


# --------------------------------------------------------------------------------- add_note


def test_add_note_appends_to_the_pursuit(no_outbound):
    from fastmcp import Client

    async def _run():
        ids = await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool(
                "add_note", {"pursuit_id": ids["pursuit"], "note": "recruiter called"}
            )).data, ids

    result, ids = asyncio.run(_run())
    assert result["noted"] is True

    async def _check(s):
        return (await s.get(Pursuit, ids["pursuit"])).notes_md

    notes = asyncio.run(_read(_check))
    assert "recruiter called" in notes


def test_add_note_on_a_missing_pursuit_reports_an_error(no_outbound):
    from fastmcp import Client

    async def _run():
        await _seed()
        async with Client(build_mcp()) as c:
            return (await c.call_tool("add_note", {"pursuit_id": 999999, "note": "x"})).data

    assert "error" in asyncio.run(_run())


# ------------------------------------------------------------------- the guard guards something


# RFC 5737 TEST-NET-1: reserved for documentation and guaranteed not to be a real host, so if the
# guard ever regresses this suite still cannot reach anything on the actual internet.
_UNROUTABLE = "192.0.2.1"


def test_the_outbound_guard_catches_the_async_path(no_outbound):
    """The load-bearing case. Every network path in Kestrel is async httpx, and on Windows the
    Proactor loop's ConnectEx bypasses socket.socket.connect entirely. A guard that only patched
    the sync entry points let a real SYN out while reporting success, which made the "nothing left
    the machine" half of every test in this file vacuous. This pins the async path specifically."""

    async def _attempt():
        await asyncio.open_connection(_UNROUTABLE, 80)

    with pytest.raises(OutboundCallAttempted):
        asyncio.run(_attempt())


def _raised_outbound(exc: BaseException) -> bool:
    """True if OutboundCallAttempted is anywhere in this exception, including nested inside an
    ExceptionGroup. anyio (under httpx) collects failures into a BaseExceptionGroup, so a plain
    `pytest.raises(OutboundCallAttempted)` would miss a guard that fired correctly."""
    if isinstance(exc, OutboundCallAttempted):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_raised_outbound(sub) for sub in exc.exceptions)
    return exc.__cause__ is not None and _raised_outbound(exc.__cause__)


def test_the_outbound_guard_catches_httpx(no_outbound):
    """httpx is what the tools would actually use, so assert on it by name rather than by proxy."""
    import httpx

    async def _attempt():
        async with httpx.AsyncClient() as c:
            await c.get(f"http://{_UNROUTABLE}/")

    with pytest.raises(BaseException) as caught:  # noqa: PT011 - unwrapped below
        asyncio.run(_attempt())
    assert _raised_outbound(caught.value), f"guard did not fire; got {caught.value!r}"


def test_the_outbound_guard_catches_the_sync_paths(no_outbound):
    with pytest.raises(OutboundCallAttempted):
        socket.create_connection((_UNROUTABLE, 80), timeout=0.1)

    with pytest.raises(OutboundCallAttempted):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect((_UNROUTABLE, 80))
        finally:
            s.close()

    with pytest.raises(OutboundCallAttempted):
        socket.getaddrinfo("example.invalid", 80)
