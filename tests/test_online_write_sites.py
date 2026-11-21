"""D-TEST-791 (D-REL-41, D-REL-40 C7): only the confirmed consent form turns online models on.

A runtime recorder, not a text search: every POST route the crawler knows is posted with every
form that targets it (never a consent field: the crawler does not post one), in two worlds where
online sends are NOT allowed today:

- "online-off": the disclosure was accepted, then online models were switched off;
- "ack-stale": switched on, but under a disclosure older than the one shown now.

Before and after each request every setting row that decides online use (llm.hosted_mode.*,
setup.run_choice, llm.mode) is read straight from the database, so a write made through any
helper, raw SQL or a background thread is seen. Each changed row is recorded with the route that
changed it. A request may write those rows (a stored preference, an off switch), but no request
may leave online sends allowed when they were not before: that takes the ticked form.

The provider answers a model list and a key check from invented fixtures, so a route that would
connect really gets far enough to write. All rows and text are invented; nothing leaves the
machine.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from tests import _crawl_support as cs
from tests import test_contracts as _contracts
from tests import test_route_crawl as _crawl
from tests.test_route_crawl import APP, CASES, crawl_world, variants

# The crawler's fixtures, under the names the tests ask for.
world_template = _crawl.world_template
blocked_side_effects = _crawl.blocked_side_effects

TICKED = cs.scrape_forms(tick=True)
_RENDERED: dict[str, list[dict[str, str]]] = {}
_POST_CASES = [c for c in CASES if c.method != "GET"]


class _Forms(HTMLParser):
    """Every form as the page really rendered it (macros expanded, hx-vals filled): its fields,
    every checkbox a person can tick, each radio choice and each named submit button. A consent
    field is never carried (CONSENT_NAMES)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, list[dict[str, str]]]] = []
        self._form: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        url = a.get("hx-post") or (a.get("action") if tag == "form" else "")
        vals = {}
        if a.get("hx-vals", "").startswith("{"):
            try:
                vals = {k: str(v) for k, v in json.loads(a["hx-vals"]).items()}
            except ValueError:
                vals = {}
        if tag == "form":
            self._form = {"url": url, "base": dict(vals), "checks": {}, "radios": {}, "buttons": []}
            return
        f = self._form
        name = a.get("name", "")
        if url and (f is None or tag != "button"):
            self.found.append((url, [vals]))
        if f is None or not name or name in cs.CONSENT_NAMES:
            if f is not None and url and tag == "button":
                self.found.append((url, [{**f["base"], **vals}]))
            return
        kind = a.get("type", "text").lower()
        if tag == "input" and kind == "checkbox":
            f["checks"][name] = a.get("value") or "on"
        elif tag == "input" and kind == "radio":
            f["radios"].setdefault(name, []).append(a.get("value") or "on")
        elif tag == "button" or (tag == "input" and kind == "submit"):
            f["buttons"].append((name, a.get("value", "")))
        elif tag in ("input", "textarea", "select"):
            f["base"].setdefault(name, a.get("value") or ("5" if kind == "number" else "1"))

    def handle_endtag(self, tag):
        f = self._form
        if tag != "form" or f is None:
            return
        self._form = None
        if not f["url"]:
            return
        base = f["base"]
        ticked = {**base, **f["checks"]}
        sets = [base, ticked]
        for name, values in f["radios"].items():
            sets += [{**ticked, name: v} for v in values]
        sets += [{**ticked, name: v} for name, v in f["buttons"]]
        self.found.append((f["url"], sets))


def _case_for(url: str):
    path = url.split("?")[0]
    for c in _POST_CASES:
        if re.fullmatch(re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(c.path)), path):
            return c
    return None


def rendered_forms(world_template, monkeypatch) -> dict[str, list[dict[str, str]]]:
    """Route key -> the field sets of every form that posts there, read from every page view in
    the worlds where online is on and where it is off. Built once per run."""
    if _RENDERED:
        return _RENDERED
    for world in ("hosted", "online-off"):
        ids = crawl_world(world_template, world, monkeypatch)
        with TestClient(APP, raise_server_exceptions=False) as client:
            for case in CASES:
                if case.method != "GET":
                    continue
                r = client.get(cs.fill(case.path, ids), headers={"HX-Request": "true"})
                if not r.headers.get("content-type", "").startswith("text/html"):
                    continue
                parser = _Forms()
                parser.feed(r.text)
                for url, sets in parser.found:
                    target = _case_for(url)
                    if target is None:
                        continue
                    have = _RENDERED.setdefault(target.key, [])
                    for d in sets:
                        if d not in have:
                            have.append(d)
    return _RENDERED


