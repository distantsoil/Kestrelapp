"""The route driver decides "served" from the handler, never from the status (D-INST-30).

Every route the driver covers has its endpoint's code replaced by a stand-in, and the real app is
driven once per stand-in: a redirect (302, 307), a raised error, a returned error and a success.
Only the success stand-in may count as served. All values are invented."""

from __future__ import annotations

import inspect
import textwrap

import pytest

from tests import route_driver
from tests.test_install_id_contract import _fresh_db_with_planted_id

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# Each stand-in's body. It runs with the endpoint module's globals (only its code is swapped), so
# it names nothing global: imports are local and every value is a literal. The redirect target
# answers 200 without any route's endpoint running.
BODIES = {
    "redirect-302": ("from starlette.responses import RedirectResponse\n"
                     "return RedirectResponse('/static/icon.svg', status_code=302)", False),
    "redirect-307": ("from starlette.responses import RedirectResponse\n"
                     "return RedirectResponse('/static/icon.svg', status_code=307)", False),
    "raise-404": ("from fastapi import HTTPException\n"
                  "raise HTTPException(status_code=404, detail='invented')", False),
    "return-409": ("from starlette.responses import Response\n"
                   "return Response('invented', status_code=409)", False),
    "success": ("from starlette.responses import HTMLResponse\n"
                "return HTMLResponse('invented')", True),
}


def _standin(body: str, cells: int):
    """An async function with this body and `cells` free variables, so its code fits an endpoint
    that is a closure (setting __code__ needs the same number of free variables)."""
    names = [f"_cell{i}" for i in range(cells)]
    inner = "async def standin(*_a, **_kw):\n" + "".join(f"    {n}\n" for n in names)
    inner += textwrap.indent(body, "    ") + "\n"
    src = "def outer():\n" + "".join(f"    {n} = None\n" for n in names)
    src += textwrap.indent(inner, "    ") + "    return standin\n"
    ns: dict = {}
    exec(compile(src, f"<standin {cells}>", "exec"), ns)  # noqa: S102 - literal test source
    return ns["outer"]()


@pytest.mark.parametrize("case", list(BODIES))
def test_every_route_is_judged_by_its_handler(monkeypatch, case):
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    body, expected = BODIES[case]
    _fresh_db_with_planted_id()
    route_driver.seed()
    app = build_app(worker=False)
    for _m, _p, route in route_driver.routes(app):
        fn = route.endpoint
        cells = len(fn.__code__.co_freevars)
        # FastAPI reads an included route's signature when it first serves it: keep the real one.
        monkeypatch.setattr(fn, "__signature__", inspect.signature(fn), raising=False)
        monkeypatch.setattr(fn, "__code__", _standin(body, cells).__code__)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        driven = route_driver.drive(c, app)
    assert len(driven) > 400
    # A shadowed route's own endpoint never runs, so it is refused even when every endpoint
    # succeeds; one that is no longer shadowed fails here. A DECIDED_BY route is refused too:
    # its stand-in never asks the decision, so nothing allowed the work.
    skip = set(route_driver.SHADOWED) | set(route_driver.DECIDED_BY)
    wrong = [(d.method, d.path, d.response.status_code) for d in driven
             if d.served is not (expected and (d.method, d.path) not in skip)]
    assert wrong == [], f"{len(wrong)} routes judged wrongly: {wrong[:10]}"
    assert set(route_driver.SHADOWED) <= {(d.method, d.path) for d in driven}


class _AfterHandler:
    """A stand-in client whose request runs the route's endpoint (it returns a 200 of its own)
    and then answers `code`, as when encoding or a middleware fails after the handler returned."""

    def __init__(self, route, code: int) -> None:
        self.route, self.code = route, code

    def request(self, method, url, **kw):
        import asyncio

        asyncio.run(self.route.endpoint())
        return type("R", (), {"status_code": self.code})()


async def _ok():
    from starlette.responses import Response

    return Response(status_code=200)


