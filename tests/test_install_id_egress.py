"""The install ID never leaves the computer (D-INST-2), driven from the route table (D-INST-14).

Round 5 replaces the hand-picked, GET-only page drivers: every route the app serves, in every
method it declares, is requested with fixture values for its path parameters and a body built
from its declared parameters; every MCP tool is called with arguments built from its schema; and
every subprocess, browser open and network connection is recorded. A route, a path parameter, a
method or a tool argument type with no driver fails the test, so a new route is driven the day it
is written. Exemptions are listed with a reason, and an exemption for a route that no longer
exists fails too.

All values are invented. Nothing reaches the network or starts a process: the recorder answers
locally and refuses the rest."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from kestrel.services import install_id
from tests import route_driver
from tests.test_install_id_contract import (  # noqa: F401 - recorder is a fixture
    PLANTED,
    _forms,
    _fresh_db_with_planted_id,
    recorder,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

class Spawns:
    """Every process start and browser open: recorded, then refused."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, *parts) -> None:
        self.calls.append(json.dumps([str(p) for p in parts]))

    def leaks(self, full: str) -> list[str]:
        return [c for c in self.calls for f in _forms(full) if f in c]


@pytest.fixture
def spawns(monkeypatch) -> Spawns:
    import os
    import subprocess
    import webbrowser

    rec = Spawns()

    def popen(self, args, *a, **kw):
        env = kw.get("env") or {}
        rec.record("popen", args, kw.get("cwd"), *(f"{k}={v}" for k, v in env.items()))
        raise OSError("test: no processes")

    def run(*a, **kw):
        rec.record("run-input", kw.get("input"))
        return real_run(*a, **kw)

    async def exec_(*args, **kw):
        env = kw.get("env") or {}
        rec.record("exec", *args, *(f"{k}={v}" for k, v in env.items()))
        raise OSError("test: no processes")

    async def shell(cmd, **kw):
        rec.record("shell", cmd)
        raise OSError("test: no processes")

    def system(cmd):
        rec.record("system", cmd)
        return 1

    def browser(url, *a, **kw):
        rec.record("browser", url)
        return False

    real_run = subprocess.run
    monkeypatch.setattr(subprocess.Popen, "__init__", popen)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", shell)
    monkeypatch.setattr(os, "system", system)
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, browser)
    return rec


def test_every_route_has_a_driver():
    """The route table decides what is driven: an undeclared path parameter, a method with no
    driver or a stale exemption fails here, before anything runs."""
    from kestrel.web.app import build_app

    routes = route_driver.routes(build_app(worker=False))
    missing = sorted({p for _m, path, _r in routes for p in re.findall(r"{([^}:]+)", path)
                      if p not in route_driver.PATH_VALUES})
    assert missing == [], f"path parameters with no fixture value: {missing}"
    methods = sorted({m for m, _p, _r in routes if m not in route_driver.METHODS})
    assert methods == [], f"methods with no driver: {methods}"
    live = {(m, p) for m, p, _r in routes}
    assert set(route_driver.EXEMPT) <= live, sorted(set(route_driver.EXEMPT) - live)
    assert len(routes) > 400  # the table really was read


# Sweeps every route and MCP tool (O(routes)): 46.5 s alone on the owner's Windows PC against
# pyproject's 60 s default, which on Windows stops the xdist worker; same sizing as the contract
# sweep it shares a recorder with (D-TEST-797).
@pytest.mark.timeout(240)
def test_no_route_sends_or_spawns_with_the_install_id(recorder, spawns):  # noqa: F811
    """Every route, every method, from this computer and with the ID shown on About: no request
    Kestrel makes, no process it starts and no browser it opens carries any form of the ID."""
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    _fresh_db_with_planted_id()
    route_driver.seed()
    app = build_app(worker=False)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        about = c.get("/about").text
        driven = route_driver.drive(c, app)
    routes = [x for x in route_driver.routes(app) if x[:2] not in route_driver.EXEMPT]
    assert install_id.short_form(PLANTED) in about  # the ID really was on a page
    assert len(driven) == len(routes)
    # Not vacuous: every route's own handler did its work (D-INST-30), or the route is in
    # route_driver.UNSERVED with its reason and still answers that status (D-INST-18).
    assert (left := route_driver.unexplained(driven)) == [], left
    assert (stale := route_driver.stale_unserved(driven)) == [], stale
    assert recorder.leaks(PLANTED) == []
    assert spawns.leaks(PLANTED) == []
    assert recorder.sockets == [] and recorder.lookups == [], (recorder.sockets, recorder.lookups)