egress_recorder = _contracts.recorder

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

WORLDS = ("online-off", "ack-stale")

_MODEL_LIST = {
    "data": [
        {
            "id": "vendor/invented-free:free",
            "name": "Invented free",
            "context_length": 64000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["structured_outputs", "response_format"],
        },
        {
            "id": cs.MODEL,
            "name": "Invented paid",
            "context_length": 128000,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "supported_parameters": ["structured_outputs", "response_format"],
        },
    ]
}
_KEY = {"data": {"label": "invented", "usage": 1.0, "limit": 10.0}}


def answer_the_provider(monkeypatch, rec) -> None:
    """Record every request, as the egress recorder does, and answer the provider's model list
    and key check; anything else still fails as a closed network."""

    def reply(request: httpx.Request) -> httpx.Response:
        rec.http.append(request)
        path = request.url.path.rstrip("/")
        if request.url.host == "openrouter.ai" and request.method == "GET":
            if path.endswith("/models"):
                return httpx.Response(200, json=_MODEL_LIST, request=request)
            if path.endswith("/key"):
                return httpx.Response(200, json=_KEY, request=request)
        raise httpx.ConnectError("test: the network is closed", request=request)

    async def ahandle(self, request):
        return reply(request)

    def handle(self, request):
        return reply(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)


def changed(before: dict, after: dict) -> dict[str, tuple[object, object]]:
    return {
        k: (before.get(k), after.get(k))
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k)
    }


def write_site_problems(client, rec, case, ids, world, rendered=()) -> list[str]:
    url = cs.fill(case.path, ids)
    out = []
    sets = list(variants(case))
    for d in [*TICKED.get(cs._norm(case.path), []), *rendered]:
        if d not in sets:
            sets.append(d)
    for data in sets:
        before = cs.online_rows()
        allowed_before = cs.online_consented(before)
        client.request(case.method, url, data=data, headers={"HX-Request": "true"})
        after = cs.online_rows()
        if not allowed_before and cs.online_consented(after):
            writes = changed(before, after)
            out.append(
                f"RULE: only the confirmed consent form turns online models on. ROUTE: "
                f"{case.method} {url} ({world}, form {sorted(data)}). WRITES: {writes}. FIX: "
                "write these rows only from the ticked disclosure form, or leave online as it was"
            )
    return out


def params():
    for case in CASES:
        if case.method == "GET":
            continue
        for world in WORLDS:
            yield pytest.param(case, world, id=f"{world}-{case.method}-{case.path}")


@pytest.mark.parametrize(("case", "world"), params())
def test_no_post_turns_online_on_without_the_confirmed_form(
    case, world, world_template, egress_recorder, monkeypatch, blocked_side_effects
):
    from kestrel.services import setup_state

    rendered = rendered_forms(world_template, monkeypatch).get(case.key, [])
    ids = crawl_world(world_template, world, monkeypatch)
    answer_the_provider(monkeypatch, egress_recorder)
    assert not cs.online_consented(cs.online_rows()), "the world must start with online not allowed"
    with TestClient(APP, raise_server_exceptions=False) as client:
        problems = write_site_problems(client, egress_recorder, case, ids, world, rendered)
    started = setup_state.running_action()
    if started is not None:
        started.join(40.0)
    assert not problems, "\n".join(problems)


def test_the_recorder_sees_a_write_that_turns_online_on(
    world_template, egress_recorder, monkeypatch, blocked_side_effects
):
    """The contract can fail: a route that flips the switch with no form is caught, whichever
    helper wrote the row."""
    from kestrel.web import settings_io as io
    from kestrel.web.csp import KestrelFastAPI

    ids = crawl_world(world_template, "online-off", monkeypatch)
    app = KestrelFastAPI()

    @app.post("/flip")
    async def flip():
        from kestrel.db.engine import get_sessionmaker

        async with get_sessionmaker()() as s:
            await io.write(s, "llm.hosted_mode.enabled", True)
            await s.commit()
        return {"ok": True}

    case = next(c for c in cs.enumerate_cases(app, {}) if c.path == "/flip")
    with TestClient(app, raise_server_exceptions=False) as client:
        problems = write_site_problems(client, egress_recorder, case, ids, "online-off")
    assert problems and "llm.hosted_mode.enabled" in problems[0], problems
