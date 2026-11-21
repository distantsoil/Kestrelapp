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
from kestrel.web.csp import POLICY
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
    # Both reads cache in-process; clear them so what a route sends does not depend on which
    # route ran before it (the exact egress allowlist is per route, D-TEST-790).
    from kestrel.services import hosted_balance, hosted_models

    hosted_balance.clear_cache()
    hosted_models.clear_cache()
    if mode != "local":
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
        if (r.headers.get("content-type", "").startswith(cs.HTML_TYPES)
                and case.key not in cs.CSP_MARKUP_ALLOWLIST):
            for problem in sorted(set(cs.inline_markup_problems(r.text))):
                out.append(explain("no answer carries markup the Content-Security-Policy refuses",
                                   where, problem))
        got = r.headers.get_list("content-security-policy")
        if got != [POLICY]:
            out.append(
                explain(
                    "every response carries exactly the app's Content-Security-Policy",
                    where,
                    f"the response carried {got!r}; answer through build_app's stack "
                    "(KestrelFastAPI in kestrel/web/csp.py) and never set the header yourself",
                )
            )
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
        sent = cs.page_view_requests(rec, case.key, mode) if case.method == "GET" else []
        if sent:
            out.append(
                explain(
                    "a page view makes no request that leaves this computer, except the exact "
                    "reads listed for that route while online is in use",
                    where,
                    f"remove or defer the outbound request {sent}, or list it for this route in "
                    "PAGE_VIEW_EGRESS with its reason",
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
        for mode in ("local", "hosted", "hosted-local"):
            if mode == "hosted-local" and case.method != "GET":
                continue  # online on but Local chosen: only the page-view egress rule differs
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
    from kestrel.services import setup_state

    ids = crawl_world(world_template, mode, monkeypatch)
    with TestClient(APP, raise_server_exceptions=False) as client:
        problems = check_case(client, egress_recorder, case, ids, mode)
    # A route that starts a Setup action (POST /models/health/run starts a doctor run) answers
    # at once while the action works on; the crawl waits for it, so it never runs into the next
    # test's data home (D-TEST-583, D-TEST-581).
    started = setup_state.running_action()
    if started is not None:
        started.join(40.0)
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
def _broken_app(extra_routes, app_class=None):
    from kestrel.web.csp import KestrelFastAPI

    app = (app_class or KestrelFastAPI)()
    for path, fn in extra_routes:
        app.get(path)(fn)
    return app


def _run_on(app, path, rec, monkeypatch, tmp_path, mode="local"):
    case = next(c for c in cs.enumerate_cases(app, {}) if c.path == path)
    with TestClient(app, raise_server_exceptions=False) as client:
        return check_case(client, rec, case, {}, mode)


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


async def _get_quietly(url: str, **kw) -> dict:
    import httpx

    async with httpx.AsyncClient() as c:
        try:
            await c.request("GET", url, **kw)
        except httpx.HTTPError:
            pass
    return {"ok": True}


def test_crawler_allows_only_the_listed_reads_for_their_own_route_and_only_with_the_key(
    egress_recorder, monkeypatch, tmp_path
):
    """D-TEST-790: the page-view egress list is exact, keyed by (route, method, host, path)."""
    key = "https://openrouter.ai/api/v1/key"
    models = "https://openrouter.ai/api/v1/models"

    async def balance():
        return await _get_quietly(key, headers={"Authorization": "x"})

    async def balance_with_text():
        return await _get_quietly(key, content=b"invented resume text")

    async def other_host():
        return await _get_quietly("https://example.invalid/api/v1/key")

    async def model_list():
        return await _get_quietly(models)

    async def other_models():
        return await _get_quietly("https://example.invalid/v1/models")

    monkeypatch.setitem(cs.PAGE_VIEW_EGRESS, ("GET /bal", *cs._BALANCE), "invented for the test")
    monkeypatch.setitem(cs.PAGE_VIEW_EGRESS, ("GET /bal2", *cs._BALANCE), "invented for the test")
    monkeypatch.setitem(cs.PAGE_VIEW_EGRESS, ("GET /oth", *cs._BALANCE), "invented for the test")
    monkeypatch.setitem(cs.PAGE_VIEW_EGRESS, ("GET /mods", *cs._MODELS), "invented for the test")
    app = _broken_app([("/bal", balance), ("/bal2", balance_with_text), ("/oth", other_host),
                       ("/mods", model_list), ("/unlisted", model_list),
                       ("/othmods", other_models)])

    def sent(path, mode="hosted"):
        out = _run_on(app, path, egress_recorder, monkeypatch, tmp_path, mode)
        return [p for p in out if "leaves this computer" in p]

    assert not sent("/bal")
    assert not sent("/mods")
    assert any("carries a body" in p for p in sent("/bal2"))
    assert sent("/oth")  # a listed route, but another host
    # The old rule let any bodiless GET ending /models through from any route and any host.
    assert sent("/unlisted")
    assert sent("/othmods")
    # A listed read is still refused where online is not in use: Local chosen, or no online.
    for mode in ("hosted-local", "local"):
        assert sent("/bal", mode) and sent("/mods", mode)


def test_the_page_view_egress_list_names_real_routes_and_only_the_providers_fixed_host():
    """Every listed route exists and is crawled, and every read goes to OpenRouter's own host and
    a fixed path: the only provider the crawl world connects (D-TEST-790)."""
    keys = {c.key for c in CASES}
    for (route, method, host, path), why in cs.PAGE_VIEW_EGRESS.items():
        assert route in keys, explain("the egress list names only crawled routes", route,
                                      "delete the entry")
        assert (method, host, path) in (cs._BALANCE, cs._MODELS), (route, method, host, path)
        assert route.startswith("GET ") and len(why) > 20


@pytest.mark.parametrize("sent", [None, "default-src 'self'"])
def test_crawler_flags_a_missing_or_different_policy(sent, egress_recorder, monkeypatch,
                                                     tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    async def bare():
        return HTMLResponse("<p>Invented</p>",
                            headers={"Content-Security-Policy": sent} if sent else None)

    out = _run_on(_broken_app([("/bare", bare)], app_class=FastAPI), "/bare", egress_recorder,
                  monkeypatch, tmp_path)
    assert any("Content-Security-Policy" in p for p in out), out


@pytest.mark.parametrize(
    ("markup", "needle"),
    [
        ('<p style="color: red">x</p>', "style attribute"),
        ('<button onclick="go()">x</button>', "inline handler"),
        ('<svg><rect onload="go()"/></svg>', "inline handler"),
        ('<form hx-post="/x" hx-on::after-request="go()"></form>', "hx-on"),
        ("<div hx-get=\"/x\" hx-vals='js:{a: 1}'></div>", "htmx evaluates it"),
        ("<div hx-get=\"/x\" hx-vals='javascript:{a: 1}'></div>", "htmx evaluates it"),
        ("<div hx-get=\"/x\" hx-headers='js:{a: 1}'></div>", "htmx evaluates it"),
        ("<div hx-get=\"/x\" hx-headers='javascript:{a: 1}'></div>", "htmx evaluates it"),
        ("<div hx-get=\"/x\" data-hx-headers=' js:{a: 1}'></div>", "htmx evaluates it"),
        ("<div hx-get=\"/x\" hx-vars='a: 1'></div>", "htmx evaluates it"),
        ('<a href="java&#9;script:go()">x</a>', "javascript:"),
        ('<a href="&#1;javascript:go()">x</a>', "javascript:"),
        ('<a href="java\nscript:go()">x</a>', "javascript:"),
        ('<a href="vbscript:go()">x</a>', "vbscript:"),
        ('<meta http-equiv="refresh" content="0;url=javascript:go()">', "refresh"),
        ("<meta http-equiv=\"Refresh\" content=\"0; URL='javascript:go()'\">", "refresh"),
        ('<svg><a><set attributeName="href" to="javascript:go()"/></a></svg>', "javascript:"),
        ('<svg><animate attributeName="href" values="/a;javascript:go()"/></svg>', "javascript:"),
        ('<object data="javascript:go()"></object>', "javascript:"),
        ('<div hx-get="javascript:go()"></div>', "javascript:"),
        ('<button data-hx-post="javascript:go()">x</button>', "javascript:"),
        ('<iframe src="data:text/html,x"></iframe>', "data:"),
        ('<a href="data:text/html,x">x</a>', "data:"),
        ('<div hx-get="/x" hx-trigger="every 2s [ok()]"></div>', "trigger filter"),
        ('<a href=" JavaScript:go()">x</a>', "javascript:"),
        ('<form action="javascript:go()"></form>', "javascript:"),
        ('<button formaction="javascript:go()">x</button>', "javascript:"),
        ("<script>go()</script>", "inline script"),
        ('<script type="module">go()</script>', "inline script"),
        ("<style>p { color: red }</style>", "inline stylesheet"),
        ('<span x-html="html"></span>', "x-html"),
        ('<iframe srcdoc="&lt;style&gt;p{}&lt;/style&gt;"></iframe>', "inside a srcdoc frame"),
    ],
)
def test_crawler_flags_markup_the_policy_refuses(markup, needle, egress_recorder, monkeypatch,
                                                 tmp_path):
    from fastapi.responses import HTMLResponse

    async def inline():
        return HTMLResponse(f"<div>{markup}</div>")

    out = _run_on(_broken_app([("/inline", inline)]), "/inline", egress_recorder, monkeypatch,
                  tmp_path)
    assert any(needle in p for p in out), out


def test_crawler_parses_an_xhtml_answer_too(egress_recorder, monkeypatch, tmp_path):
    from fastapi.responses import Response

    async def xhtml():
        return Response('<html xmlns="http://www.w3.org/1999/xhtml"><p style="color: red">x</p>'
                        "</html>", media_type="application/xhtml+xml")

    out = _run_on(_broken_app([("/xhtml", xhtml)]), "/xhtml", egress_recorder, monkeypatch,
                  tmp_path)
    assert any("style attribute" in p for p in out), out


def test_the_markup_allowlist_names_crawled_routes_with_reasons():
    stale = sorted(set(cs.CSP_MARKUP_ALLOWLIST) - {c.key for c in cs.enumerate_cases(APP, {})})
    assert not stale, explain("CSP_MARKUP_ALLOWLIST names only crawled routes", f"{stale}",
                              "delete the entry")
    for key, reason in cs.CSP_MARKUP_ALLOWLIST.items():
        assert len(reason) > 20, explain("every markup allowlist entry has a reason", key,
                                         "say why the route may carry that markup")


def test_crawler_allows_data_blocks_and_files():
    ok = ('<img src="data:image/png;base64,iVBORw0KGgo=" alt="">'
          '<a href="https://kestrelapp.co/">Invented</a><a href="mailto:a@example.invalid">m</a>'
          '<a href="#top">t</a><a href="?track=consulting">q</a>'
          '<meta http-equiv="refresh" content="5;url=/runs">'
          '<script type="application/json" id="d">{"a": "<b>"}</script>'
          '<script src="/static/js/k-head.js"></script>'
          '<link rel="stylesheet" href="/frame-css/' + "0" * 64 + '.css">'
          '<div data-k-style="width: 4%" hx-trigger="every 2s" data-k-when="poll=no-modal"></div>'
          '<a href="/opportunities">Invented</a>')
    assert cs.inline_markup_problems(ok) == []


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
