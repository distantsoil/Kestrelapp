"""Issue #218 (D-PRIV-800 to D-PRIV-802): the contract for every send that can reach an online
model under confidential mode.

Invariant: under confidential mode nothing leaves the machine with the current employer
unredacted, whatever mix of held run settings and live client the send uses. Each send's
redaction comes from the client that actually sends, decided in one function
(`kestrel.pipeline._shared.plan_hosted_send`).

Three parts, each found by scanning the code, so a send site added later fails here the day it
is added instead of in review:

1. Inventory. Every call in `kestrel/` that can end in the model client (`run_prompt_hosted`,
   `run_prompt`, `run_guarded`, `run_doc_select`, a client's own `complete`) is listed below,
   either as a site that goes through the shared wrapper `run_prompt_hosted` (which plans from
   the client it is handed) or as a direct site with its own driver here.
2. One owner. Every `plan_hosted_send` call names the sending client as `sender`, apart from a
   short list of disclosures and refusals shown before a client exists.
3. Behavior. Every direct site, and the wrapper on every tier, is driven with a real client
   whose send is captured at the client boundary, under each mix: held settings local with the
   client online, both online, and a retry. No payload holds the employer's name; with no
   employer chosen nothing is sent at all.

Threat model: these guards catch a future contributor's accident; test code is not a security
boundary (D-PROC-12). All text is invented. Nothing reaches the network.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from sqlalchemy import update

from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Opportunity, Task
from kestrel.llm.client import ChatResult, LLMClient
from kestrel.privacy import EmployerMap, contains_live_code
from tests.test_confidential_sender import POSTING, _db, _hold_local, _run_task
from tests.test_contracts import (
    EMPLOYER,
    MODEL,
    TIERS,
    _confidential,
    _lines_world,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "kestrel"

# ------------------------------------------------------------------------------ 1. inventory
SEND_CALLS = ("run_prompt_hosted", "run_prompt", "run_guarded", "run_doc_select")

# The plumbing every send passes through; not send sites themselves.
INTERNAL = {
    "kestrel/llm/guard.py::run_guarded",  # the attempt and its one repair: the client boundary
    "kestrel/llm/guard.py::run_prompt",
    "kestrel/llm/guard.py::run_doc_select",
    "kestrel/pipeline/_shared.py::run_prompt_hosted",  # the shared wrapper itself
}

# Sites that hand their client to the shared wrapper, which plans from that client and encodes
# the messages before the send. Covered by `test_the_wrapper_*` on every tier.
WRAPPED = {
    "kestrel/analytics/summary.py::honest_summary.run",
    "kestrel/docs/imports.py::suggest_variants",
    "kestrel/docs/letters.py::_run",
    "kestrel/ingest/triggers.py::classify_and_store",
    "kestrel/interview/mock.py::submit_answer.run",
    "kestrel/interview/question_task.py::generate_question_set.run",
    "kestrel/interview/stories.py::_merge_cluster.run",
    "kestrel/negotiation/scripts.py::draft_scripts.run",
    "kestrel/research/company.py::research_org",
    "kestrel/research/contacts.py::discover_contacts",
    "kestrel/research/org_summary.py::summarize.run",
    "kestrel/services/follow_up.py::_write.run",
    "kestrel/services/title_suggest.py::generate",
}

# Sites that call the guard themselves: each plans through `plan_hosted_send` from its client,
# and each has a driver below (the value is the driver's name).
DIRECT = {
    "kestrel/pipeline/tier1.py::handle_t1": "tier1",
    "kestrel/pipeline/tier2.py::handle_t2": "tier2",
    "kestrel/pipeline/tier3.py::transmit_escalation": "tier3",
    "kestrel/docs/assess.py::assess_master": "assess",
    "kestrel/docs/select.py::select_config": "select",
    "kestrel/docs/imports.py::_ask": "import",
    "kestrel/bench/check.py::_quick_screen": "quick_screen",
    "kestrel/bench/harness.py::_run_item": "bench",
}

# `plan_hosted_send` calls whose sender is not a variable named `client`: a disclosure or a
# refusal made before a client exists, from the very settings the client is then built from, or
# the helper that forwards its own caller's client. Each has a reason.
SENDER_NOT_NAMED_CLIENT = {
    # The preview is built before the confirmation; the send re-derives it from the client's
    # own settings (transmit_escalation), and a different destination fails the token check.
    "kestrel/pipeline/tier3.py::build_escalation_preview",
    # A disclosure for the import panel; the send plans again from its client (propose_import).
    "kestrel/web/pages/documents.py::_import_confidential_send",
    # A refusal before a research job starts; its sends go through the wrapper.
    "kestrel/research/jobs.py::_approval_error",
    # Refusals before anything runs; each step plans again from its client.
    "kestrel/bench/check.py::_send_plan",
    "kestrel/cli.py::_bench",
}


class _Visit(ast.NodeVisitor):
    def __init__(self, rel: str, out: list[tuple[str, str, ast.Call]]) -> None:
        self.rel, self.out, self.stack = rel, out, []

    def visit_FunctionDef(self, node):  # noqa: N802
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Call(self, node):  # noqa: N802
        fn = node.func
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else "")
        if name == "complete" and isinstance(fn, ast.Attribute):
            # A task queue's `complete(task_id)` marks a task done; it sends nothing.
            name = "" if "queue" in ast.unparse(fn.value).lower() else "client.complete"
        self.out.append((f"{self.rel}::{'.'.join(self.stack) or '<module>'}", name, node))
        self.generic_visit(node)


def _calls() -> list[tuple[str, str, ast.Call]]:
    """(site, callee, call) for every call in the package; site is `path::enclosing.function`."""
    out: list[tuple[str, str, ast.Call]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        _Visit(rel, out).visit(ast.parse(path.read_text(encoding="utf-8")))
    return out


def _send_sites() -> dict[str, set[str]]:
    sites: dict[str, set[str]] = {}
    for site, callee, _call in _calls():
        if callee in SEND_CALLS or callee == "client.complete":
            sites.setdefault(site, set()).add(callee)
    return sites


def test_every_send_site_is_in_the_inventory():
    found = _send_sites()
    listed = INTERNAL | WRAPPED | set(DIRECT)
    new = sorted(set(found) - listed)
    assert not new, (
        f"send sites not in the inventory: {new}. A call that can reach the model client must "
        "plan through plan_hosted_send from its client; add it to WRAPPED or to DIRECT with a "
        "driver (issue #218)."
    )
    gone = sorted(listed - set(found))
    assert not gone, f"inventory rows with no send call any more: {gone}"


def test_a_wrapped_site_sends_only_through_the_wrapper_with_its_own_client():
    found = _send_sites()
    for site in sorted(WRAPPED):
        assert found[site] == {"run_prompt_hosted"}, (site, found[site])
    handed = {site: ast.unparse(call.args[1]) for site, callee, call in _calls()
              if callee == "run_prompt_hosted" and site in WRAPPED}
    assert handed == dict.fromkeys(WRAPPED, "client"), handed


def test_the_only_client_boundary_is_the_guard():
    boundary = {s for s, callees in _send_sites().items() if "client.complete" in callees}
    assert boundary == {"kestrel/llm/guard.py::run_guarded"}, boundary


def test_every_plan_names_the_sending_client():
    senders = [(site, {k.arg: ast.unparse(k.value) for k in call.keywords}.get("sender"))
               for site, callee, call in _calls() if callee == "plan_hosted_send"]
    assert senders, "no plan_hosted_send call was found"
    missing = [site for site, sender in senders if not sender]
    assert not missing, f"plan_hosted_send without sender: {missing}"
    wrong = [(site, sender) for site, sender in senders
             if sender != "client" and site not in SENDER_NOT_NAMED_CLIENT]
    assert not wrong, (
        f"the plan's sender is not the client that sends: {wrong}. Plan from the client, or "
        "add the site to SENDER_NOT_NAMED_CLIENT with its reason."
    )
    seen = {site for site, _sender in senders}
    assert SENDER_NOT_NAMED_CLIENT <= seen, sorted(SENDER_NOT_NAMED_CLIENT - seen)


def test_every_direct_site_plans_from_its_client():
    """Each direct site reaches `plan_hosted_send` (itself, or through the one function named
    here that plans for it) before it sends."""
    planner = {
        "kestrel/docs/imports.py::_ask": "kestrel/docs/imports.py::propose_import",
        "kestrel/pipeline/tier3.py::transmit_escalation":
            "kestrel/pipeline/tier3.py::build_escalation_preview",
        "kestrel/bench/harness.py::_run_item": "kestrel/bench/harness.py::_run_variant",
    }
    plans = {site for site, callee, _c in _calls()
             if callee in ("plan_hosted_send", "plan_client_send")}
    for site in DIRECT:
        assert planner.get(site, site) in plans, f"{site} sends without the shared plan"


# -------------------------------------------------------------------------------- 3. behavior
ALL_TIERS = (*TIERS, "frontier")
RESUME = f"""# Jordan Vale