@pytest.mark.parametrize(("code", "expected"), [(200, True), (500, False), (503, False)])
def test_a_crash_after_the_handler_returned_takes_served_away(monkeypatch, code, expected):
    """The status can only take served away, never grant it (D-INST-30)."""
    route = type("Route", (), {"endpoint": staticmethod(_ok), "status_code": None})()
    monkeypatch.setattr(route_driver, "routes", lambda app: [("GET", "/invented", route)])
    monkeypatch.setattr(route_driver, "request_parts", lambda r: ({}, {}, None))
    (d,) = route_driver.drive(_AfterHandler(route, code), app=None)
    assert d.served is expected


RETRY_ROUTES = sorted(route_driver.DECIDED_BY)


def _drive_only(monkeypatch, keep, prepare=None):
    """Seed, optionally change the seeded rows, then drive only the routes in `keep`."""
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    _fresh_db_with_planted_id()
    route_driver.seed()
    if prepare is not None:
        prepare()
    app = build_app(worker=False)
    real = route_driver.routes
    monkeypatch.setattr(route_driver, "routes",
                        lambda a: [e for e in real(a) if e[:2] in keep])
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        return route_driver.drive(c, app)


def _sql(statement: str) -> None:
    import sqlite3

    from kestrel import paths

    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute(statement)
        con.commit()
    finally:
        con.close()


def test_both_retry_routes_serve_on_their_own_rows(monkeypatch):
    """#206 with D-INST-32: each retry route runs on its own seeded row, so row_retry.plan allows
    it and the queue takes the task; both are served, with no refusal recorded."""
    driven = _drive_only(monkeypatch, set(RETRY_ROUTES))
    assert [(d.path, d.served, d.refusals) for d in driven] == [
        (p, True, ()) for _m, p in RETRY_ROUTES]
    assert route_driver.unexplained(driven) == []


@pytest.mark.parametrize("route", RETRY_ROUTES, ids=lambda r: r[1])
def test_a_retry_the_row_refuses_is_refused_with_the_plans_why(monkeypatch, route):
    """The decision, not the status, makes the verdict: a role that moved on (stage outside
    row_retry.RUNS_FROM) is refused by row_retry.plan, whether the page answers 200 or 409, and
    the refusal recorded is the plan's own MOVED_ON."""
    from kestrel.queue import row_retry

    (d,) = _drive_only(monkeypatch, {route},
                       lambda: _sql("UPDATE opportunity SET stage = 'applied'"))
    assert d.served is False
    assert ("row_retry.plan", row_retry.MOVED_ON) in d.refusals
    # Unlisted, it is unexplained; listed with the plan's why, it is explained; listed with
    # another why, it is not (the record decides, never the status alone).
    assert route_driver.unexplained([d]) != []
    status = d.response.status_code
    monkeypatch.setitem(route_driver.UNSERVED, route, (status, route_driver.STATE, "invented"))
    monkeypatch.setitem(route_driver.BECAUSE, route, ("row_retry.plan", lambda: row_retry.MOVED_ON))
    assert route_driver.unexplained([d]) == []
    monkeypatch.setitem(route_driver.BECAUSE, route, ("row_retry.plan", lambda: row_retry.KEPT))
    assert route_driver.unexplained([d]) != []


def test_a_retry_whose_task_is_already_queued_is_refused(monkeypatch):
    """Driving both retries on one row: the second finds the role already waiting, so
    enqueue_once refuses and the route is refused, even where its page answers 200."""
    keep = set(RETRY_ROUTES)
    monkeypatch.setitem(route_driver.ROUTE_VALUES, "/runs/quarantine/{quarantine_id}/retry",
                        {"quarantine_id": "1"})
    first, second = _drive_only(monkeypatch, keep)
    assert first.served is True
    assert second.served is False
    assert ("tasks.enqueue_once", route_driver.ALREADY_QUEUED) in second.refusals


def test_the_decision_tables_agree():
    """Every DECIDED_BY decision is watched, and a route in UNSERVED that a decision decides
    names the refusal it expects in BECAUSE."""
    assert set(route_driver.DECIDED_BY.values()) <= set(route_driver.DECISIONS)
    assert {k for k in route_driver.UNSERVED if k in route_driver.DECIDED_BY} <= set(
        route_driver.BECAUSE)
    assert set(route_driver.BECAUSE) <= set(route_driver.DECIDED_BY)