def _tool_args(schema: dict) -> dict:
    out = {}
    for name, prop in (schema.get("properties") or {}).items():
        kind = prop.get("type")
        if kind is None and prop.get("anyOf"):
            kind = next((o.get("type") for o in prop["anyOf"] if o.get("type") != "null"), None)
        samples = {"integer": 1, "number": 1.0, "string": "invented", "boolean": False,
                   "array": [], "object": {}}
        assert kind in samples, f"tool argument {name!r} has no driver for type {kind!r}"
        out[name] = samples[kind]
    return out


def _call_every_tool(mcp) -> tuple[list[str], list[str]]:
    """Call every tool the server lists, with arguments from its schema; returns the tool names
    and what each returned (its text content, or the refusal it raised)."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    returned: list[str] = []

    async def go():
        async with Client(mcp) as c:
            tools = await c.list_tools()
            for tool in tools:
                try:
                    result = await c.call_tool(tool.name, _tool_args(tool.inputSchema))
                    returned.append(json.dumps([getattr(b, "text", str(b))
                                                for b in result.content]))
                except ToolError as exc:
                    returned.append(str(exc))  # refused input still ran the tool's own code
            return [t.name for t in tools]

    return asyncio.run(go()), returned


def _carrying(returned: list[str]) -> list[str]:
    return [r for r in returned if any(f in r for f in _forms(PLANTED))]


def test_no_mcp_tool_sends_spawns_or_returns_the_install_id(recorder, spawns):  # noqa: F811
    from kestrel.mcp.server import build_mcp

    _fresh_db_with_planted_id()
    route_driver.seed()
    names, returned = _call_every_tool(build_mcp())
    assert names and len(returned) == len(names)
    # Round 7: what each tool returns (to the agent on the other end) carries no form of the ID.
    assert _carrying(returned) == []
    assert recorder.leaks(PLANTED) == []
    assert spawns.leaks(PLANTED) == []
    assert recorder.sockets == [] and recorder.lookups == [], (recorder.sockets, recorder.lookups)


def test_the_tool_result_scan_would_see_a_leak():
    """The result scan can fail: a tool that returns the short or bare ID is reported."""
    from fastmcp import FastMCP

    mcp = FastMCP("invented")

    @mcp.tool
    def short() -> str:
        return install_id.short_form(PLANTED)

    @mcp.tool
    def bare() -> dict:
        return {"x": PLANTED.replace("-", "")}

    names, returned = _call_every_tool(mcp)
    assert sorted(names) == ["bare", "short"] and len(_carrying(returned)) == 2


def test_the_spawn_recorder_would_see_a_leak(spawns):
    """Each kind of process start and browser open is recorded with what it carried."""
    import os
    import subprocess
    import webbrowser

    with pytest.raises(OSError):
        subprocess.Popen(["invented-tool", PLANTED.upper()])
    with pytest.raises(OSError):
        subprocess.run(["invented-tool"], input=PLANTED, check=False)
    with pytest.raises(OSError):
        asyncio.run(asyncio.create_subprocess_exec("x", env={"K": PLANTED.replace("-", "")}))
    with pytest.raises(OSError):
        asyncio.run(asyncio.create_subprocess_shell(f"echo {PLANTED}"))
    assert os.system(f"echo {PLANTED}") == 1  # noqa: S605 - test: the recorder refuses it
    webbrowser.open(f"https://example.invalid/?k={install_id.short_form(PLANTED)}")
    kinds = {json.loads(c)[0] for c in spawns.leaks(PLANTED)}
    assert kinds == {"popen", "run-input", "exec", "shell", "system", "browser"}, kinds


@pytest.mark.parametrize(("prop", "value"), [
    ({"type": "integer"}, 1), ({"type": "string"}, "invented"), ({"type": "boolean"}, False),
    ({"type": "array"}, []), ({"type": "object"}, {}), ({"type": "number"}, 1.0),
    ({"anyOf": [{"type": "null"}, {"type": "integer"}]}, 1),
])
def test_tool_arguments_are_built_for_every_schema_type(prop, value):
    assert _tool_args({"properties": {"a": prop}}) == {"a": value}


def test_a_tool_argument_with_no_driver_fails():
    """The hatch: a schema type the driver does not know stops the test, never a skip."""
    with pytest.raises(AssertionError, match="no driver"):
        _tool_args({"properties": {"a": {"type": "invented"}}})


def _d(method: str, path: str, code: int, served: bool) -> route_driver.Driven:
    return route_driver.Driven(method, path, type("R", (), {"status_code": code})(), served)


def test_the_unserved_allowlist_can_fail():
    """The allowlist is a hatch (AGENT-RULES 8a.6): a refused route missing from it, an entry
    whose handler now serves and an entry for a route that is gone are each reported."""
    listed = next(iter(route_driver.UNSERVED))
    status = route_driver.UNSERVED[listed][0]
    assert route_driver.unexplained([_d("GET", "/not-listed", 404, False)]) == [
        ("GET", "/not-listed", 404)]
    assert route_driver.unexplained([_d(*listed, status, False)]) == []
    # Listed, but refused with another status: unexplained.
    assert route_driver.unexplained([_d(*listed, status + 1, False)]) != []
    assert (*listed, None) in route_driver.stale_unserved([_d(*listed, 200, True)])
    assert listed not in route_driver.stale_unserved([_d(*listed, status, False)])
    assert set(route_driver.stale_unserved([])) == set(route_driver.UNSERVED) | set(
        route_driver.FLAKY)
    # D-INST-30: the verdict is the handler's record, never the status: a 200 with no record is
    # refused, and unexplained reads only the record (drive applies the 5xx veto itself).
    assert route_driver.unexplained([_d("GET", "/not-listed", 200, False)]) != []
    assert route_driver.unexplained([_d("GET", "/not-listed", 303, True)]) == []


def test_a_handler_result_is_served_only_on_an_allowlisted_status():
    """What a handler returns decides served (D-INST-30): a Response with a 2xx or 303 of its own,
    or a plain value under the route's declared status. 302, 307 and every other code are not."""
    from starlette.responses import Response

    for code in (200, 201, 204, 299, 303):
        assert route_driver.handler_served(Response(status_code=code)), code
    for code in (100, 199, 300, 301, 302, 304, 307, 308, 400, 404, 409, 422, 500, 503):
        assert not route_driver.handler_served(Response(status_code=code)), code
    assert route_driver.handler_served({"ok": True})
    assert route_driver.handler_served({"ok": True}, 201)
    assert not route_driver.handler_served({"ok": True}, 302)