## Experience

### {EMPLOYER}, Shift Lead
2021 - Present
- Ran the night shift at {EMPLOYER} across two depots.

### Harbor Lane Freight, Planner
2016 - 2021
- Planned 40 routes a week.
"""
GOLD = [{"track": "employment", "title": "Planner", "org": EMPLOYER,
         "description_md": f"Plan the routes at {EMPLOYER} for a busy season.",
         "location": "Remote, US", "comp_min": 1, "comp_max": 2, "remote_type": "remote",
         "gold_verdict": "consider", "gold_score": 3.0}]


class Boundary:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def named(self) -> list[str]:
        return [p for p in self.sent if EMPLOYER in p]

    def coded(self) -> bool:
        return any(contains_live_code(p) for p in self.sent)


@pytest.fixture
def boundary(monkeypatch) -> Boundary:
    """Every payload the real client is asked to send, captured where it would leave. The reply
    is an empty object, so the guard's own repair (a second send of the same text) runs too."""
    import httpx

    rec = Boundary()

    async def complete(self, *, tier, messages, **kw):
        rec.sent.append("\n".join(str(m.get("content") or "") for m in messages))
        return ChatResult(content="{}", model=MODEL)

    async def embed(self, texts, **kw):
        return [[0.0] * 8 for _ in texts]

    async def closed(self, request):
        raise httpx.ConnectError("test: the network is closed")

    monkeypatch.setattr(LLMClient, "complete", complete)
    monkeypatch.setattr(LLMClient, "embed", embed)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", closed)
    return rec


