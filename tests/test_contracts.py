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
import os
import re
import shutil
import sys
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
from tests.symlink_probe import link_folder

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
def route_table(tmp_path_factory) -> list[tuple[str, object]]:
    """Built in a data home of its own. A module fixture runs before the function-scoped
    `_isolate_data_home`, so without this it reads the home the previous test in the worker left
    set, and a settings file that test wrote fails the build (order-dependent; stress seed
    1240528)."""
    from tests import throwaway

    before = os.environ.get("KESTREL_DATA_HOME")
    home = tmp_path_factory.mktemp("kestrel_home_routes")
    throwaway.mark(home)
    os.environ["KESTREL_DATA_HOME"] = str(home)
    try:
        return list(_walk(_build_app().routes))
    finally:
        if before is None:
            os.environ.pop("KESTREL_DATA_HOME", None)
        else:
            os.environ["KESTREL_DATA_HOME"] = before


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


def _off_machine(rec) -> list[str]:
    return [f"{q.method} {q.url}" for q in rec.http
            if q.url.host not in ("127.0.0.1", "localhost", "::1")]


def _no_catalog_requests(monkeypatch) -> None:
    """The provider's model list and the key's credit, answered without the network, so these
    tests attempt no request at all (the recorder and the socket guard would block one)."""
    from kestrel.llm.providers import ModelInfo
    from kestrel.services import hosted_balance, hosted_models

    async def models(name, backend, *, refresh=False):
        return [ModelInfo(id=MODEL, name="Contract Model", context_length=131072,
                          structured_outputs=True, json_mode="schema")]

    async def no_balance(backend):
        return hosted_balance.Reading(False)

    monkeypatch.setattr(hosted_models, "fetch_models", models)
    monkeypatch.setattr(hosted_balance, "_fetch", no_balance)


