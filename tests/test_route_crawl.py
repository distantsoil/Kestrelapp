"""D-TEST-8: the route crawler. Calls EVERY route of the app against one rich invented world, in
local mode and in hosted mode (an online route configured, a fake provider key, the egress
recorder armed), and holds each answer to the same rules:

- status below 500;
- the body has no traceback, no coroutine object, no "object has no attribute", no absolute path of
  the data folder and no exception class name in the visible text;
- a GET never calls a model and never makes a request that leaves this computer, in either mode,
  except the documented ones in PAGE_VIEW_EGRESS (tests/_crawl_support.py), key only;
- a hosted-mode POST never calls a model, because the crawler never posts a consent field
  (test_contracts.py owns the consent assertions; this does not repeat them).

A failure names the rule, the route and the fix. A route that must not be crawled goes in
ALLOWLIST in tests/_crawl_support.py with its reason; everything else is crawled automatically.

All rows and text are invented; the network and every model call are closed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from tests import _crawl_support as cs
from tests import test_contracts as _contracts
from tests import throwaway
from tests.test_contracts import _build_app

# test_contracts' fixture that records every model call and outbound request, under a new name
egress_recorder = _contracts.recorder

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

APP = _build_app()
CASES = cs.enumerate_cases(APP)
FORMS = cs.scrape_forms()


@pytest.fixture(scope="module")
def world_template(tmp_path_factory):
    """The invented world, built once into its own folder; each test gets a copy."""
    home = tmp_path_factory.mktemp("crawl_world")
    old = os.environ.get("KESTREL_DATA_HOME")
    os.environ["KESTREL_DATA_HOME"] = str(home)
    throwaway.mark(home)
    try:
        ids = asyncio.run(cs.build_world())
    finally:
        if old is None:
            os.environ.pop("KESTREL_DATA_HOME", None)
        else:
            os.environ["KESTREL_DATA_HOME"] = old
    return home, ids


@pytest.fixture
def blocked_side_effects(monkeypatch):
    """Nothing spawns a program, opens a window or makes a PDF during a crawl."""
    spawned: list[str] = []

    def refuse(*a, **kw):
        spawned.append(str(a[:1]))
        raise FileNotFoundError("test: no program can be started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    from kestrel.docs import render as docrender

    async def no_browser(*a, **kw):
        raise RuntimeError("test: no browser")

    monkeypatch.setattr(docrender, "render_pdf", no_browser)
    return spawned


def crawl_world(world_template, mode, monkeypatch):
    home, ids = world_template
    shutil.copytree(home, paths.data_home(), dirs_exist_ok=True)
    cs.repoint_files(home, paths.data_home())
    cs.apply_mode(mode)
    if mode == "hosted":
        monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    return ids


def variants(case: cs.Case) -> list[dict[str, str]]:
    if case.method == "GET":
        return [{}]
    return FORMS.get(cs._norm(case.path), [{}]) or [{}]


def explain(rule: str, where: str, fix: str) -> str:
    return f"RULE: {rule}. ROUTE: {where}. FIX: {fix}."


def check_case(client, rec, case: cs.Case, ids, mode: str) -> list[str]:
    """Call the route (once per form that posts to it) and return every broken rule."""
    out: list[str] = []
    url = cs.fill(case.path, ids)
    for data in variants(case):
        rec.llm.clear()
        rec.http.clear()
        headers = {"HX-Request": "true"}
        if case.method == "GET":
            r = client.get(url, headers=headers)
        else:
            r = client.request(case.method, url, data=data, headers=headers)
        where = f"{case.method} {url} ({mode}, form {sorted(data)})"
        for p in cs.body_problems(
            r, paths.data_home(), shows_path=case.key in cs.SHOWS_DATA_FOLDER
        ):
            out.append(explain("every route answers without a crash or leak", where, p))
        if rec.llm and (case.method == "GET" or mode == "hosted"):
            out.append(
                explain(
                    "a request that carries no consent never calls a model",
                    where,
                    "move the model call out of this handler into a worker task started by a "
                    "confirmed POST",
                )
            )
        if case.method == "GET" and cs.page_view_requests(rec):
            out.append(
                explain(
                    "a page view makes no request that leaves this computer",
                    where,
                    f"remove or defer the outbound request {cs.page_view_requests(rec)}, or add it "
                    "to "
                    "PAGE_VIEW_EGRESS with its docs/OUTBOUND-CALLS.md row",
                )
            )
    return out


def case_id(case: cs.Case) -> str:
    return f"{case.method}-{case.path}"


# Real product bugs the crawler found. Each is a strict xfail: when the product is fixed the test
# XPASSes, which fails the run, and the fix is to delete the entry here. Key: (route, mode).
BUGS: dict[tuple[str, str], str] = {}


def params():
    for case in CASES:
        for mode in ("local", "hosted"):
            reason = BUGS.get((case.key, mode))
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
            yield pytest.param(case, mode, id=f"{mode}-{case_id(case)}", marks=marks)


@pytest.mark.parametrize(("case", "mode"), params())
def test_every_route_answers_cleanly(
    case,
    mode,
    world_template,
    egress_recorder,
    monkeypatch,
    blocked_side_effects,
):
    ids = crawl_world(world_template, mode, monkeypatch)
    with TestClient(APP, raise_server_exceptions=False) as client:
        problems = check_case(client, egress_recorder, case, ids, mode)
    assert not problems, "\n".join(problems)


# ------------------------------------------------------------------------------ the crawl itself
def test_the_allowlist_has_no_stale_entries_and_every_entry_a_reason():
    stale = cs.stale_allowlist(APP)
    assert not stale, explain(
        "the crawler allowlist names only routes that exist",
        f"tests/_crawl_support.py ALLOWLIST {stale}",
        "delete the entry",
    )
    for key, reason in cs.ALLOWLIST.items():
        assert len(reason) > 20, explain(
            "an allowlisted route says why", key, "write the reason in a full sentence"
        )


def test_no_route_is_registered_twice():
    dup = cs.shadowed_routes(APP)
    assert not dup, explain(
        "a method and path has one handler",
        f"{dup}",
        "delete the handler that never runs, or merge the two",
    )


def test_every_known_bug_names_a_real_route():
    keys = {c.key for c in CASES}
    stale = sorted(k for k, _ in BUGS if k not in keys)
    assert not stale, explain("BUGS names only crawled routes", f"{stale}", "delete the entry")


def test_the_crawl_covers_every_route():
    total = len({(m, p) for p, r in cs._walk(APP.routes) for m in r.methods if m != "HEAD"})
    assert len(CASES) + len(cs.ALLOWLIST) == total, explain(
        "every route is crawled or allowlisted with a reason",
        f"{len(CASES)} crawled + {len(cs.ALLOWLIST)} allowlisted != {total}",
        "list the missing route in ALLOWLIST with its reason",
    )
    assert len(CASES) > 400


# ------------------------------------------------------------- proof the guard can fail
def _broken_app(extra_routes):
    from fastapi import FastAPI

    app = FastAPI()
    for path, fn in extra_routes:
        app.get(path)(fn)
    return app


def _run_on(app, path, rec, monkeypatch, tmp_path):
    case = next(c for c in cs.enumerate_cases(app, {}) if c.path == path)
    with TestClient(app, raise_server_exceptions=False) as client:
        return check_case(client, rec, case, {}, "local")


def test_crawler_flags_a_route_that_500s(egress_recorder, monkeypatch, tmp_path):
    async def boom():
        raise RuntimeError("invented failure")

    out = _run_on(_broken_app([("/boom", boom)]), "/boom", egress_recorder, monkeypatch, tmp_path)
    assert any("status 500" in p and "RULE:" in p and "FIX:" in p for p in out), out


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("Traceback (most recent call last)", "Traceback"),
        ("<coroutine object f at 0x1>", "coroutine"),
        ("'X' object has no attribute 'y'", "object has no attribute"),
        ("<p>Saved to {home}/x.pdf</p>", "data folder path"),
        ("<p>ValueError: bad</p>", "exception classes"),
    ],
)
def test_crawler_flags_a_leaking_body(text, needle, egress_recorder, monkeypatch, tmp_path):
    from fastapi.responses import HTMLResponse

    home = paths.data_home()

    async def leak():
        return HTMLResponse(text.replace("{home}", str(home)))

    out = _run_on(_broken_app([("/leak", leak)]), "/leak", egress_recorder, monkeypatch, tmp_path)
    assert any(needle in p for p in out), out


def test_crawler_allows_an_exception_name_under_show_details(
    egress_recorder, monkeypatch, tmp_path
):
    from fastapi.responses import HTMLResponse

    async def detail():
        return HTMLResponse("<p>The window could not open.</p><details><summary>Show details"
                            "</summary><p>ModuleNotFoundError: gi</p></details>")

    out = _run_on(_broken_app([("/detail", detail)]), "/detail", egress_recorder, monkeypatch,
                  tmp_path)
    assert not any("exception classes" in p for p in out), out


def test_crawler_flags_a_get_that_calls_a_model_or_the_network(
    egress_recorder, monkeypatch, tmp_path
):
    import httpx

    from kestrel.llm.client import LLMClient

    async def sneaky():
        try:
            await LLMClient.complete(None)
        except RuntimeError:
            pass
        async with httpx.AsyncClient() as c:
            try:
                await c.post("https://api.example.invalid/v1/chat", content=b"data")
            except httpx.HTTPError:
                pass
        return {"ok": True}

    out = _run_on(
        _broken_app([("/sneaky", sneaky)]), "/sneaky", egress_recorder, monkeypatch, tmp_path
    )
    assert any("calls a model" in p for p in out), out
    assert any("leaves this computer" in p for p in out), out


def test_crawler_allows_only_the_documented_balance_call_and_only_with_the_key(
    egress_recorder, monkeypatch, tmp_path
):
    import httpx

    async def balance():
        async with httpx.AsyncClient() as c:
            try:
                await c.get("https://openrouter.ai/api/v1/key", headers={"Authorization": "x"})
            except httpx.HTTPError:
                pass
        return {"ok": True}

    async def balance_with_text():
        async with httpx.AsyncClient() as c:
            try:
                await c.request(
                    "GET", "https://openrouter.ai/api/v1/key", content=b"invented resume text"
                )
            except httpx.HTTPError:
                pass
        return {"ok": True}

    async def other_host():
        async with httpx.AsyncClient() as c:
            try:
                await c.get("https://example.invalid/api/v1/key")
            except httpx.HTTPError:
                pass
        return {"ok": True}

    app = _broken_app([("/bal", balance), ("/bal2", balance_with_text), ("/oth", other_host)])
    ok = _run_on(app, "/bal", egress_recorder, monkeypatch, tmp_path)
    assert not any("leaves this computer" in p for p in ok), ok
    text = _run_on(app, "/bal2", egress_recorder, monkeypatch, tmp_path)
    assert any("carries a body" in p for p in text), text
    other = _run_on(app, "/oth", egress_recorder, monkeypatch, tmp_path)
    assert any("leaves this computer" in p for p in other), other


def test_a_clean_route_passes_the_crawler(egress_recorder, monkeypatch, tmp_path):
    async def fine():
        return {"ok": True}

    assert (
        _run_on(_broken_app([("/fine", fine)]), "/fine", egress_recorder, monkeypatch, tmp_path)
        == []
    )


def test_the_data_folder_rule_ignores_show_details_and_flags_the_rest(tmp_path):
    class R:
        status_code = 200
        headers = {"content-type": "text/html"}

        def __init__(self, text):
            self.text = text

    home = tmp_path / "kestrel_home"
    inside = f"<details><summary>Show details</summary><code>run {home}/x</code></details>"
    assert cs.body_problems(R(inside), home) == []
    assert any("data folder" in p for p in cs.body_problems(R(f"<p>{home}/x</p>"), home))
    assert cs.body_problems(R(f"<p>{home}/x</p>"), home, shows_path=True) == []


def test_the_form_scraper_finds_forms_and_drops_consent(tmp_path):
    (tmp_path / "a.html").write_text(
        '<form hx-post="/x/{{ o.id }}/save"><input name="title" value="t">'
        '<input type="checkbox" name="confirmed"><input type="hidden" name="confirmed" value="1">'
        '<textarea name="body"></textarea><select name="k"><option value="v1"></option></select>'
        "</form>",
        encoding="utf-8",
    )
    got = cs.scrape_forms(tmp_path)
    assert got == {"/x/\x00/save": [{"title": "t", "body": "Invented text", "k": "v1"}]}