def _settings():
    from kestrel.services.setting_overrides import load_settings_with_db

    return _db(load_settings_with_db)


def _name_the_employer(ids) -> None:
    async def go(s):
        opp = await s.get(Opportunity, ids["strong"])
        if POSTING not in (opp.description_md or ""):
            opp.description_md = f"{opp.description_md or ''}\n\n{POSTING}"

    _db(go)


class Site:
    """One direct send site: `prepare` runs while the stored settings are online, `drive` makes
    the send with `client` (and the held settings where the site takes them), `rearm` puts the
    work back so a second drive is a retry."""

    def __init__(self, ids, monkeypatch, tmp_path) -> None:
        self.ids, self.monkeypatch, self.tmp_path = ids, monkeypatch, tmp_path

    def prepare(self) -> None:
        _name_the_employer(self.ids)

    def drive(self, client, held):
        raise NotImplementedError

    def rearm(self) -> None:
        return None


class _Scoring(Site):
    task_type = stage = ""

    def prepare(self) -> None:
        _run_task(self.task_type, self.ids["strong"], self.stage)

    def drive(self, client, held):
        from kestrel.pipeline.tier1 import handle_t1
        from kestrel.pipeline.tier2 import handle_t2
        from kestrel.pipeline.worker import TierStats, Worker

        handler = handle_t1 if self.task_type == "t1" else handle_t2

        async def go():
            worker = Worker(get_sessionmaker(), client, held)
            await worker.drain_tier(self.task_type, 1, handler, TierStats())

        asyncio.run(go())

    def rearm(self) -> None:
        async def go(s):
            opp = await s.get(Opportunity, self.ids["strong"])
            opp.stage = self.stage
            await s.execute(update(Task).where(Task.type == self.task_type).values(
                status="pending", attempts=0, last_error=None, lease_until=None,
                retry_at=None, worker_id=None))

        _db(go)


