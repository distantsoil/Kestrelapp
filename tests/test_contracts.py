"""D-TEST-7 B: contract tests that enumerate, not sample.

Two contracts, each found by scanning the repo, so a new form or route is covered the day it is
added and a forgotten one fails here instead of in review:

1. Consent. Every template that holds an online send (an ask-first panel) keeps the consent in
   the person's hands: no hidden `confirmed` field pre-answers it, the box that gates the send
   posts as `confirmed`, and every POST endpoint those forms (and the handlers that call the
   consent check) reach refuses an unconfirmed post while an online route is configured: no
   model call, no network request, no queued task.
2. Files. Every route that serves or deletes a file named by a path stored in the database
   refuses a path outside Kestrel's documents folder.

Invariants (stated per CLAUDE.md "state the invariant in every risky brief"):
- Nothing is sent to an online model without the confirmed ask-first step, including retries.
- A path read back from the database is never served or deleted unless it is inside the
  documents (or prep packs) folder.

All text and rows here are invented. Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kestrel import paths
from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import (
    DocRender,
    DocSlot,
    Letter,
    Offer,
    PrepPack,
    Pursuit,
    Quarantine,
    Setting,
    Source,
    Task,
)
from kestrel.llm.client import LLMClient
from kestrel.web.pages.offers import PREP_SITUATION
from tests import role_seed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "kestrel" / "web" / "templates"

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


# ------------------------------------------------------------------------------ route table
def _walk(routes, prefix: str = ""):
    """Every concrete route of the app, through included routers: (full path, route)."""
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            yield from _walk(r.original_router.routes, prefix + r.include_context.prefix)
        elif hasattr(r, "endpoint"):
            yield prefix + r.path, r


def _build_app():
    from kestrel.web.app import build_app

    return build_app(worker=False)


@pytest.fixture(scope="module")
def route_table() -> list[tuple[str, object]]:
    return list(_walk(_build_app().routes))


def _norm(path: str) -> str:
    """A route or template URL with every parameter replaced by one marker."""
    path = re.sub(r"\{\{.*?\}\}", "\x00", path.split("?")[0])
    return re.sub(r"\{[^}]+\}", "\x00", path)


def _reach(fn, depth: int = 3, seen: set | None = None) -> str:
    """Source of a handler plus the module-level functions it calls, a few levels down, so a
    delete or a consent check done by a helper is still seen."""
    seen = seen if seen is not None else set()
    if fn in seen or depth < 0 or not hasattr(fn, "__code__"):
        return ""
    if not (getattr(fn, "__module__", "") or "").startswith("kestrel"):
        return ""
    seen.add(fn)
    try:
        out = inspect.getsource(fn)
    except OSError:
        return ""
    for name in fn.__code__.co_names:
        target = fn.__globals__.get(name)
        if isinstance(target, types.FunctionType):
            out += _reach(target, depth - 1, seen)
    return out


# --------------------------------------------------------------------- 1. consent: templates
SEND_MARKERS = re.compile(
    r"data-consent-send|consent_check\(|online_ask\.ask\(|data-consent-box|data-cl-ack"
    r"|name=\"confirmed\"|[\"']confirmed[\"']\s*:"
)


# Templates that match a marker without holding an online send.
NOT_A_SEND_TEMPLATE = {
    "partials/role_side.html": "its confirmed=1 is the Remove from my list confirmation, a local "
    "delete that sends nothing",
}


def _consent_templates() -> dict[str, str]:
    return {
        p.relative_to(TEMPLATES).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(TEMPLATES.rglob("*.html"))
        if p.relative_to(TEMPLATES).as_posix() not in NOT_A_SEND_TEMPLATE
        and SEND_MARKERS.search(p.read_text(encoding="utf-8"))
    }


# Templates whose ask-first dialog carries `confirmed` itself: the press of the dialog's own send
# button is the consent (the dialog names the provider, model and what is sent and has no box).
# Any new hidden `confirmed` field must be added here deliberately, with the reason.
HIDDEN_CONFIRMED_ALLOWED = {
    "partials/role_deeper.html": "dialog; the Send button is the consent, bound to the previewed "
    "payload by a token the server re-derives",
    "partials/role_online.html": "dialog (Research, Story); Send and continue is the consent and "
    "the server re-checks model and destination",
    "partials/pp_route.html": "carries the confirmation the practice dialog (pp_online.html) "
    "already collected into the next request",
    "partials/score_again_dialog.html": "dialog mode only: the field is disabled until the second "
    "step's Send button is pressed; page mode (the one with a box) never renders it",
    "partials/run_now_dialog.html": "dialog mode only: disabled until the second step's Send "
    "button is pressed; page mode (the one with a box) never renders it",
}

# Boxes that post under another name, because their route reads that name (checked below against
# the route source). Anything else must post as `confirmed`.
CONFIRM_NAME_ALLOWED = {
    "partials/bench_advanced.html": "confirm",
    "partials/bench_result.html": "confirm",
    "partials/models_change.html": "confirm",
}


def _call_args(text: str, start: int) -> str:
    """The argument text of the call whose "(" is at `start`, quotes respected."""
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    return text[start + 1 :]


def _checkbox_names(name: str, text: str) -> list[str]:
    """The `name` each consent box in the template posts under."""
    found = []
    for m in re.finditer(r"consent_check\(", text):
        if text[max(0, m.start() - 6) : m.start()] == "macro ":
            continue
        args = _call_args(text, m.end() - 1)
        kw = re.search(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", args)
        found.append(kw.group(1) if kw else "confirmed")
    for m in re.finditer(r"<input\b[^>]*>", text):
        tag = m.group(0)
        if 'type="checkbox"' in tag and re.search(r"data-consent-box|data-cl-ack", tag):
            nm = re.search(r'\bname="([^"]+)"', tag)
            if nm and "{{" in nm.group(1):
                continue  # the macro's own definition, checked by its default
            found.append(nm.group(1) if nm else "")
    return found


def test_the_consent_templates_are_discovered():
    found = _consent_templates()
    assert found, "no template with an online consent send was found"
    # Printed on every run (pytest -rP / on failure): the set this contract covers.
    print(f"\nconsent templates ({len(found)}): " + ", ".join(sorted(found)))
    for must in ("partials/online_ask.html", "partials/role_letter_ask.html"):
        assert must in found, f"{must} should be discovered, got {sorted(found)}"


def test_the_macro_default_box_posts_as_confirmed():
    text = (TEMPLATES / "partials/online_ask.html").read_text(encoding="utf-8")
    m = re.search(r"macro consent_check\(([^)]*)\)", text)
    assert m and re.search(r"name\s*=\s*'confirmed'", m.group(1)), m and m.group(1)


def test_no_hidden_confirmed_field_outside_the_allowlist():
    """A hidden `confirmed` pre-answers the consent the box is there to collect."""
    hidden = re.compile(
        r"<input\b(?=[^>]*type=\"hidden\")(?=[^>]*name=\"confirmed\")[^>]*>", re.IGNORECASE
    )
    found = {n for n, t in _consent_templates().items() if hidden.search(t)}
    extra = sorted(found - set(HIDDEN_CONFIRMED_ALLOWED))
    assert not extra, (
        f"hidden input name=confirmed in {extra}: a form with a consent box must not pre-answer "
        "it. A dialog with no box where the press is the consent may be added to "
        "HIDDEN_CONFIRMED_ALLOWED, with the reason."
    )
    stale = sorted(set(HIDDEN_CONFIRMED_ALLOWED) - found)
    assert not stale, f"allowlisted but no longer holding a hidden confirmed: {stale}"


def test_no_form_with_a_box_pre_answers_confirmed_in_its_values():
    """The same pre-answer by another route: a box beside hx-vals / a vals dict carrying
    confirmed=1 would let the press skip the box."""
    baked = re.compile(r"[\"']confirmed[\"']\s*:\s*[\"']1[\"']")
    bad = []
    for name, text in _consent_templates().items():
        if baked.search(text) and _checkbox_names(name, text):
            bad.append(name)
    assert not bad, f"{bad}: a template with a consent box also bakes confirmed=1 into the post"


def test_every_consent_box_posts_as_confirmed():
    names = {}
    for name, text in _consent_templates().items():
        for got in _checkbox_names(name, text):
            names.setdefault(name, set()).add(got)
    assert names, "no consent box found"
    print("\nconsent boxes: " + ", ".join(f"{n}={sorted(v)}" for n, v in sorted(names.items())))
    wrong = {
        n: sorted(v)
        for n, v in names.items()
        if v != {"confirmed"} and not (v == {CONFIRM_NAME_ALLOWED.get(n)})
    }
    assert not wrong, f"consent boxes that do not post as `confirmed`: {wrong}"


def test_a_renamed_box_is_one_the_route_reads(route_table):
    """The allowlisted `confirm` boxes are only safe if their routes read that field."""
    reads = 0
    for path, route in route_table:
        if path.startswith(("/bench/", "/models/change/")) and "POST" in route.methods:
            if re.search(r"""get\(["']confirm["']\)|["']confirm["']""", _reach(route.endpoint)):
                reads += 1
    assert reads >= 2, "no bench or models route reads `confirm`; the allowlist is stale"


# -------------------------------------------------------------------- 1. consent: endpoints
# A POST in a consent template that is not a send (it undoes, dismisses, accepts or deletes).
NOT_A_SEND = re.compile(r"undo|reset|dismiss|accept|delete|promote|put-back|/remove$|/cancel$")

# Templates that build their post URL in Python or by concatenation; the routes they post to.
DYNAMIC_URLS = {
    "partials/role_online.html": [
        "/opportunities/{opp_id}/role/story",
        "/opportunities/{opp_id}/role/research",
    ],
    "partials/role_deeper.html": ["/opportunities/{opp_id}/role/deeper/send"],
    "partials/doc_online_consent.html": [
        "/documents/slot/{slot_id}/suggest",
        "/documents/{master_id}/assess",
        "/api/documents/import",
    ],
    "partials/pp_online.html": ["/mock/{opportunity_id}/route"],
    "partials/pp_route.html": [],  # carries the answer into the practice routes above
    "partials/retry_online.html": ["/quarantine/{quarantine_id}/model"],
    "partials/offer_send_notice.html": [],  # included by the offers templates, which post
    "partials/online_ask.html": [],  # the macros; the forms around them post
}

# Handlers that call the consent check (found by reading their source) and are not posted from a
# template in this list, or are posted from script.
CODE_MARKERS = re.compile(
    r"consent_problem|approval_problem|online_run_refusal|_online_gate|online_problem"
    r"|\bgate\(|\"confirmed\"|'confirmed'|\bconfirmed:\s*str"
)
NOT_CONSENT_ROUTES = {
    "/opportunities/{opp_id}/role/remove": "a remove asks hx-confirm, it sends nothing online",
}

_URL = re.compile(r"""(?:hx-post=|action=|'hx-post':|\burl\s*=|post_url\s*=)\s*["']([^"']+)["']""")


def _consent_endpoints(route_table) -> dict[str, set[str]]:
    """POST route path -> the templates or code that make it a consent endpoint."""
    posts = {p: r for p, r in route_table if "POST" in r.methods}
    by_shape = {_norm(p): p for p in posts}
    out: dict[str, set[str]] = {}
    for name, text in _consent_templates().items():
        for url in _URL.findall(text):
            path = by_shape.get(_norm(url))
            if path and not NOT_A_SEND.search(path):
                out.setdefault(path, set()).add(name)
        for path in DYNAMIC_URLS.get(name, []):
            assert path in posts, f"{name}: dynamic target {path} is not a route"
            out.setdefault(path, set()).add(name)
    for path, route in posts.items():
        if path in NOT_CONSENT_ROUTES or NOT_A_SEND.search(path):
            continue
        if CODE_MARKERS.search(_reach(route.endpoint, depth=0)):
            out.setdefault(path, set()).add("handler source")
    return out


def test_every_consent_template_resolves_to_an_endpoint(route_table):
    """A template with a send that reaches no route is one this contract cannot see."""
    endpoints = _consent_endpoints(route_table)
    covered = {t for ts in endpoints.values() for t in ts}
    unresolved = [
        n for n, t in _consent_templates().items() if n not in covered and n not in DYNAMIC_URLS
    ]
    assert not unresolved, (
        f"{unresolved}: no POST route found for these consent templates. Add them to "
        "DYNAMIC_URLS (template builds its URL in code) with the routes they post to."
    )
    print(f"\nconsent endpoints ({len(endpoints)}): " + ", ".join(sorted(endpoints)))


# ------------------------------------------------------------------------ world and recorders
TIERS = ("tier1", "tier2", "tier3", "trigger", "docsel")
MODEL = "vendor/contract-model"


async def _world() -> dict[str, int]:
    """Fictional rows for every route that takes an id, and an online route for every tier."""
    ids = await role_seed.seed_async()
    async with get_sessionmaker()() as s:
        rows = {
            "llm.hosted_mode.enabled": True,
            "llm.hosted_mode.acknowledged_at": "2026-10-15T00:00:00Z",
            "llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms(),
        }
        for t in TIERS:
            rows[f"llm.tiers.{t}.backend"] = "frontier"
            rows[f"llm.tiers.{t}.model"] = MODEL
        for t in ("tier1", "tier2", "docsel"):
            rows[f"llm.hosted_tiers.{t}.backend"] = "frontier"
            rows[f"llm.hosted_tiers.{t}.model"] = MODEL
        for key, value in rows.items():
            await s.merge(Setting(key=key, value=value))
        applied = Pursuit(
            opportunity_id=ids["strong"],
            track="employment",
            status="applied",
            applied_at="2026-10-20T00:00:00Z",
        )
        offered = Pursuit(opportunity_id=ids["second"], track="employment", status="offer")
        s.add_all([applied, offered])
        await s.flush()
        s.add(Offer(pursuit_id=offered.id, base=120000, status="offer"))
        quarantined = Quarantine(
            opportunity_id=ids["maybe"],
            tier=1,
            model="m",
            raw_response="x",
            error="invalid",
            attempts=2,
        )
        s.add(quarantined)
        await s.flush()
        slot = (await s.execute(select(DocSlot).limit(1))).scalars().first()
        source = (await s.execute(select(Source).limit(1))).scalars().first()
        await s.commit()
        ids.update(
            {
                "pursuit": applied.id,
                "offer_pursuit": offered.id,
                "quarantine": quarantined.id,
                "slot": slot.id if slot else 0,
                "source": source.id,
            }
        )
        # The path parameters the routes use, by name.
        ids.update(
            {
                "opp_id": ids["strong"],
                "org_id": ids["org"],
                "master_id": ids["master"],
                "pursuit_id": ids["pursuit"],
                "quarantine_id": ids["quarantine"],
                "slot_id": ids["slot"],
                "source_id": ids["source"],
                "render_id": ids["render"],
            }
        )
    return ids


class Recorder:
    """Every model call and every network request the app tries; each one raises."""

    def __init__(self) -> None:
        self.llm: list[str] = []
        self.http: list = []

    def sent(self) -> list[str]:
        """The requests that could carry the person's data: not to this computer, and not a
        bodiless GET of a provider's model list (a catalog lookup that sends only the key the
        person saved for that provider, which several pages make to show what can be picked)."""
        out = []
        for req in self.http:
            host = req.url.host
            if host in ("127.0.0.1", "localhost", "::1"):
                continue
            catalog = (
                req.method == "GET"
                and not req.content
                and req.url.path.rstrip("/").endswith("/models")
            )
            if not catalog:
                out.append(f"{req.method} {req.url}")
        return out


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    import httpx

    rec = Recorder()

    async def complete(self, *a, **kw):
        rec.llm.append("complete")
        raise RuntimeError("test: a model call was made")

    async def embed(self, *a, **kw):
        rec.llm.append("embed")
        raise RuntimeError("test: a model call was made")

    monkeypatch.setattr(LLMClient, "complete", complete)
    monkeypatch.setattr(LLMClient, "embed", embed)

    async def ahandle(self, request):
        rec.http.append(request)
        raise httpx.ConnectError("test: the network is closed")

    def handle(self, request):
        rec.http.append(request)
        raise httpx.ConnectError("test: the network is closed")

    # The real transports only; TestClient talks to the app through its own.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return rec


async def _count_tasks() -> int:
    async with get_sessionmaker()() as s:
        return int((await s.execute(select(func.count()).select_from(Task))).scalar_one())


# Each consent endpoint: the form a browser posts WITHOUT the box ticked. A route listed in
# _consent_endpoints() with no entry here fails test_every_consent_endpoint_has_a_case.
#   data   the fields posted (on top of the hidden fields of `get`, when given)
#   get    a page that renders the form; its named hidden inputs are posted too, as a browser would
#   ids    path parameter -> key of the seeded row
#   prep   extra setup after the world is seeded: prep(ids, monkeypatch)
#   json   post a JSON body instead of a form
#   local_ok  the box is an opt-in to include online models (bench "Include online models"): left
#          unticked the post is a local-only run, so it need not show a refusal, but it still
#          must call no model, make no request and queue no task
CASES: dict[str, dict] = {}


def _case(
    path: str,
    data: dict | None = None,
    query: str = "",
    *,
    get: str = "",
    prep=None,
    json: dict | None = None,
    local_ok: bool = False,
    **ids: str,
) -> None:
    CASES[path] = {
        "data": data or {},
        "query": query,
        "get": get,
        "prep": prep,
        "json": json,
        "local_ok": local_ok,
        "ids": ids,
    }


def _prep_follow_up(ids, monkeypatch) -> None:
    from datetime import date

    async def go() -> None:
        async with get_sessionmaker()() as s:
            p = await s.get(Pursuit, ids["pursuit"])
            p.next_action = "Follow up"
            p.next_action_at = date.today().isoformat()
            await s.commit()

    asyncio.run(go())


def _prep_score_again(ids, monkeypatch) -> None:
    """Roles scored before the proof points were added, from a profile that has some."""
    from kestrel.services import profile_readiness as pr

    block = {
        "proof_points": [{"claim": "Cut late shipments", "evidence": "22 percent"}],
        "narrative": "An invented narrative.",
        "employment_terms": {"target_titles": ["Operations Analyst"]},
    }
    monkeypatch.setattr(pr, "operator_profile", lambda track: block)

    async def go() -> None:
        async with get_sessionmaker()() as s:
            await pr.mark_proof_points_changed(s, "employment")
            await s.commit()

    asyncio.run(go())


def _prep_bench_apply(ids, monkeypatch) -> None:
    from tests import model_check_seed

    model_check_seed.seed_check(online_best=True)


_case("/analytics/narrative")
_case("/orgs/{org_id}/summary/refresh", {"model": MODEL})
_case(
    "/pursuits/{pursuit_id}/follow-up", get="/pursuits/{pursuit_id}/follow-up", prep=_prep_follow_up
)
_case(
    "/offers/{pursuit_id}/scripts",
    {"situation": "counter", "walk_away": "100000", "model": MODEL},
    pursuit_id="offer_pursuit",
)
_case("/offers/prep/scripts", {"situation": PREP_SITUATION, "walk_away": "100000", "model": MODEL})
_case("/opportunities/{opp_id}/questions/regenerate", {"model": MODEL})
_case("/stories/build", {"model": MODEL})
_case("/profile/stories/build", {"model": MODEL})
_case("/title-suggestions/refresh", {"model": MODEL})
_case("/opportunities/{opp_id}/role/cover-letter/generate", {"model": MODEL})
_case("/opportunities/{opp_id}/role/research", {"model": MODEL})
_case("/opportunities/{opp_id}/role/story", {"situation": "An invented situation.", "rescore": "1"})
_case("/opportunities/{opp_id}/role/deeper/send", {"model": MODEL})
_case("/mock/{opportunity_id}/route", {"model": MODEL}, opportunity_id="strong")
_case("/quarantine/{quarantine_id}/model", {"model": f"hosted:{MODEL}"})
_case("/documents/slot/{slot_id}/suggest", {"model": MODEL})
_case("/documents/{master_id}/assess")
_case("/api/documents/import", {"text": "Alex Example\nOperations Analyst\nCut late shipments."})
_case("/rescore", {"model": f"hosted:{MODEL}", "opp_ids": "{opp_id}"})
_case("/api/profile/score-again", {"track": "employment"}, prep=_prep_score_again)
_case("/sources/{source_id}/run-now", {"mode": "hosted"})
_case("/runs/start", {"mode": "hosted"})
_case("/runs/now", {"mode": "hosted"})
_case("/api/run/now", {"mode": "hosted"}, "mode=hosted")
_case("/sources/check-all", {})
_case("/scan/start", {})
_case("/renders/{render_id}/pick", {"model": MODEL})
_case("/models/change/{tier}", {"where": "online", "model": MODEL})
_case("/bench/run", {"tier": "tier2", "model": f"hosted:{MODEL}", "backend": "frontier"})
_case("/bench/start", {"deeper": "on"}, local_ok=True)  # `online` left unticked
_case("/bench/check", json={"online": {"rating": [MODEL]}})
_case("/bench/apply", {"step": "rating"}, prep=_prep_bench_apply)
_case("/opportunities/{opp_id}/decide/keep-anyway", {})

PARAMS = {"tier": "tier2"}  # a path parameter that is a name, not a row


def _fill(path: str, ids: dict[str, int], pick: dict[str, str] | None = None) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in PARAMS:
            return PARAMS[name]
        return str(ids.get((pick or {}).get(name) or name, 0))

    return re.sub(r"\{(\w+)\}", sub, path)


def test_every_consent_endpoint_has_a_case(route_table):
    endpoints = _consent_endpoints(route_table)
    missing = sorted(set(endpoints) - set(CASES))
    stale = sorted(set(CASES) - set(endpoints))
    assert not missing, (
        f"consent endpoints with no CASES entry (add one, with the form a browser posts "
        f"unticked): {missing}"
    )
    assert not stale, f"CASES entries that are no longer consent endpoints: {stale}"


REFUSAL_WORDS = (
    "nothing was sent",
    "tick the box",
    "data-consent-box",
    "data-cl-ack",
    "not started",
    "confirm in the",
    "confirm what",
    "tick the",
    "ask first",
    "what will be sent",
    "what is sent",
    "online model",
    "did not start",
    "not sent",
    "held",
)


def _shows_refusal(r) -> bool:
    shown = (r.text + " " + " ".join(f"{k} {v}" for k, v in r.headers.items())).lower()
    if r.status_code in (400, 403, 409, 412, 423):
        return True
    return 200 <= r.status_code < 300 and any(w in shown for w in REFUSAL_WORDS)


def _hidden_fields(html: str) -> dict[str, str]:
    """What a browser posts from the page's forms with every box left unticked: the named hidden
    inputs and selects' values; a checkbox posts nothing when it is not ticked."""
    from bs4 import BeautifulSoup

    out: dict[str, str] = {}
    for i in BeautifulSoup(html, "html.parser").find_all("input"):
        if i.get("name") and i.get("type") == "hidden" and not i.has_attr("disabled"):
            out[i["name"]] = i.get("value", "")
    return out


@pytest.mark.parametrize("path", sorted(CASES))
def test_an_unconfirmed_post_is_refused_and_sends_nothing(path, recorder, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")  # a provider is connected
    ids = asyncio.run(_world())
    case = CASES[path]
    if case["prep"]:
        case["prep"](ids, monkeypatch)
    url = _fill(path, ids, case["ids"]) + (f"?{case['query']}" if case["query"] else "")
    before = asyncio.run(_count_tasks())
    with TestClient(_build_app(), raise_server_exceptions=False) as client:
        data = {}
        if case["get"]:
            page = client.get(_fill(case["get"], ids, case["ids"]))
            data.update(_hidden_fields(page.text))
        data.update({k: _fill(v, ids) if "{" in v else v for k, v in case["data"].items()})
        headers = {"HX-Request": "true"}
        if case["json"] is not None:
            r = client.post(url, json=case["json"], headers=headers)
        else:
            r = client.post(url, data=data, headers=headers)
    where = f"POST {url} {data or case['json']} -> {r.status_code}: {r.text[:300]!r}"
    assert r.status_code not in (404, 405, 422) and r.status_code < 500, (
        f"the request did not reach the consent check (fix the case): {where}"
    )
    import html as _html

    seen = _html.unescape(re.sub(r"<[^>]+>|\s+", " ", r.text)).strip()
    print(f"\n{path} -> {r.status_code} {seen[:140]!r}")
    after = asyncio.run(_count_tasks())
    _assert_nothing_happened(recorder, before, after, where)
    # Secondary: the person is told. Wording alone never passes the test; the effects above do.
    assert case["local_ok"] or _shows_refusal(r), f"an unconfirmed post was not refused: {where}"


def _assert_nothing_happened(rec: Recorder, before: int, after: int, where: str) -> None:
    """The primary contract of every consent endpoint, whatever it answers: no model call, no
    network request that could carry the person's data, and no task queued. A 2xx page that says
    "tick the box" but went ahead fails here."""
    assert rec.llm == [], f"a model was called: {where}"
    assert rec.sent() == [], f"a network request was made: {rec.sent()} {where}"
    assert after == before, f"a task was queued ({before} -> {after}): {where}"


def test_refusal_wording_cannot_excuse_a_post_that_proceeded():
    """A 200 page full of refusal words does not pass when the post called a model, made a
    network request or queued a task."""
    import httpx

    words = "Nothing was sent. Tick the box to send."
    where = f"POST /x -> 200: {words!r}"
    clean = Recorder()
    _assert_nothing_happened(clean, 0, 0, where)  # nothing happened: fine
    called = Recorder()
    called.llm.append("complete")
    with pytest.raises(AssertionError, match="a model was called"):
        _assert_nothing_happened(called, 0, 0, where)
    sent = Recorder()
    sent.http.append(httpx.Request("POST", "https://api.example.invalid/v1/chat", content=b"x"))
    with pytest.raises(AssertionError, match="a network request was made"):
        _assert_nothing_happened(sent, 0, 0, where)
    with pytest.raises(AssertionError, match="a task was queued"):
        _assert_nothing_happened(Recorder(), 0, 1, where)


# ------------------------------------------------------------------------ 2. files
SERVES = re.compile(r"FileResponse")
DELETES = re.compile(r"\.unlink\(|os\.remove|rmtree")
STORED = re.compile(r"pdf_path|stored_pdf|stored_prep_pdf")


def _file_routes(route_table) -> dict[str, str]:
    """Route -> "METHOD path" for every route that serves or deletes a file by a stored path."""
    out = {}
    for path, route in route_table:
        src = _reach(route.endpoint)
        if (SERVES.search(src) or DELETES.search(src)) and STORED.search(src):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out[f"{method} {path}"] = path
    return out


OUTSIDE_PDF = b"%PDF-1.4\ninvented text outside the documents folder\n%%EOF\n"

FILE_ROUTES = {
    "GET /renders/{render_id}/download",
    "GET /prep/{prep_pack_id}/download",
    "GET /opportunities/{opp_id}/role/cover-letter/{render_id}/download",
    "POST /opportunities/{opp_id}/role/cover-letter/{render_id}/save",
    "POST /opportunities/{opp_id}/role/cover-letter/{render_id}/discard",
    # Draft again (D-LETTER-4): keeping one draft deletes another, and Undo deletes the newer.
    "POST /opportunities/{opp_id}/role/cover-letter/{render_id}/keep-new",
    "POST /opportunities/{opp_id}/role/cover-letter/{render_id}/keep-previous",
    "POST /opportunities/{opp_id}/role/cover-letter/{render_id}/undo",
    "POST /documents/{master_id}/delete",
}


def test_the_file_routes_are_discovered(route_table):
    found = _file_routes(route_table)
    assert found, "no route that serves or deletes a stored file was found"
    print(f"\nfile routes ({len(found)}): " + ", ".join(sorted(found)))
    assert set(found) == FILE_ROUTES, (
        f"new or removed file routes: {sorted(set(found) ^ FILE_ROUTES)}. Add a case to "
        "test_a_stored_path_outside_the_documents_folder_is_left_alone and list it in FILE_ROUTES."
    )


def _stored(where: str, tmp_path: Path, folder: Path) -> Path:
    """A real file the database will name: inside `folder` (the control), or outside it."""
    if where == "inside":
        target = folder / "invented-control.pdf"
    elif where == "elsewhere":
        target = tmp_path / "elsewhere" / "outside.pdf"
    else:  # a traversal that starts inside the folder
        target = folder / ".." / "outside-by-traversal.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(OUTSIDE_PDF)
    return target


async def _file_world(doc_pdf: str, prep_pdf: str) -> dict[str, int]:
    import hashlib

    ids = await role_seed.seed_async()
    body = "Invented letter text."
    async with get_sessionmaker()() as s:
        letter = DocRender(
            opportunity_id=ids["strong"],
            doc_master_id=ids["master"],
            template="cover_letter",
            config={
                "kind": "cover_letter",
                "pdf_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            },
            pdf_path=doc_pdf,
            filename="letter.pdf",
            page_count=1,
        )
        tailored = DocRender(
            opportunity_id=ids["strong"],
            doc_master_id=ids["master"],
            config={},
            pdf_path=doc_pdf,
            filename="resume.pdf",
            page_count=1,
        )
        pack = PrepPack(
            opportunity_id=ids["strong"], content_md="Invented pack.", pdf_path=prep_pdf
        )
        s.add_all([letter, tailored, pack])
        await s.flush()
        # The letter's text lives in the letter table, attached to its render (D-LETTER-1).
        s.add(Letter(render_id=letter.id, opportunity_id=ids["strong"], kind="cover_letter",
                     body_md=body, generated_md=body, word_count=3))
        # The seeded render is the one the master delete removes: it names the file too.
        seeded = await s.get(DocRender, ids["render"])
        seeded.pdf_path = doc_pdf
        await s.commit()
        ids.update(
            {
                "letter": letter.id,
                "tailored": tailored.id,
                "pack": pack.id,
                "opp_id": ids["strong"],
                "master_id": ids["master"],
            }
        )
    return ids


@pytest.fixture
def no_browser(monkeypatch):
    """Making a PDF would start a browser; the contract only cares which file is served."""
    from kestrel.docs import render as docrender

    async def refuse(*a, **kw):
        raise RuntimeError("test: no browser")

    monkeypatch.setattr(docrender, "render_pdf", refuse)


async def _letter_state(path: str, ids: dict[str, int]) -> int:
    """Keep new, Keep previous and Undo delete a letter only in one state (D-LETTER-4): put the
    seeded letter, the one that names the file, in the state each one deletes. Returns the render
    id the route is asked about."""
    async with get_sessionmaker()() as s:
        seeded = (await s.execute(
            select(Letter).where(Letter.render_id == ids["letter"]))).scalars().one()
        if path.endswith("/keep-previous"):
            seeded.status = "proposed"  # the new draft the person declines
        elif path.endswith(("/keep-new", "/undo")):
            other = DocRender(opportunity_id=ids["strong"], doc_master_id=ids["master"],
                              template="cover_letter", config={"kind": "cover_letter"})
            s.add(other)
            await s.flush()
            keeping = path.endswith("/keep-new")
            s.add(Letter(render_id=other.id, opportunity_id=ids["strong"], kind="cover_letter",
                         body_md="Another invented letter.",
                         status="proposed" if keeping else "previous"))
            if keeping:  # keeping the new draft lets go of the oldest one, the seeded one
                seeded.status = "previous"
                await s.commit()
                return other.id
        await s.commit()
    return ids["letter"]


def _drive_file_route(route, where, tmp_path):
    doc = _stored(where, tmp_path, paths.documents_dir())
    prep = _stored(where, tmp_path / "prep", paths.prep_packs_dir())
    ids = asyncio.run(_file_world(str(doc), str(prep)))
    method, path = route.split(" ", 1)
    ids["prep_pack_id"] = ids["pack"]
    ids["render_id"] = (asyncio.run(_letter_state(path, ids)) if "cover-letter" in path
                        else ids["tailored"])
    url = _fill(path, ids)
    data = ({"body_md": "A new invented text."} if path.endswith("/save")
            else {"sure": "1"} if path.endswith("/undo") else {})
    with TestClient(_build_app(), raise_server_exceptions=False) as client:
        r = client.request(method, url, data=data or None)
    shown = f"{route} ({where}) -> {r.status_code}: {r.text[:200]!r}"
    return method, doc, prep, r, shown


@pytest.mark.parametrize("route", sorted(r for r in FILE_ROUTES if r.startswith("GET ")))
def test_a_stored_path_inside_the_documents_folder_is_served(route, tmp_path, no_browser):
    """The control: the same request, the same route, with the file where it belongs, must serve
    it, which proves the outside cases reach the code they are meant to test."""
    _method, _doc, _prep, r, shown = _drive_file_route(route, "inside", tmp_path)
    assert r.status_code == 200, f"control not served: {shown}"
    assert r.content == OUTSIDE_PDF, f"control not served: {shown}"


@pytest.mark.parametrize("route", sorted(r for r in FILE_ROUTES if not r.startswith("GET ")))
def test_a_stored_path_inside_the_documents_folder_is_deleted(route, tmp_path, no_browser):
    """The control for the delete routes: the file where it belongs is removed."""
    _method, doc, _prep, r, shown = _drive_file_route(route, "inside", tmp_path)
    assert r.status_code not in (404, 405, 422), shown
    assert r.status_code < 500 or r.status_code == 503, shown
    assert not doc.exists(), f"control file not deleted (the case never got there): {shown}"


@pytest.mark.parametrize("where", ["elsewhere", "traversal"])
@pytest.mark.parametrize("route", sorted(FILE_ROUTES))
def test_a_stored_path_outside_the_documents_folder_is_left_alone(
    route, where, tmp_path, no_browser
):
    method, doc, prep, r, shown = _drive_file_route(route, where, tmp_path)
    assert r.status_code not in (405, 422), shown
    assert method == "GET" or r.status_code != 404, shown
    assert r.status_code < 500 or r.status_code == 503, shown
    assert OUTSIDE_PDF not in r.content, f"{route} served a file from outside the folder"
    assert doc.read_bytes() == OUTSIDE_PDF and prep.read_bytes() == OUTSIDE_PDF, (
        f"{route} changed or deleted a file outside the folder: {shown}"
    )
    # Refusing the stored path is the contract. A route may then make a fresh PDF from the saved
    # text (renders download does when a renderer is available), which is never the outside file:
    # OUTSIDE_PDF is checked above. Whether it can render depends on the machine, so 200 (a fresh
    # PDF), 404 and 503 are all refusals of the stored path for a GET.
    assert method != "GET" or r.status_code in (200, 404, 503), shown