class _Answers:
    """A stand-in client: each request runs the route's endpoint, which returns the next status
    from a list as its own Response (so the recorder sees the handler's result)."""

    def __init__(self, route, codes: list[int]) -> None:
        self.route, self.codes, self.calls = route, list(codes), 0

    def request(self, method, url, **kw):
        from starlette.responses import Response

        assert kw.get("follow_redirects") is False
        self.calls += 1
        code = self.codes.pop(0) if self.codes else 500
        asyncio.run(self.route.endpoint(Response(status_code=code)))
        return type("R", (), {"status_code": code})()


async def _echo(resp):
    return resp


@pytest.mark.parametrize(("codes", "calls", "final"), [
    ([500, 200], 2, 200), ([200], 1, 200), ([500, 500, 500, 200], 3, 500),
    ([302, 307, 302], 3, 302)],
    ids=["served-on-retry", "served-first", "500-every-try", "redirect-every-try"])
def test_a_flaky_route_must_serve_within_its_tries(monkeypatch, codes, calls, final):
    """Round 8: a FLAKY route is retried up to FLAKY_TRIES times and must be served once; one
    refused on every try ends refused and is unexplained. D-INST-30: served is the handler's
    own result, so a redirect on every try is refused."""
    flaky = next(iter(route_driver.FLAKY))
    route = type("Route", (), {"endpoint": staticmethod(_echo), "status_code": None})()
    monkeypatch.setattr(route_driver, "routes", lambda app: [(*flaky, route)])
    monkeypatch.setattr(route_driver, "request_parts", lambda r: ({}, {}, None))
    client = _Answers(route, codes)
    (d,) = route_driver.drive(client, app=None)
    assert client.calls == calls and d.response.status_code == final
    assert d.served is (final == 200)
    assert (route_driver.unexplained([d]) == []) is (final == 200)