class Tier1(_Scoring):
    task_type, stage = "t1", "t0_pass"


class Tier2(_Scoring):
    task_type, stage = "t2", "t1_pass"


class Tier3(Site):
    def drive(self, client, held):
        from kestrel.pipeline import tier3

        async def go(s):
            preview = await tier3.build_escalation_preview(s, client.settings, self.ids["strong"])
            return await tier3.transmit_escalation(
                s, client, held, self.ids["strong"],
                confirmation_token=preview.confirmation_token,
            )

        return _db(go)

    def rearm(self) -> None:
        from kestrel.db.models import FrontierTransmission

        async def go(s):  # one confirmation is one send: the retry is a fresh confirmation
            for row in (await s.execute(FrontierTransmission.__table__.select())).all():
                await s.execute(FrontierTransmission.__table__.delete().where(
                    FrontierTransmission.id == row.id))

        _db(go)


class Assess(Site):
    def drive(self, client, held):
        from kestrel.docs import assess
        from kestrel.docs.catalog import load_catalog

        self.monkeypatch.setattr(
            assess, "document_text",
            lambda catalog, redact=False: f"Shift Lead at {EMPLOYER}. Ran the night shift.",
        )

        async def go(s):
            catalog = await load_catalog(s, self.ids["master"])
            return await assess.assess_master(client, catalog, session=s)

        return _db(go)


class Select(Site):
    def drive(self, client, held):
        from kestrel.docs import select as select_mod
        from kestrel.docs.catalog import load_catalog

        self.monkeypatch.setattr(
            select_mod, "_render_user",
            lambda spec, **kw: f"Pick the bullets for a role at {EMPLOYER}.",
        )

        async def go(s):
            catalog = await load_catalog(s, self.ids["master"])
            return await select_mod.select_config(
                s, client, catalog, opportunity_id=self.ids["strong"])

        return _db(go)


class Import(Site):
    def drive(self, client, held):
        from kestrel.docs.imports import propose_import

        async def go(s):
            return await propose_import(client, RESUME, source_format="md", session=s)

        return _db(go)


class _Gold(Site):
    def gold(self) -> Path:
        (self.tmp_path / "goldset.employment.jsonl").write_text(
            "\n".join(json.dumps(r) for r in GOLD) + "\n", encoding="utf-8")
        return self.tmp_path


class QuickScreen(_Gold):
    def drive(self, client, held):
        from kestrel.bench import check

        # The check builds its client from the candidate's settings; the mix is a client that
        # goes online whatever the settings the step was handed say.
        return asyncio.run(check._quick_screen(
            lambda _s: client, held, MODEL, None, self.gold()))


class Bench(_Gold):
    def drive(self, client, held):
        from kestrel.bench import harness

        return asyncio.run(harness.run_bench(
            settings=held, models=[MODEL], tracks=("employment",), gold_dir=self.gold(),
            make_client=lambda _s: client,
        ))


DRIVERS = {"tier1": Tier1, "tier2": Tier2, "tier3": Tier3, "assess": Assess, "select": Select,
           "import": Import, "quick_screen": QuickScreen, "bench": Bench}
MIXES = ("held_local_client_online", "held_online_client_online", "retry")


def test_every_direct_site_has_a_driver():
    assert set(DIRECT.values()) == set(DRIVERS)