def _post_unconfirmed(path: str, monkeypatch):
    """World up, the task count taken, then the case's form posted with every box left unticked.
    The count must come after the world exists (a fresh test database has no tables yet).
    Returns (where, r, before)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")  # a provider is connected
    _no_catalog_requests(monkeypatch)
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
    return where, r, before


@pytest.mark.parametrize("path", sorted(CASES))
def test_an_unconfirmed_post_reaches_the_consent_check_and_sends_nothing(
    path, recorder, monkeypatch
):
    """Every consent endpoint, whatever it answers: the request reaches the consent check, no
    model is called, nothing leaves the machine, and no task is queued."""
    where, r, before = _post_unconfirmed(path, monkeypatch)
    assert r.status_code not in (404, 405, 422) and r.status_code < 500, (
        f"the request did not reach the consent check (fix the case): {where}"
    )
    after = asyncio.run(_count_tasks())
    _assert_nothing_happened(recorder, before, after, where)
    # With the catalog and credit lookups answered locally, no request leaves this computer,
    # not even a bodiless lookup (a probe of the local model server on loopback is allowed).
    assert _off_machine(recorder) == [], where


# The bench/start case is the one exception: its 200 page is a local-only run whose own copy
# may name the "online model" box the person left unticked, so it is refused nothing. Every
# other unconfirmed post is refused in words as well as in effects.
REFUSAL_CASES = [p for p, c in sorted(CASES.items()) if not c["local_ok"]]


@pytest.mark.parametrize("path", REFUSAL_CASES)
def test_an_unconfirmed_post_that_must_be_refused_is_told_so(path, recorder, monkeypatch):
    where, r, _before = _post_unconfirmed(path, monkeypatch)
    # Wording alone never passes a consent test; the effects test above does. This pins the
    # second half: the person is told nothing was sent.
    assert _shows_refusal(r), f"an unconfirmed post was not refused: {where}"
    assert _off_machine(recorder) == [], where


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
# A download is a FileResponse, or the bytes of a stored file read through paths.read_servable
# and sent with pdf_download (D-SEC-55).
SERVES = re.compile(r"FileResponse|pdf_download\(")
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
        "test_an_outside_stored_path_is_never_served_by_a_get / "
        "test_an_outside_stored_path_is_left_alone_by_the_write_routes and list it in FILE_ROUTES."
    )


def _stored(where: str, tmp_path: Path, folder: Path) -> Path:
    """A real file the database will name: inside `folder` (the control), or outside it."""
    if where == "inside":
        target = folder / "invented-control.pdf"
    elif where == "elsewhere":  # an absolute path to a file outside the folder
        target = tmp_path / "elsewhere" / "outside.pdf"
    elif where == "symlink":
        # The link itself sits inside the folder; what it points at is outside. A check that
        # resolves its argument refuses it; a check of the stored spelling alone would not.
        real = tmp_path / "elsewhere" / "outside-via-symlink.pdf"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(OUTSIDE_PDF)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # A file symlink needs privilege on Windows; a junction to the folder that holds
            # the file does not, and hides the same file behind an inside spelling.
            import _winapi

            alias = folder / "alias"
            _winapi.CreateJunction(str(real.parent), str(alias))
            return alias / real.name
        os.symlink(real, folder / "link-via-symlink.pdf")
        return folder / "link-via-symlink.pdf"
    elif where == "folder-link":
        # The folder itself replaced by a link to another folder: every stored path inside it
        # resolves into that other folder, so only a check of the folder itself refuses it.
        real = tmp_path / "elsewhere-folder" / "outside-via-folder-link.pdf"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(OUTSIDE_PDF)
        folder.parent.mkdir(parents=True, exist_ok=True)
        if folder.exists():
            shutil.rmtree(folder)
        link_folder(folder, real.parent)
        return folder / real.name
    elif where == "hardlink":
        # A second name inside the folder for a file outside it: it resolves inside, so only a
        # check of the file itself (a single name) refuses it.
        real = tmp_path / "elsewhere" / "outside-via-hardlink.pdf"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(OUTSIDE_PDF)
        folder.mkdir(parents=True, exist_ok=True)
        os.link(real, folder / "link-via-hardlink.pdf")
        return folder / "link-via-hardlink.pdf"
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
    assert r.status_code == 200, shown
    assert not doc.exists(), f"control file not deleted (the case never got there): {shown}"


OUTSIDE_WHERE = ["elsewhere", "traversal", "symlink"]
# A hard link inside the folder is only a serving case: removing that name is a removal inside
# the folder, and the outside file keeps its own name.
SERVED_OUTSIDE_WHERE = [*OUTSIDE_WHERE, "hardlink", "folder-link"]


@pytest.mark.parametrize("where", SERVED_OUTSIDE_WHERE)
@pytest.mark.parametrize("route", sorted(r for r in FILE_ROUTES if r.startswith("GET ")))
def test_an_outside_stored_path_is_never_served_by_a_get(route, where, tmp_path, no_browser):
    """Every shape of a path that is not where it belongs: an absolute path to a file outside
    the folder ("elsewhere"), a `..` climb out of it ("traversal"), and a link that sits inside
    it and leads out ("symlink"). None is served: a fresh PDF made from the saved text (when a
    renderer is available), a 404 refusal, or a 503 (no renderer) is every answer the route may
    give, and none of them is the outside file (issue #150: a restored backup may name any
    readable file)."""
    _method, doc, prep, r, shown = _drive_file_route(route, where, tmp_path)
    assert r.status_code not in (405, 422), shown
    assert r.status_code in (200, 404, 503), shown
    assert OUTSIDE_PDF not in r.content, f"{route} served a file from outside the folder"
    assert doc.read_bytes() == OUTSIDE_PDF and prep.read_bytes() == OUTSIDE_PDF, (
        f"{route} changed or deleted a file outside the folder: {shown}"
    )


@pytest.mark.parametrize("where", OUTSIDE_WHERE)
@pytest.mark.parametrize("route", sorted(r for r in FILE_ROUTES if not r.startswith("GET ")))
def test_an_outside_stored_path_is_left_alone_by_the_write_routes(route, where, tmp_path,
                                                                  no_browser):
    """The write routes (delete, save, discard, keep, undo) must not touch a file that lives
    outside the documents folder: the row operation succeeds (200), the outside file is left
    exactly as it was (issue #150: a restored backup may name any readable file)."""
    _method, doc, prep, r, shown = _drive_file_route(route, where, tmp_path)
    assert r.status_code == 200, shown
    assert OUTSIDE_PDF not in r.content, f"{route} served a file from outside the folder"
    assert doc.read_bytes() == OUTSIDE_PDF and prep.read_bytes() == OUTSIDE_PDF, (
        f"{route} changed or deleted a file outside the folder: {shown}"
    )


# ------------------------------------------------------------------------ 3. modal sends
# kestrel-ui.js's delegated click handler matches data-k-close before the browser sees a
# submit: it closes the dialog and cancels the click, so the form never submits and htmx
# never sends it. #172 shipped that way: the Restore confirm in Settings > Your data closed
# and restored nothing. Every type="submit" in a dialog must therefore send on its own, and
# a button that swallows the send on purpose must be listed here.
SUBMIT_BUTTON = re.compile(r"<button\b[^>]*>")


def _submit_close_buttons() -> dict[str, list[int]]:
    """Template -> the lines of its submit buttons that also carry data-k-close."""
    out: dict[str, list[int]] = {}
    for f in sorted(TEMPLATES.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        hits = [
            text[: m.start()].count("\n") + 1
            for m in SUBMIT_BUTTON.finditer(text)
            if re.search(r"type\s*=\s*[\"']submit[\"']", m.group(0))
            and "data-k-close" in m.group(0)
        ]
        if hits:
            out[str(f.relative_to(TEMPLATES))] = hits
    return out


# A type="submit" button that also carries data-k-close never submits: kestrel-ui.js closes
# the dialog and cancels the click. List one here only if swallowing the send is the intent;
# none is today.
INTENDED_EXCEPTIONS: dict[str, list[int]] = {}


def test_no_submit_button_in_a_dialog_is_silenced_by_data_k_close():
    found = _submit_close_buttons()
    bad = {n: lines for n, lines in found.items() if lines != INTENDED_EXCEPTIONS.get(n, [])}
    assert not bad, (
        f"a type=\"submit\" button also carrying data-k-close never submits (kestrel-ui.js "
        f"closes the dialog and cancels the click): {bad}. Remove data-k-close from the "
        "button and close the dialog from the reply, or list the button in "
        "INTENDED_EXCEPTIONS if swallowing the send is the intent."
    )


# ------------------------------------------------------------------ 3. consent: correction lines
# Issue #178 (D-PRIV-670). Invariant: every panel that confirms an online scoring send lists
# exactly the correction lines that send carries, from the same rows Tier 2 sends
# (`operator_correction_lines(scoring_corrections(...))`), and posts their digest, so a change
# while the panel is open sends nothing. Found by scanning: every POST route whose handler
# reaches `enqueue_rescore` needs a driver in LINES_PANELS, so a new scoring panel without the
# comparison fails test_every_scoring_send_has_a_lines_panel.
LINE_A = "I ran the night shift rota for two invented winters."
LINE_B = "Only one invented winter, and only as a stand-in."
PROCESS_REQ = "Process improvement across distribution centers"  # on the seeded strong role


def _soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")


def _correct(c, opp: int, note: str, reason: str = "not_me") -> None:
    from kestrel.match_text import requirement_hash

    r = c.post(
        f"/opportunities/{opp}/role/correction",
        data={"h": requirement_hash(PROCESS_REQ), "reason": reason, "note": note},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text[:300]


def _hidden(node) -> dict[str, str]:
    return {
        i["name"]: i.get("value", "")
        for i in node.select("input[type=hidden][name]")
        if not i.has_attr("disabled")
    }


def _listed(node, selector: str = "[data-correction-lines] li") -> list[str]:
    return [li.get_text() for li in node.select(selector)]


def _score_again_page(c, url: str, ids, extra: dict) -> tuple[list[str], str, dict]:
    page = _soup(c.get(url).text)
    form = page.select_one("form[data-score-again-form]")
    assert form is not None, f"no Score again form at {url}"
    data = _hidden(form)
    data.update({"model_choice": "same", "model": "same", "confirmed": "1", **extra})
    # The ask-first step lists what is sent; the main step lists the same lines.
    return _listed(page, "[data-testid=score-again-sends-corrections] li"), "/rescore", data


# Each driver opens its panel the way the browser does: (lines listed, url, the confirmed form).
def _drive_rescore(c, ids):
    return _score_again_page(c, f"/rescore/choose?opp_ids={ids['strong']}", ids, {})


def _set_aside(ids) -> None:
    """A Tier 0 rule set the strong role aside: the panels that score it anyway apply."""
    from kestrel.db.models import Opportunity

    async def go() -> None:
        async with get_sessionmaker()() as s:
            (await s.get(Opportunity, ids["strong"])).stage = "t0_reject"
            await s.commit()

    asyncio.run(go())


def _drive_keep_anyway(c, ids):
    _set_aside(ids)
    r = c.post(f"/opportunities/{ids['strong']}/decide/keep-anyway",
               headers={"HX-Request": "true"})
    to = r.headers.get("HX-Redirect", "")
    assert to.startswith("/rescore/choose?"), f"Keep it anyway did not open Score again: {to!r}"
    return _score_again_page(c, to, ids, {"tier0_anyway": "1"})


def _drive_quarantine_anyway(c, ids):
    _set_aside(ids)
    q = ids["lines_quarantine"]
    r = c.post(f"/quarantine/{q}/anyway", headers={"HX-Request": "true"})
    to = r.headers.get("HX-Redirect", "")
    assert to.startswith("/rescore/choose?"), f"Score it anyway did not open Score again: {to!r}"
    return _score_again_page(c, to, ids, {"tier0_anyway": "1"})


def _drive_profile(c, ids):
    r = c.post("/api/profile/score-again", data={"track": "employment"},
               headers={"HX-Request": "true"})
    page = _soup(r.text)
    form = page.select_one("form[data-testid=score-again-ask-form]")
    assert form is not None, f"no profile ask panel: {r.text[:300]!r}"
    return _listed(form), "/api/profile/score-again", {**_hidden(form), "confirmed": "1"}


def _drive_quarantine_model(c, ids):
    q = ids["lines_quarantine"]
    page = _soup(c.get(f"/quarantine/{q}/online").text)
    pick = page.select_one("#rt-online-model option")
    assert pick is not None, "the online dialog offers no model"
    data = {**_hidden(page), "model": pick["value"], "confirmed": "1"}
    return _listed(page), f"/quarantine/{q}/model", data


def _drive_story(c, ids):
    url = f"/opportunities/{ids['strong']}/role/story"
    r = c.post(url, data={"situation": "An invented situation.", "rescore": "1"},
               headers={"HX-Request": "true"})
    page = _soup(r.text)
    form = page.select_one("#rl-online-form")
    assert form is not None, f"no story ask dialog: {r.text[:300]!r}"
    return _listed(page), url, _hidden(form)


LINES_PANELS = {
    "/rescore": _drive_rescore,
    "/opportunities/{opp_id}/decide/keep-anyway": _drive_keep_anyway,
    "/quarantine/{quarantine_id}/anyway": _drive_quarantine_anyway,
    "/api/profile/score-again": _drive_profile,
    "/quarantine/{quarantine_id}/model": _drive_quarantine_model,
    "/opportunities/{opp_id}/role/story": _drive_story,
}


# Discovery is by running, never by reading source (D-PRIV-674): every mutating route is posted
# with an online approval stamp and either no corrections digest or a stale one, while a
# correction exists. Two hooks record what happened: `correction_lines._observe` sees every
# confirmation attempt, and `queue.rescore._require_confirmed` sees every attempt to stage a
# scoring task. Both are module globals looked up at call time, so a route reaches them however it
# imports enqueue_rescore (module level, function local, through a helper).
#   - A route that tried to confirm lines is a scoring send: it must have a LINES_PANELS driver.
#   - Any staging attempt with an approval and no matching confirmation is a violation.
#   - Any task that carries an online approval appearing after such a post is a violation.
STALE_DIGEST = "0" * 64

# Routes the sweep does not post, with the reason. Each one is not a scoring send.
SWEEP_EXEMPT = {
    "/api/quit": "asks the desktop shell to quit; it queues nothing",
}


# Routes that refuse the sweep's generic form (a 4xx) before doing anything, by kind. A refused
# post never reaches `confirm_corrections`, so a scoring route among them would pass unseen: every
# 4xx outside this list fails the sweep, any 5xx fails it always, and an entry whose route no
# longer refuses fails it too (round 4 finding 1). None of these queues a score: each kind says why
# it answers before any send.
REFUSES_NO_ROW = "its path names a row the sweep world does not seed (id 0): 404, form unread"
REFUSES_NOT_FOUND = "a form field the generic form lacks names its row or rule: 404, nothing done"
REFUSES_FORM = "needs its own form fields or values; the generic form is refused (400 or 422)"
REFUSES_STATE = "refused for the state the world is in (nothing to undo, a run already going): 409"
REFUSES_DELETES = "deletes its own row on the first post, so the second post finds nothing: 404"
REFUSES_KEY = ("a browser-extension route: it needs X-Kestrel-Key, which the sweep's form never "
               "carries (D-EXT-741), so 403; /api/extension/features takes PUT only, so 405. None "
               "of them can queue an online score: Read queues a local embed only")

REFUSES_NO_WINDOW = ("the desktop window's own channel: without the shell's window registered "
                     "(every test app) it is not there: 404, nothing done (D-SEC-61)")
SWEEP_REFUSES = {
    "/api/window/call/{name}": REFUSES_NO_WINDOW,
    "/api/diag/page": REFUSES_FORM,
    "/api/extension/features": REFUSES_KEY,
    "/api/extension/roles/{opp_id}/applied": REFUSES_KEY,
    "/api/extension/roles/{opp_id}/description": REFUSES_KEY,
    "/api/learning/calibration/{cal_id}/accept": REFUSES_NO_ROW,
    "/api/learning/calibration/{cal_id}/reject": REFUSES_NO_ROW,
    "/api/learning/label": REFUSES_FORM,
    "/api/opportunities/ingest": REFUSES_FORM,
    "/api/opportunities/lookup": REFUSES_FORM,
    "/bench/check": REFUSES_STATE,
    "/calibrate/quick/answer": REFUSES_FORM,
    "/calibrate/quick/undo/{label_id}": REFUSES_NO_ROW,
    "/calibrate/rate": REFUSES_FORM,
    "/calibrate/skip": REFUSES_FORM,
    "/calibrate/tune-undo/{cal_id}": REFUSES_NO_ROW,
    "/calibrate/tune/{cal_id}/{decision}": REFUSES_FORM,
    "/calibrate/undo/{label_id}": REFUSES_NO_ROW,
    "/documents/section/{section_id}": REFUSES_NO_ROW,
    "/documents/section/{section_id}/bullets": REFUSES_NO_ROW,
    "/documents/section/{section_id}/confirm-current": REFUSES_NO_ROW,
    "/documents/section/{section_id}/delete": REFUSES_NO_ROW,
    "/documents/slot/{slot_id}/delete": REFUSES_DELETES,
    "/documents/variant/{variant_id}": REFUSES_NO_ROW,
    "/documents/{master_id}/sections/{kind}": REFUSES_NO_ROW,
    "/documents/{master_id}/sections/{kind}/{entry_id}": REFUSES_NO_ROW,
    "/documents/{master_id}/sections/{kind}/{entry_id}/delete": REFUSES_NO_ROW,
    "/documents/{master_id}/skills/{key}/delete": REFUSES_NO_ROW,
    "/documents/{master_id}/summary/{key}/delete": REFUSES_NO_ROW,
    "/mock/session/{mock_session_id}/answer": REFUSES_NO_ROW,
    "/mock/session/{mock_session_id}/claim": REFUSES_FORM,
    "/mock/session/{mock_session_id}/end": REFUSES_NO_ROW,
    "/mock/session/{mock_session_id}/skip": REFUSES_NO_ROW,
    "/onboarding/edit/{key}": REFUSES_NO_ROW,
    "/onboarding/models/action/{name}": REFUSES_NO_ROW,
    "/onboarding/models/mode": REFUSES_FORM,
    "/onboarding/restart": REFUSES_FORM,
    "/opportunities/{opp_id}/role/cover-letter/{render_id}/redraft": REFUSES_NOT_FOUND,
    "/opportunities/{opp_id}/role/cover-letter/{render_id}/save": REFUSES_NOT_FOUND,
    "/opportunities/{opp_id}/role/view/undo/{override_id}": REFUSES_NO_ROW,
    "/prep/{prep_pack_id}/pdf": REFUSES_NO_ROW,
    "/profile/corrections/{cid}": REFUSES_FORM,
    "/profile/corrections/{cid}/remove": REFUSES_NO_ROW,
    "/profile/corrections/{cid}/restore": REFUSES_STATE,
    "/profile/undo": REFUSES_STATE,
    "/profile/{track}/{section}": REFUSES_NO_ROW,
    "/renders/{render_id}/skills": REFUSES_FORM,
    "/renders/{render_id}/swap": REFUSES_FORM,
    # A retry the row cannot run now (here the role is already waiting, from the Needs another
    # try route swept just before) queues nothing and spends no try: 409 (D-UI-790).
    "/runs/quarantine/{quarantine_id}/retry": REFUSES_STATE,
    "/runs/start": REFUSES_STATE,
    "/settings/appearance": REFUSES_FORM,
    "/settings/calibration/{calibration_id}/{action}": REFUSES_FORM,
    "/settings/computer/action/{name}": REFUSES_NO_ROW,
    "/settings/computer/mode": REFUSES_FORM,
    "/settings/roles/cutoffs": REFUSES_FORM,
    "/settings/roles/number": REFUSES_NOT_FOUND,
    "/settings/roles/rule": REFUSES_NOT_FOUND,
    "/settings/roles/unhide/{org_id}": REFUSES_NOT_FOUND,
    "/settings/scoring/learn": REFUSES_NOT_FOUND,
    "/settings/scoring/suggestion/{cal_id}/{do}": REFUSES_NO_ROW,
    "/setup/action/{name}": REFUSES_NO_ROW,
    "/setup/mode": REFUSES_FORM,
    "/setup/profile": REFUSES_FORM,
    "/sources/{source_id}/cadence": REFUSES_FORM,
    "/sources/{source_id}/default-boards": REFUSES_NOT_FOUND,
    "/sources/{source_id}/remove": REFUSES_FORM,
    "/stories/{story_id}": REFUSES_NO_ROW,
    "/title-variants/{action}": REFUSES_NO_ROW,
}


class SweepRecorder:
    def __init__(self) -> None:
        self.route = ""
        self.confirms: list[tuple[str, bool]] = []
        self.violations: list[str] = []
        self.statuses: dict[str, list[int]] = {}

    def observe(self, event: str, **facts) -> None:
        if event == "confirm":
            self.confirms.append((self.route, bool(facts.get("ok"))))

    def wrap_require(self, real):
        rec = self

        def require(extra, corrections, ids):
            try:
                real(extra, corrections, ids)
            except Exception as exc:
                if "online_approval" in extra:
                    rec.violations.append(f"{rec.route}: staged online without confirmation "
                                          f"({type(exc).__name__})")
                raise
            if "online_approval" in extra and corrections.posted in ("", STALE_DIGEST):
                stamped = [c for c in rec.confirms if c[0] == rec.route and c[1]]
                if not stamped:
                    rec.violations.append(f"{rec.route}: staged online with no confirmation")

        return require


@pytest.fixture
def sweep(monkeypatch):
    from kestrel.learning import correction_lines
    from kestrel.queue import rescore as rescore_queue

    rec = SweepRecorder()
    monkeypatch.setattr(correction_lines, "_observe", rec.observe)
    monkeypatch.setattr(rescore_queue, "_require_confirmed",
                        rec.wrap_require(rescore_queue._require_confirmed))
    return rec


async def _online_tasks() -> dict[int, object]:
    """Scoring tasks (the ones that state correction lines) carrying an online approval -> the
    correction digest each one binds. Other online tasks (an organization summary, a deeper
    read) send no correction lines; their consent is the section 1 contract."""
    from kestrel.services.online_consent import CORRECTIONS_KEY

    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(Task).where(Task.type.in_(("t1", "t2"))))).scalars().all()
        return {t.id: (t.payload or {}).get(CORRECTIONS_KEY) for t in rows
                if "online_approval" in (t.payload or {})}


async def _world_intact(ids) -> bool:
    from kestrel.db.models import Opportunity, RequirementCorrection

    async with get_sessionmaker()() as s:
        opp = await s.get(Opportunity, ids["strong"])
        n = (await s.execute(select(func.count()).select_from(RequirementCorrection))).scalar()
        return opp is not None and bool(n)


SWEEP_PARAMS = {"tier": "tier2", "track": "employment"}


def _sweep_url(path: str, ids) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        return SWEEP_PARAMS.get(name) or str(ids.get(name, 0))

    return re.sub(r"\{(\w+)(?::[^}]*)?\}", sub, path)


def _sweep_form(ids, approval: str, digest: str | None) -> dict:
    form = {
        "online_approval": approval, "confirmed": "1", "model": "same", "model_choice": "same",
        "opp_ids": str(ids["strong"]), "tier0_anyway": "1", "track": "employment",
        "situation": "An invented situation.", "rescore": "1", "back": "today",
    }
    if digest is not None:
        form["corrections_digest"] = digest
    return form


def _run_sweep(c, ids, rec: SweepRecorder, routes: list[str]) -> tuple[list[str], list[str]]:
    """Post every route twice (no digest, stale digest). Returns (problems, routes that broke the
    seeded world). Routes whose names say they delete or reset run last."""
    from kestrel.services.online_consent import approval_for
    from kestrel.services.setting_overrides import load_settings_with_db

    async def stamp() -> tuple[str, dict]:
        import json as _json

        from kestrel.services.online_consent import online_route

        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            route = await online_route(s, settings, "tier2")
        named = {"model": str(route.get("model") or ""), "dest": str(route.get("dest") or "")}
        return _json.dumps(approval_for(settings, ("tier1", "tier2")),
                           separators=(",", ":")), named

    approval, named = asyncio.run(stamp())
    assert approval not in ("", "{}"), "the sweep world must have an online scoring route"
    problems: list[str] = []
    broke: list[str] = []
    for path in sorted(routes, key=lambda p: (bool(NOT_A_SEND.search(p)), p)):
        if path in SWEEP_EXEMPT:
            continue
        rec.route = path
        # Two posts: no digest with Same model, and a stale digest naming the online model and
        # its destination, so a route that checks either before the lines is still reached.
        for digest, extra in ((None, {}), (STALE_DIGEST, named)):
            before = asyncio.run(_online_tasks())
            r = c.post(_sweep_url(path, ids),
                       data={**_sweep_form(ids, approval, digest), **extra},
                       headers={"HX-Request": "true"})
            rec.statuses.setdefault(path, []).append(r.status_code)
            after = asyncio.run(_online_tasks())
            # No digest reads as "no lines": a role with no lines may be queued, binding "".
            # Anything that binds lines here binds lines no panel showed; a stale digest never
            # matches, so it queues nothing online at all.
            bad = {i: d for i, d in after.items() if i not in before and (
                digest is not None or d != "")}
            if bad:
                problems.append(f"{path} (digest {digest!r}): queued online tasks {bad}")
        if not asyncio.run(_world_intact(ids)):
            broke.append(path)
            break
    return problems + rec.violations + _status_problems(rec.statuses, routes), broke


def _status_problems(statuses: dict[str, list[int]], routes: list[str]) -> list[str]:
    """A route that errors on the sweep form never reached the lines check: list it, unless an
    exemption names it; and an exemption whose route no longer errors is stale."""
    out = []
    for path, got in sorted(statuses.items()):
        if any(s >= 500 for s in got):
            out.append(f"{path}: server error {got} (never exempt)")
        elif any(s >= 400 for s in got) and path not in SWEEP_REFUSES:
            out.append(f"{path}: refused the sweep form {got}; it never reached the lines "
                       "check. Make it reachable, or add it to SWEEP_REFUSES with the reason.")
    for path in sorted(SWEEP_REFUSES):
        if path in routes and path in statuses and not any(s >= 400 for s in statuses[path]):
            out.append(f"{path}: in SWEEP_REFUSES but answered {statuses[path]}; remove it")
    return out


def _mutating_routes(app) -> list[str]:
    return sorted({p for p, r in _walk(app.router.routes)
                   if getattr(r, "methods", None) and {"POST", "PUT", "PATCH"} & r.methods})


# Sweeps every write route: 37-44 s alone on the owner's Windows PC against the 60 s default,
# and a timeout under the full gate's -n 8 load stopped the worker there. The sweep's size,
# not a wait, is the cost (profile in the PR notes); a smaller sweep is filed for 0.7.8.
@pytest.mark.timeout(240)
def test_no_route_queues_an_online_score_without_the_confirmed_lines(recorder, sweep,
                                                                     monkeypatch):
    """Issue #178 round 2, findings 1 and 2: every mutating route, posted with a valid approval
    stamp and no or a stale corrections digest while a correction exists, queues no online task
    and stages nothing; and the routes that tried to confirm lines are exactly LINES_PANELS."""
    ids = _lines_world(monkeypatch)
    # Every id-taking route points at the role that has a correction line.
    ids.update(opp_id=ids["strong"], opportunity_id=ids["strong"],
               quarantine_id=ids["lines_quarantine"])
    app = _build_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        problems, broke = _run_sweep(c, ids, sweep, _mutating_routes(app))
    assert not broke, (
        f"{broke[0]} removed the seeded role or correction, so later routes were not exercised. "
        "Add it to SWEEP_EXEMPT with the reason, or reseed after it."
    )
    assert not problems, "\n".join(problems)
    gone = sorted(set(SWEEP_REFUSES) - set(_mutating_routes(app)))
    assert not gone, f"SWEEP_REFUSES names routes that no longer exist: {gone}"
    found = {route for route, _ok in sweep.confirms}
    missing = sorted(found - set(LINES_PANELS))
    stale = sorted(set(LINES_PANELS) - found)
    assert not missing, (
        f"routes that confirm correction lines (scoring sends) with no driver in LINES_PANELS: "
        f"{missing}. Their panel must list the lines and post corrections_digest."
    )
    assert not stale, f"LINES_PANELS entries no route reached when posted: {stale}"
    # `recorder` only keeps the network closed here: the sweep also posts `confirmed=1` to
    # consented actions that are not scoring (research, summaries), which may try a model.


def _rogue_app(ids, body):
    """The app plus one route the sweep has never seen, calling `body(session)`."""
    from kestrel.web.deps import sessionmaker

    app = _build_app()

    async def rogue():
        async with sessionmaker()() as s:
            await body(s)
            await s.commit()
        return {"ok": True}

    app.post("/rogue/send")(rogue)
    return app


async def _approval_json() -> dict:
    from kestrel.services.online_consent import approval_for
    from kestrel.services.setting_overrides import load_settings_with_db

    async with get_sessionmaker()() as s:
        return approval_for(await load_settings_with_db(s), ("tier1", "tier2"))


def test_the_sweep_catches_a_route_that_queues_through_a_function_local_import(
    recorder, sweep, monkeypatch
):
    """The red case for the discovery: a new route, invisible to any source scan of the route
    table (its import is local to the function), stages an online task with no confirmation."""
    ids = _lines_world(monkeypatch)
    approval = asyncio.run(_approval_json())

    async def body(s):
        from kestrel.queue.rescore import enqueue_rescore  # function local, on purpose

        await enqueue_rescore(s, [ids["strong"]], online_approval=approval)

    app = _rogue_app(ids, body)
    with TestClient(app, raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        problems, _broke = _run_sweep(c, ids, sweep, ["/rogue/send"])
    assert problems and "staged online without confirmation" in problems[0], problems


def _status_app(handler):
    """The app plus one route the sweep has never seen, answering with `handler()`."""
    app = _build_app()
    app.post("/rogue/send")(handler)
    return app


@pytest.mark.parametrize(("kind", "expect"), [
    ("refused", "refused the sweep form"),
    ("crashes", "server error"),
])
def test_the_sweep_reports_a_route_that_errors_before_the_lines_check(
    kind, expect, recorder, sweep, monkeypatch
):
    """Round 4 finding 1: a route that answers 4xx or 5xx to the sweep form never reaches
    confirm_corrections, so it must be reported, not pass unseen."""
    from fastapi import HTTPException

    ids = _lines_world(monkeypatch)

    async def rogue():
        if kind == "refused":
            raise HTTPException(status_code=422, detail="needs a field the sweep lacks")
        raise RuntimeError("an invented crash")

    with TestClient(_status_app(rogue), raise_server_exceptions=False) as c:
        problems, _broke = _run_sweep(c, ids, sweep, ["/rogue/send"])
    assert any("/rogue/send" in p and expect in p for p in problems), problems


def test_an_exemption_whose_route_no_longer_refuses_is_reported(recorder, sweep, monkeypatch):
    """The exemption list's own red case: an entry for a route that now answers 200 is stale."""
    ids = _lines_world(monkeypatch)
    monkeypatch.setitem(SWEEP_REFUSES, "/rogue/send", REFUSES_FORM)

    async def fine():
        return {"ok": True}

    with TestClient(_status_app(fine), raise_server_exceptions=False) as c:
        problems, _broke = _run_sweep(c, ids, sweep, ["/rogue/send"])
    assert any("/rogue/send" in p and "remove it" in p for p in problems), problems


def test_an_exempted_route_that_errors_is_not_reported(recorder, sweep, monkeypatch):
    """The control for the two tests above: an exempt refusal is allowed."""
    from fastapi import HTTPException

    ids = _lines_world(monkeypatch)
    monkeypatch.setitem(SWEEP_REFUSES, "/rogue/send", REFUSES_FORM)

    async def refused():
        raise HTTPException(status_code=400, detail="needs a field the sweep lacks")

    with TestClient(_status_app(refused), raise_server_exceptions=False) as c:
        problems, _broke = _run_sweep(c, ids, sweep, ["/rogue/send"])
    assert problems == [], problems


def test_the_sweep_catches_a_route_that_confirms_one_role_and_queues_another(
    recorder, sweep, monkeypatch
):
    ids = _lines_world(monkeypatch)
    approval = asyncio.run(_approval_json())

    async def body(s):
        from kestrel.learning.correction_lines import confirm_corrections, dialog_digest
        from kestrel.queue.rescore import enqueue_rescore

        other = await confirm_corrections(s, [ids["second"]],
                                          await dialog_digest(s, [ids["second"]]))
        await enqueue_rescore(s, [ids["strong"]], online_approval=approval, corrections=other)

    app = _rogue_app(ids, body)
    with TestClient(app, raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        problems, _broke = _run_sweep(c, ids, sweep, ["/rogue/send"])
    assert problems and "staged online without confirmation" in problems[0], problems


def _forgeries(real):
    """Every way round 3's review built a confirmation without confirm_corrections."""
    import copy
    import dataclasses
    import pickle

    from kestrel.learning import correction_lines as cl

    def by_hand():
        o = object.__new__(cl.ConfirmedCorrections)
        for name, value in (("digests", ((1, "forged"),)), ("posted", ""), ("_seal", cl._SEAL)):
            object.__setattr__(o, name, value)
        return o

    def edited():
        object.__setattr__(real, "digests", ((1, "forged"),))
        return real

    return {
        "constructor": lambda: cl.ConfirmedCorrections(((1, ""),), "", object()),
        "constructor with the seal": lambda: cl.ConfirmedCorrections(((1, ""),), "", cl._SEAL),
        "dataclasses.replace": lambda: dataclasses.replace(real, digests=((1, "forged"),)),
        "copy": lambda: copy.copy(real),
        "deepcopy": lambda: copy.deepcopy(real),
        "built by hand": by_hand,
        "pickle": lambda: pickle.dumps(real),  # raises: nothing to load
        "edited in place": edited,  # last: it spoils `real`
    }


def test_the_registry_matches_the_issued_object_not_an_equal_one():
    """Round 4 finding 2: a value with fields equal to an issued one, made while the issued one
    is alive, is refused. The registry keys on identity (the class keeps object's __eq__ and
    __hash__)."""
    from kestrel.learning import correction_lines as cl

    real = cl.ConfirmedCorrections(((1, "a"),), "", cl._SEAL)
    cl._ISSUED[real] = real.digests
    twin = cl.ConfirmedCorrections(((1, "a"),), "", cl._SEAL)
    assert cl.issued_digests(real) == ((1, "a"),)
    with pytest.raises(cl.CorrectionsNotConfirmed):
        cl.issued_digests(twin)


def test_a_confirmation_cannot_be_copied_or_pickled():
    """Refused at once, before the registry is asked."""
    import copy
    import pickle

    from kestrel.learning import correction_lines as cl

    real = cl.ConfirmedCorrections(((1, "a"),), "", cl._SEAL)
    for make in (lambda: copy.copy(real), lambda: copy.deepcopy(real),
                 lambda: pickle.dumps(real)):
        with pytest.raises(cl.CorrectionsNotConfirmed):
            make()


def test_a_confirmation_cannot_be_forged(monkeypatch):
    """Round 3 finding 2: only the very object confirm_corrections issued, with the digests it was
    issued with, is accepted by the queue."""
    from kestrel.learning import correction_lines as cl
    from kestrel.queue.rescore import _require_confirmed

    real = cl.ConfirmedCorrections(((1, "a"),), "", cl._SEAL)
    cl._ISSUED[real] = real.digests  # as confirm_corrections issues it
    _require_confirmed({"online_approval": {"tier2": ["x"]}}, real, [1])  # the control
    for name, make in _forgeries(real).items():
        try:
            fake = make()
        except cl.CorrectionsNotConfirmed:
            continue  # refused while being made
        with pytest.raises(cl.CorrectionsNotConfirmed):
            _require_confirmed({"online_approval": {"tier2": ["x"]}}, fake, [1])
        assert name  # each forgery reached the queue's check and was refused there


def _offline_catalog(monkeypatch) -> None:
    """The online model list, as the provider would answer it, without the network."""
    from kestrel.llm.providers import ModelInfo
    from kestrel.services.hosted_models import PickerModel

    raw = [ModelInfo(id=MODEL, name="Contract Model", context_length=131072,
                     prompt_usd_per_m=1.0, completion_usd_per_m=2.0)]
    picker = [PickerModel(id=MODEL, name="Contract Model", context_length=131072, free=False,
                          price="x")]

    async def hosted(_settings):
        return ("OpenRouter", "frontier", raw, picker, {}, None)

    monkeypatch.setattr("kestrel.web.pages.needs_try._hosted_models", hosted)
    monkeypatch.setattr("kestrel.web.pages.rescore._hosted_models", hosted)

    from kestrel.services import hosted_balance

    async def no_balance(backend):  # the credit lookup would ask the provider
        return hosted_balance.Reading(False)

    monkeypatch.setattr(hosted_balance, "_fetch", no_balance)


def _lines_world(monkeypatch) -> dict[str, int]:
    """The contract world, a failure row for the strong role, the profile notice asking for
    a score again, and an online model list read from no network."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")
    ids = asyncio.run(_world())
    _prep_score_again(ids, monkeypatch)
    _offline_catalog(monkeypatch)

    async def go() -> int:
        async with get_sessionmaker()() as s:
            q = Quarantine(opportunity_id=ids["strong"], tier=2, model="m", raw_response="x",
                           error="invalid", attempts=1)
            s.add(q)
            await s.commit()
            return q.id

    ids["lines_quarantine"] = asyncio.run(go())
    return ids


def _strong_tasks(opp: int) -> list[Task]:
    async def go():
        async with get_sessionmaker()() as s:
            rows = (await s.execute(select(Task).where(Task.type.in_(("t1", "t2"))))).scalars()
            return [t for t in rows if (t.payload or {}).get("opportunity_id") == opp]

    return asyncio.run(go())


def _sent_lines(sent: str) -> list[str]:
    """The correction lines in a prompt that was sent: the block under the header, to the first
    blank line."""
    from kestrel.pipeline.context import OPERATOR_CORRECTIONS_HEADER

    at = sent.find(OPERATOR_CORRECTIONS_HEADER)
    if at < 0:
        return []
    out = []
    for line in sent[at + len(OPERATOR_CORRECTIONS_HEADER):].lstrip("\n").split("\n"):
        if not line.strip():
            break
        out.append(line)
    return out


def _drain_scoring(client_settings=None) -> list[str]:
    """The real worker drains Tier 1, then Tier 2, against a recording fake model; returns every
    prompt Tier 2 was sent."""
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.services.setting_overrides import load_settings_with_db
    from tests.pipeline_fakes import FakeLLM

    class Recording(FakeLLM):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.sent: list[str] = []

        async def complete(self, **kw):
            self.sent.append("\n".join(m.get("content", "") for m in kw["messages"]))
            return await super().complete(**kw)

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            settings = await load_settings_with_db(s)
        client = Recording(client_settings or settings)
        worker = Worker(sm, client, settings)
        await worker.drain_tier("t1", 1, handle_t1, TierStats())
        before = len(client.sent)
        await worker.drain_tier("t2", 1, handle_t2, TierStats())
        return client.sent[before:]

    return asyncio.run(go())


@pytest.mark.parametrize("path", sorted(LINES_PANELS))
def test_a_scoring_panel_lists_exactly_the_lines_its_send_carries(path, recorder, monkeypatch):
    """Seed a correction, open the panel, confirm: the lines it listed are the lines bound to
    the queued task and the lines Tier 2 then sends, in order, nothing more."""
    from kestrel.learning.correction_lines import lines_digest
    from kestrel.services.online_consent import CORRECTIONS_KEY

    ids = _lines_world(monkeypatch)
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        listed, url, data = LINES_PANELS[path](c, ids)
        assert any(LINE_A in x for x in listed), f"{path}: the panel lists {listed!r}"
        r = c.post(url, data=data, headers={"HX-Request": "true"})
        assert r.status_code < 400, f"{path}: {r.status_code} {r.text[:300]!r}"
    tasks = _strong_tasks(ids["strong"])
    assert tasks, f"{path}: the confirmed send queued nothing: {r.text[:300]!r} {r.headers}"
    bound = {(t.payload or {}).get(CORRECTIONS_KEY) for t in tasks}
    assert bound == {lines_digest(listed)}, f"{path}: bound {bound}, listed {listed!r}"
    sent = _drain_scoring()
    assert sent, f"{path}: Tier 2 sent nothing"
    assert _sent_lines(sent[-1]) == listed, f"{path}: sent {_sent_lines(sent[-1])!r}"


@pytest.mark.parametrize("path", sorted(LINES_PANELS))
def test_a_correction_changed_while_a_scoring_panel_is_open_sends_nothing(
    path, recorder, monkeypatch
):
    ids = _lines_world(monkeypatch)
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        listed, url, data = LINES_PANELS[path](c, ids)
        assert any(LINE_A in x for x in listed), f"{path}: the panel lists {listed!r}"
        _correct(c, ids["strong"], LINE_B, reason="overstated")  # in another window
        before = asyncio.run(_count_tasks())
        r = c.post(url, data=data, headers={"HX-Request": "true"})
    where = f"{path} -> {r.status_code}: {r.text[:300]!r} {dict(r.headers)}"
    assert _shows_refusal(r), f"the changed send was not refused in words: {where}"
    assert recorder.llm == [], f"a model was called: {where}"
    assert recorder.sent() == [], f"a network request was made: {where}"
    assert asyncio.run(_count_tasks()) == before, f"a task was queued: {where}"
    assert _strong_tasks(ids["strong"]) == [], f"a scoring task exists: {where}"


def test_a_panel_with_no_corrections_lists_nothing(recorder, monkeypatch):
    """With no corrections, the smaller panels look as they did: no lines block."""
    ids = _lines_world(monkeypatch)
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        for path in ("/api/profile/score-again", "/quarantine/{quarantine_id}/model",
                     "/opportunities/{opp_id}/role/story"):
            listed, _url, data = LINES_PANELS[path](c, ids)
            assert listed == [], f"{path}: {listed!r}"
            assert data.get("corrections_digest") == "", f"{path}: {data!r}"


# Finding 1 (round 2): Keep it anyway and Score it anyway skip the hand-off when a post carries
# an approval stamp. Such a post with no digest must stage nothing, while a correction exists.
@pytest.mark.parametrize("path", [
    "/opportunities/{opp_id}/decide/keep-anyway",
    "/quarantine/{quarantine_id}/anyway",
])
def test_an_approval_stamp_without_the_lines_queues_nothing(path, recorder, monkeypatch):
    import json as _json

    ids = _lines_world(monkeypatch)
    ids.update(opp_id=ids["strong"], quarantine_id=ids["lines_quarantine"])
    approval = _json.dumps(asyncio.run(_approval_json()), separators=(",", ":"))
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        r = c.post(_fill(path, ids), data={"online_approval": approval, "confirmed": "1"},
                   headers={"HX-Request": "true"})
    where = f"{path} -> {r.status_code}: {r.text[:300]!r} {dict(r.headers)}"
    assert _strong_tasks(ids["strong"]) == [], f"a scoring task was staged: {where}"
    assert _shows_refusal(r), f"not refused in words: {where}"
    assert recorder.llm == [] and recorder.sent() == []


# Finding 3 (round 2): a correction saved between the confirmation and the enqueue. The task binds
# what was confirmed, so Tier 2 holds it and the changed line is never sent.
@pytest.mark.parametrize("path", sorted(LINES_PANELS))
def test_a_correction_saved_during_the_confirmation_is_never_sent(path, recorder, monkeypatch):
    import sqlite3

    from kestrel.learning import correction_lines
    from kestrel.services.online_consent import APPROVAL_HOLD

    ids = _lines_world(monkeypatch)
    fired: list[bool] = []

    def observe(event, **facts):
        if event == "confirm" and facts.get("ok") and not fired:
            fired.append(True)
            # Another window saves a change after the lines were read and before the queue write.
            con = sqlite3.connect(paths.db_path(), timeout=5)
            try:
                con.execute("UPDATE requirement_correction SET note = ?", (LINE_B,))
                con.commit()
            finally:
                con.close()

    monkeypatch.setattr(correction_lines, "_observe", observe)
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], LINE_A)
        listed, url, data = LINES_PANELS[path](c, ids)
        assert any(LINE_A in x for x in listed), listed
        c.post(url, data=data, headers={"HX-Request": "true"})
    assert fired, f"{path}: the confirmation never ran"
    sent = _drain_scoring()
    assert not any(LINE_B in s for s in sent), f"{path}: the unseen change was sent"
    assert not any(LINE_A in s for s in sent), f"{path}: stale lines were sent as current"
    # Tier 1 states no correction lines and may run; the Tier 2 task waits for a new OK.
    tasks = _strong_tasks(ids["strong"])
    shown = [(t.type, t.status, t.last_error) for t in tasks]
    t2 = [t for t in tasks if t.type == "t2"]
    assert t2 and all(t.status == "pending" and t.last_error == APPROVAL_HOLD for t in t2), (
        f"{path}: {shown}")
    assert all(t.status in ("done", "pending") for t in tasks), f"{path}: {shown}"


# Finding 4 (round 2, D-PRIV-673): confidential mode. The panel shows the lines as they are sent:
# the employer's names hidden by its "Show it as" text, where the send carries a one-time code.
EMPLOYER = "Invented Employer Group"
SHOWN_AS = "a company in logistics"


def _confidential(monkeypatch) -> None:
    from kestrel.privacy import Employer, EmployerMap

    mapping = EmployerMap([Employer(EMPLOYER, SHOWN_AS)])

    async def mapped(_session, **_kwargs):
        return mapping

    monkeypatch.setattr("kestrel.docs.current_employer.load_employer_map", mapped)

    async def on() -> None:
        async with get_sessionmaker()() as s:
            await s.merge(Setting(key="confidential_mode", value=True))
            await s.commit()

    asyncio.run(on())


def _set_confidential(value: bool) -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            await s.merge(Setting(key="confidential_mode", value=value))
            await s.commit()

    asyncio.run(go())


def test_confidential_mode_lists_and_sends_the_lines_with_the_employer_hidden(
    recorder, monkeypatch
):
    from kestrel.privacy import _CODE_CHARS, _CODE_LEN

    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    note = f"I ran the night shift rota at {EMPLOYER} for two invented winters."
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], note)
        listed, url, data = _drive_rescore(c, ids)
        assert listed and all(EMPLOYER not in x for x in listed), listed
        assert any(SHOWN_AS in x for x in listed), listed
        r = c.post(url, data=data, headers={"HX-Request": "true"})
        assert "Queued 1 role" in r.text, r.text[:300]
    sent = _drain_scoring()
    assert sent and EMPLOYER not in sent[-1]
    got = _sent_lines(sent[-1])
    code = f"[{_CODE_CHARS}]{{{_CODE_LEN}}}"
    assert len(got) == len(listed)
    for shown, line in zip(listed, got, strict=True):
        pattern = "".join(code if part == SHOWN_AS else re.escape(part)
                          for part in re.split(f"({re.escape(SHOWN_AS)})", shown))
        assert re.fullmatch(pattern, line), (shown, line)


def test_turning_confidential_mode_off_after_the_confirmation_sends_nothing(
    recorder, monkeypatch
):
    from kestrel.services.online_consent import APPROVAL_HOLD

    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    note = f"I ran the night shift rota at {EMPLOYER} for two invented winters."
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], note)
        _listed, url, data = _drive_rescore(c, ids)
        r = c.post(url, data=data, headers={"HX-Request": "true"})
        assert "Queued 1 role" in r.text, r.text[:300]
    _set_confidential(False)  # the panel showed the employer hidden; the send would name it
    sent = _drain_scoring()
    assert not any(EMPLOYER in s for s in sent), "the employer was sent by name"
    assert sent == []
    tasks = _strong_tasks(ids["strong"])
    assert tasks and all(t.last_error == APPROVAL_HOLD for t in tasks)


def test_the_panel_and_the_send_hide_the_employer_by_one_rule_when_only_the_pick_is_online(
    recorder, monkeypatch
):
    """Round 3 finding 3: the configured full review is on this computer and only the one-time
    pick in Failed scoring goes online. The panel's hiding and Tier 2's come from one function,
    so the confirmed send goes out, hidden, instead of being held for ever."""
    ids = _lines_world(monkeypatch)

    async def local_tier2() -> None:
        async with get_sessionmaker()() as s:
            await s.merge(Setting(key="llm.tiers.tier2.backend", value="ollama"))
            await s.merge(Setting(key="llm.tiers.tier2.model", value="local-model:latest"))
            await s.commit()

    asyncio.run(local_tier2())
    _confidential(monkeypatch)
    note = f"I ran the night shift rota at {EMPLOYER} for two invented winters."
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], note)
        listed, url, data = _drive_quarantine_model(c, ids)
        assert listed and all(EMPLOYER not in x and SHOWN_AS in x for x in listed), listed
        c.post(url, data=data, headers={"HX-Request": "true"})
    sent = _drain_scoring()
    assert sent, [(t.type, t.status, t.last_error) for t in _strong_tasks(ids["strong"])]
    assert EMPLOYER not in sent[-1]
    assert len(_sent_lines(sent[-1])) == len(listed)


def test_a_confirmed_send_from_an_online_client_is_sent_hidden(recorder, monkeypatch):
    """Round 3 finding 3 and issue #218: the panel showed the employer hidden, and at send time
    only the client goes online (the task's held settings keep the tier local). The plan is built
    from the client that sends (D-PRIV-679), so the send hides the employer as the panel showed,
    and the confirmation matches: it goes out, hidden, and the name never leaves."""
    from kestrel.privacy import _CODE_CHARS, _CODE_LEN
    from kestrel.services.setting_overrides import load_settings_with_db

    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)

    async def online_settings():
        async with get_sessionmaker()() as s:
            return await load_settings_with_db(s)

    online = asyncio.run(online_settings())
    note = f"I ran the night shift rota at {EMPLOYER} for two invented winters."
    with TestClient(_build_app(), raise_server_exceptions=False) as c:
        _correct(c, ids["strong"], note)
        listed, url, data = _drive_rescore(c, ids)
        assert all(EMPLOYER not in x for x in listed), listed
        r = c.post(url, data=data, headers={"HX-Request": "true"})
        assert "Queued 1 role" in r.text, r.text[:300]

    async def local_tier2() -> None:
        async with get_sessionmaker()() as s:
            await s.merge(Setting(key="llm.tiers.tier2.backend", value="ollama"))
            await s.merge(Setting(key="llm.tiers.tier2.model", value="local-model:latest"))
            await s.commit()

    asyncio.run(local_tier2())
    sent = _drain_scoring(client_settings=online)  # the client still points online
    assert sent, [(t.type, t.status, t.last_error) for t in _strong_tasks(ids["strong"])]
    assert not any(EMPLOYER in s for s in sent), "the employer was sent by name"
    got = _sent_lines(sent[-1])
    code = f"[{_CODE_CHARS}]{{{_CODE_LEN}}}"
    assert len(got) == len(listed)
    for shown, line in zip(listed, got, strict=True):
        pattern = "".join(code if part == SHOWN_AS else re.escape(part)
                          for part in re.split(f"({re.escape(SHOWN_AS)})", shown))
        assert re.fullmatch(pattern, line), (shown, line)

# ------------------------------------------------------------------------ 4. system changes
# Issue #181: a route that changes the person's own computer (Ollama's environment variables,
# a restart of Ollama) does so only on its exact button. Discovered structurally: every route
# whose handler's module holds the ollama_env service. Every GET must change nothing, and every
# POST must change nothing when it arrives without the digest of what was on screen (a forged
# or replayed post, including one that says confirmed=1), and must be local-only.
def _system_change_routes(route_table) -> list[tuple[str, object]]:
    from kestrel.services import ollama_env

    return [(p, r) for p, r in route_table
            if any(v is ollama_env for v in getattr(r.endpoint, "__globals__", {}).values())]


class _SystemRecorder:
    """Every write and restart a route asks for, in place of the real computer."""

    def __init__(self):
        from kestrel.services import ollama_env

        class _Be(ollama_env.Backend):
            def owned(inner):
                return dict.fromkeys(ollama_env.NAMES)

            def write(inner, values):
                self.calls.append(("write", dict(values)))

            def commands(inner, values):
                return []

            def restart(inner):
                self.calls.append(("restart", None))

        self.calls: list = []
        self.backend = _Be(mechanism="contract", where="here", can_restart=True)


def test_the_system_change_routes_are_discovered(route_table):
    found = {(sorted(r.methods)[0], p) for p, r in _system_change_routes(route_table)}
    assert {("GET", "/setup/ollama-env"), ("POST", "/setup/ollama-env/apply"),
            ("POST", "/setup/ollama-env/undo"), ("POST", "/setup/ollama-env/restart")} <= found


def _system_client(monkeypatch) -> tuple[TestClient, _SystemRecorder]:
    from kestrel.db import migrate
    from kestrel.db.engine import reset_engine
    from kestrel.services import ollama_env

    rec = _SystemRecorder()
    monkeypatch.setattr(ollama_env, "_override", [rec.backend])
    monkeypatch.setattr("kestrel.llm.profiles.ollama_needed", lambda s: True)
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(_build_app()), rec


def _system_routes(route_table, method: str) -> list[str]:
    found = [p for p, r in _system_change_routes(route_table) if method in r.methods]
    assert found, f"no {method} system-change route found"
    return found


def test_no_system_change_from_a_get(route_table, monkeypatch):
    client, rec = _system_client(monkeypatch)
    for path in _system_routes(route_table, "GET"):
        assert client.get(path).status_code == 200
        assert client.get(path, params={"review": "1"}).status_code == 200
        assert rec.calls == [], f"GET {path} changed the system"


def test_every_system_change_post_is_local_only(route_table):
    from kestrel.web.auth import require_local

    posts = [(p, r) for p, r in _system_change_routes(route_table) if "POST" in r.methods]
    assert posts
    missing = [p for p, r in posts
               if not any(d.dependency is require_local for d in r.dependencies)]
    assert missing == [], f"these change this computer but are not local-only: {missing}"


@pytest.mark.parametrize("form", [{}, {"confirmed": "1"}, {"plan": "0" * 32, "undo": "0" * 32}])
def test_no_system_change_from_a_post_without_its_exact_button(route_table, monkeypatch, form):
    client, rec = _system_client(monkeypatch)
    for path in _system_routes(route_table, "POST"):
        client.post(path, data=form)
        assert rec.calls == [], f"POST {path} changed the system without its button"