def _mix(mix: str, site: Site):
    """(the online client that sends, the held settings) for a mix, after the site's set-up."""
    from kestrel.pipeline._shared import tier_is_hosted

    online = _settings()
    assert all(tier_is_hosted(online, t) for t in TIERS), "the world must be online"
    site.prepare()
    if mix != "held_online_client_online":
        _hold_local(ALL_TIERS)  # what the run holds keeps every tier on this computer
    held = _settings()
    local = not any(tier_is_hosted(held, t) for t in TIERS)
    assert local == (mix != "held_online_client_online"), (mix, local)
    return LLMClient(online), held


@pytest.mark.parametrize("mix", MIXES)
@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_no_direct_site_sends_the_employer(name, mix, boundary, monkeypatch, tmp_path):
    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    site = DRIVERS[name](ids, monkeypatch, tmp_path)
    client, held = _mix(mix, site)
    for attempt in range(2 if mix == "retry" else 1):
        before = len(boundary.sent)
        for _ in range(attempt):  # the second drive is the retry
            site.rearm()
        site.drive(client, held)
        assert len(boundary.sent) > before, (
            f"{name}: drive {attempt + 1} sent nothing, so this proves nothing")
    assert not boundary.named(), f"{name} ({mix}): the employer left by name"
    assert boundary.coded(), f"{name} ({mix}): no payload carried the employer's code"


def _wrapper_send(client, tier: str, *, session=True):
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import run_prompt_hosted

    spec = dataclasses.replace(load_prompt("variant_suggest/v1"), tier=tier)
    messages = [{"role": "system", "content": spec.system},
                {"role": "user", "content": f"Rewrite: ran the night shift at {EMPLOYER}."}]

    async def go(s):
        return await run_prompt_hosted(s if session else None, client, spec, messages,
                                       quarantine_tier=4)

    return _db(go)


@pytest.mark.parametrize("mix", MIXES)
@pytest.mark.parametrize("tier", TIERS)
def test_the_wrapper_never_sends_the_employer(tier, mix, boundary, monkeypatch):
    """Every wrapped site sends through `run_prompt_hosted` with its own client: on every tier
    and in every mix the payload carries the code, never the name."""
    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    online = _settings()
    if mix != "held_online_client_online":
        _hold_local(ALL_TIERS)
    client = LLMClient(online)
    for attempt in range(2 if mix == "retry" else 1):
        before = len(boundary.sent)
        _wrapper_send(client, tier)
        assert len(boundary.sent) > before, f"send {attempt + 1} did not reach the client"
    assert not boundary.named() and boundary.coded()


# ------------------------------------------------- confidential mode on, no employer chosen
def _no_employer(monkeypatch) -> None:
    _confidential(monkeypatch)

    async def none(_session, **_kwargs):
        return EmployerMap()

    monkeypatch.setattr("kestrel.docs.current_employer.load_employer_map", none)


@pytest.mark.parametrize("mix", MIXES[:2])
@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_with_no_employer_chosen_no_direct_site_sends(name, mix, boundary, monkeypatch,
                                                      tmp_path):
    ids = _lines_world(monkeypatch)
    _no_employer(monkeypatch)
    site = DRIVERS[name](ids, monkeypatch, tmp_path)
    client, held = _mix(mix, site)
    try:
        site.drive(client, held)
    except ValueError as exc:  # the model check and bench refuse a step by raising
        assert "hosted send refused" in str(exc)
    assert boundary.sent == [], f"{name} ({mix}): sent with no employer to hide"


@pytest.mark.parametrize("tier", TIERS)
def test_with_no_employer_chosen_the_wrapper_refuses_with_the_existing_message(
    tier, boundary, monkeypatch
):
    from kestrel.pipeline._shared import HOSTED_REFUSAL

    _lines_world(monkeypatch)
    _no_employer(monkeypatch)
    online = _settings()
    _hold_local(ALL_TIERS)
    result = _wrapper_send(LLMClient(online), tier)
    assert boundary.sent == []
    assert result.refused and result.error == HOSTED_REFUSAL


def test_with_no_employer_chosen_the_import_is_refused_unless_confirmed(boundary, monkeypatch):
    """The import's own bound confirmation (D-PRIV-1) is the one way a text goes as written with
    nothing marked: without it the import is refused with the existing message."""
    from kestrel.docs.imports import propose_import
    from kestrel.pipeline._shared import HOSTED_REFUSAL

    _lines_world(monkeypatch)
    _no_employer(monkeypatch)
    client = LLMClient(_settings())

    async def unconfirmed(s):
        return await propose_import(client, RESUME, source_format="md", session=s)

    proposal = _db(unconfirmed)
    assert proposal.value is None and proposal.error == HOSTED_REFUSAL
    assert boundary.sent == []

    async def confirmed(s):
        return await propose_import(client, RESUME, source_format="md", session=s,
                                    unmarked_confirmed=True)

    _db(confirmed)
    assert boundary.sent, "the confirmed import was not sent"


# ---------------------------------------------------------------- no session, nothing online
def test_without_a_session_an_online_client_sends_nothing(boundary, monkeypatch):
    """A caller with no database session cannot read confidential mode: a send that would go
    online is refused, never sent as written (D-PRIV-801)."""
    from kestrel.docs import assess
    from kestrel.docs.catalog import load_catalog
    from kestrel.docs.imports import propose_import, suggest_variants
    from kestrel.docs.letters import _run
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import NO_SESSION_REFUSAL

    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    client = LLMClient(_settings())
    catalog = _db(lambda s: load_catalog(s, ids["master"]))
    spec = load_prompt("variant_suggest/v1")
    messages = [{"role": "user", "content": f"Ran the night shift at {EMPLOYER}."}]

    async def go():
        assessed = await assess.assess_master(client, catalog, session=None)
        letter = await _run(client, spec, messages, session=None, on_quarantine=None)
        _found, variant_error = await suggest_variants(
            client, role_label=f"{EMPLOYER}, Shift Lead", text="Ran the night shift.",
            tags=[], session=None)
        imported = await propose_import(client, RESUME, source_format="md")
        return assessed.error, letter.error, variant_error, imported.error

    errors = asyncio.run(go())
    assert boundary.sent == []
    assert errors[0], "the assessment must report that it did not run"  # its own page sentence
    assert errors[1:] == (NO_SESSION_REFUSAL,) * 3


def test_without_a_session_a_local_client_still_runs(boundary, monkeypatch):
    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    local = _settings().model_copy(deep=True)
    local.llm.tiers["docsel"].backend = "local"
    _wrapper_send(LLMClient(local), "docsel", session=False)
    assert boundary.sent and boundary.named(), "a call on this computer is sent as written"


# ------------------------------------------------------------------ the command-line import
def test_the_import_command_sends_the_employer_as_a_code(boundary, monkeypatch, tmp_path):
    """`kestrel doc-import` built an online client and sent the resume as written: it had no
    plan at all (found by the #218 inventory)."""
    from kestrel import cli

    _lines_world(monkeypatch)
    _confidential(monkeypatch)
    source = tmp_path / "resume.md"
    source.write_text(RESUME, encoding="utf-8")
    asyncio.run(cli._doc_import(str(source), False, "employment", "resume"))
    assert boundary.sent, "nothing was sent, so this proves nothing"
    assert not boundary.named() and boundary.coded()


# ------------------------------------------------------- the deeper read hides it regardless
def test_the_deeper_read_hides_the_employer_whatever_the_redact_setting(boundary, monkeypatch,
                                                                       tmp_path):
    """The confirmed deeper read never depended on Hosted mode's redact_confidential setting:
    with it off on the client that sends, the employer still goes as a code."""
    ids = _lines_world(monkeypatch)
    _confidential(monkeypatch)
    site = Tier3(ids, monkeypatch, tmp_path)
    site.prepare()
    online = _settings().model_copy(deep=True)
    online.llm.hosted_mode.redact_confidential = False
    site.drive(LLMClient(online), online)
    assert boundary.sent and not boundary.named() and boundary.coded()
