"""D-PRIV-798: nothing goes to an online provider while online is not in use.

INVARIANT: nothing goes to an online provider while online is not in use (switched off, a stale
tick, or On this computer chosen), including inference for work that was queued and confirmed
earlier, and retries.

The provider reads already followed that rule (D-REL-41). The inference gates did not: they
asked only whether the switch was on and the tick current, so work confirmed for an online model
was still sent after the person chose On this computer. Every gate now asks one function,
`setup_state.online_send_held`. These tests stamp work while online is in use, change the state
the way the app does, then run the worker's own hold check and the real client against a
transport that records and refuses every request. Held work must stay queued, unspent, and run
again when the hold ends. Invented roles and keys; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import re
from importlib import resources

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from kestrel.config import ONLINE_CONSENT_HOW, HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting, Task
from kestrel.llm.client import ExternalBackendError, LLMClient
from kestrel.pipeline._shared import ModelOverrideError, settings_with_override, tier_is_hosted
from kestrel.queue.queue import DEFER_REASON_KEY, DEFERRED_KEY, REASON_APPROVAL, release_holds
from kestrel.services import setup_state
from kestrel.services.online_consent import (
    HOSTED_OFF,
    approval_for,
    approval_problem,
    destination_id,
    held_for_approval,
)
from kestrel.services.setting_overrides import load_settings_with_db
from tests import role_seed
from tests._crawl_support import stale_terms
from tests.pipeline_fakes import FakeLLM
from tests.test_settings_routes import _client

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/invented-model"
PICK = {"backend": "frontier", "model": "vendor/invented-pick", "json_mode": "schema",
        "context_length": 200000}
ASK = [{"role": "user", "content": "An invented question about an invented role."}]
IN_USE = {"enabled": True, "choice": "hosted", "mode": "hosted"}
# The three ways online is not in use, as the app's own controls leave the rows.
NOT_IN_USE = {
    "local": {"enabled": True, "choice": "local", "mode": "local"},
    "off": {"enabled": False, "choice": "hosted", "mode": "hosted"},
    "stale": {"enabled": True, "choice": "hosted", "mode": "hosted", "terms": "stale"},
}


async def _put(s, *, enabled=True, choice=None, mode=None, terms=None) -> None:
    """Online acknowledged, the scoring and drafting steps assigned an online model, the card
    rows as given."""
    rows = {
        "llm.hosted_mode.enabled": enabled,
        "llm.hosted_mode.acknowledged_at": "2026-10-01T00:00:00+00:00",
        "llm.hosted_mode.acknowledged_terms": (
            stale_terms() if terms == "stale" else HostedMode().disclosure_terms()
        ),
    }
    for tier in ("tier1", "tier2", "tier3", "trigger"):
        rows[f"llm.hosted_tiers.{tier}.backend"] = "frontier"
        rows[f"llm.hosted_tiers.{tier}.model"] = MODEL
    if choice is not None:
        rows["setup.run_choice"] = choice
    if mode is not None:
        rows["llm.mode"] = mode
    for key, value in rows.items():
        await s.merge(Setting(key=key, value=value))
    await s.commit()


async def _load(sm):
    async with sm() as s:
        return await load_settings_with_db(s)


@pytest.fixture
def wire(monkeypatch) -> list[httpx.Request]:
    """Every request that reaches a real transport is recorded and refused."""
    sent: list[httpx.Request] = []

    async def ahandle(self, request):
        sent.append(request)
        raise httpx.ConnectError("test: the network is closed")

    def handle(self, request):
        sent.append(request)
        raise httpx.ConnectError("test: the network is closed")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", ahandle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return sent


@pytest.fixture
def key(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "invented-key-not-real")


def _online(sent: list[httpx.Request]) -> list[str]:
    """The recorded requests that would have left this computer."""
    return [f"{r.method} {r.url}" for r in sent if r.url.host not in ("127.0.0.1", "localhost")]


class _Rec(FakeLLM):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[tuple[str, bool]] = []

    async def complete(self, **kw):
        tcfg = self.settings.llm.tiers[kw["tier"]]
        self.sent.append((tcfg.model, self.settings.llm.backends[tcfg.backend].external))
        return await super().complete(**kw)


# ------------------------------------------------- the review's probe: queued, then the choice
async def _probe(state: str | None):
    """Stamp a task while online is in use, change the state, then run the worker's hold check
    and the client the worker would use, exactly as a cycle already running does."""
    from kestrel.pipeline.worker import Worker

    sm = get_sessionmaker()
    async with sm() as s:
        await _put(s, **IN_USE)
    cycle = await _load(sm)
    assert setup_state.online_send_held(cycle) is None
    assert cycle.llm.backends[cycle.llm.tiers["tier2"].backend].external
    payload = {"opportunity_id": 1, "online_approval": approval_for(cycle, ("tier2",))}
    assert payload["online_approval"] == {"tier2": [destination_id(cycle, "tier2")]}
    worker = Worker(sm, LLMClient(cycle), cycle)
    if state is not None:
        async with sm() as s:
            await _put(s, **NOT_IN_USE[state])
    settings, client = await worker._task_settings("t2", payload)
    held = held_for_approval(settings, ("tier2",), payload) or held_for_approval(
        client.settings, ("tier2",), payload
    )
    error: BaseException | None = None
    try:
        await client.complete(tier="tier2", messages=ASK)
    except (ExternalBackendError, httpx.HTTPError, RuntimeError) as exc:
        error = exc
    return held, error, setup_state.online_send_held(settings)


def test_the_probe_can_fail_work_confirmed_while_online_is_in_use_is_sent(wire, key):
    with _client():
        held, error, why_not = asyncio.run(_probe(None))
    assert why_not is None and held is False
    assert not isinstance(error, ExternalBackendError)
    assert any(line.startswith("POST https://openrouter.ai/") for line in _online(wire)), wire


@pytest.mark.parametrize("state", sorted(NOT_IN_USE))
def test_queued_confirmed_online_work_sends_nothing_once_online_is_not_in_use(state, wire, key):
    with _client():
        held, error, why_not = asyncio.run(_probe(state))
    assert _online(wire) == [], state
    assert held is True, state
    assert isinstance(error, ExternalBackendError), (state, error)
    expected = setup_state.LOCAL_CHOSEN if state == "local" else setup_state.ONLINE_OFF
    assert why_not == expected, state


@pytest.mark.parametrize("state", sorted(NOT_IN_USE))
def test_a_one_run_online_pick_cannot_resolve_once_online_is_not_in_use(state, key):
    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, enabled=True, choice="both", mode="local")
        before = settings_with_override(await _load(sm), "tier2", PICK)
        async with sm() as s:
            await _put(s, **NOT_IN_USE[state])
        return before, await _load(sm)

    with _client():
        before, now = asyncio.run(go())
    assert before.llm.tiers["tier2"].model == PICK["model"]  # in use: the pick resolves
    with pytest.raises(ModelOverrideError):
        settings_with_override(now, "tier2", PICK)
    assert tier_is_hosted(now, "frontier") is False


# ----------------------------------------------- held work stays queued, unspent, and resumes
async def _hold(attempts: int, *, stamped: bool = True):
    """A hosted cycle is running; the person chooses On this computer through the card's own
    writer; the worker then reaches a task queued for the online model."""
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker

    ids = await role_seed.seed_async()
    sm = get_sessionmaker()
    async with sm() as s:
        await _put(s, **IN_USE)
    cycle = await _load(sm)
    payload = {"opportunity_id": ids["strong"], "rescore": True, "requested": True,
               "requested_at": "2099-01-01T00:00:00Z"}
    if stamped:
        payload["online_approval"] = approval_for(cycle, ("tier2",))
    async with sm() as s:
        s.add(Task(type="t2", payload=payload, status="pending", priority=1000,
                   attempts=attempts, max_attempts=3))
        await setup_state.set_run_mode(s, "local", cycle)
        await s.commit()
    client = _Rec(cycle)
    await Worker(sm, client, cycle).drain_tier("t2", 1, handle_t2, TierStats())
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().one()
    return sm, client.sent, task, payload


async def _next_cycle(sm):
    """What the serve loop does at the start of a cycle: fresh settings, holds released, drain."""
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker

    fresh = await _load(sm)
    released = await release_holds(sm, fresh)
    client = _Rec(fresh)
    await Worker(sm, client, fresh).drain_tier("t2", 1, handle_t2, TierStats())
    async with sm() as s:
        task = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().one()
    return released, client.sent, task


@pytest.mark.parametrize("attempts", [0, 1], ids=["first-try", "retry"])
@pytest.mark.parametrize("stamped", [True, False], ids=["confirmed", "scheduled"])
def test_work_held_by_the_local_choice_stays_queued_and_spends_no_try(attempts, stamped, key):
    with _client():
        _sm, sent, task, payload = asyncio.run(_hold(attempts, stamped=stamped))
    assert sent == [], "a task was run while On this computer was chosen"
    assert task.status == "pending" and task.attempts == attempts
    assert task.payload.get(DEFERRED_KEY) and task.payload.get(DEFER_REASON_KEY) == REASON_APPROVAL
    kept = {k: v for k, v in task.payload.items() if k not in (DEFERRED_KEY, DEFER_REASON_KEY)}
    assert kept == payload, "the held task's own payload changed"


def test_held_work_runs_online_again_when_online_comes_back_into_use(key):
    async def go():
        sm, sent, _task, _payload = await _hold(0)
        async with sm() as s:
            await _put(s, **IN_USE)
        return sent, await _next_cycle(sm)

    with _client():
        sent, (released, resumed, task) = asyncio.run(go())
    assert sent == [] and released == 1
    assert resumed == [(MODEL, True)] and task.status == "done"


def test_held_work_runs_on_this_computer_when_the_person_stays_on_local(key):
    """The chosen behavior (D-PRIV-798): On this computer moves the scoring steps to local
    models, so at the next cycle the held task's step is local and it runs there."""
    async def go():
        sm, sent, _task, _payload = await _hold(0)
        return sent, await _next_cycle(sm)

    with _client():
        sent, (released, resumed, task) = asyncio.run(go())
    assert sent == [] and released == 1
    assert len(resumed) == 1 and resumed[0][1] is False and resumed[0][0] != MODEL
    assert task.status == "done"


def test_a_task_still_bound_for_an_online_model_is_not_released(key):
    """The release test and the worker's hold test agree: while the step is online and online is
    not in use, the task stays held (no release and re-defer churn)."""
    async def go():
        sm, _sent, _task, _payload = await _hold(0)
        cycle_like = await _load(sm)
        async with sm() as s:
            await _put(s, enabled=True, choice="local", mode="hosted")
        still_online = await _load(sm)
        assert still_online.llm.backends[still_online.llm.tiers["tier2"].backend].external
        assert cycle_like is not still_online
        released = await release_holds(sm, still_online)
        async with sm() as s:
            task = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().one()
        return released, task

    with _client():
        released, task = asyncio.run(go())
    assert released == 0 and task.status == "pending" and task.payload.get(DEFERRED_KEY)


def test_a_confirmed_one_run_pick_runs_on_the_usual_local_model_after_the_local_choice(key):
    """A one-run pick that cannot run falls back to the configured model (D-UI-50), as it does
    when online is switched off. With On this computer chosen that model is local."""
    async def go():
        from kestrel.pipeline.tier2 import handle_t2
        from kestrel.pipeline.worker import TierStats, Worker

        ids = await role_seed.seed_async()
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, enabled=True, choice="both", mode="local")
        cycle = await _load(sm)
        picked = settings_with_override(cycle, "tier2", PICK)
        payload = {"opportunity_id": ids["strong"], "rescore": True, "requested": True,
                   "requested_at": "2099-01-01T00:00:00Z", "model_override": PICK,
                   "online_approval": {"tier2": [destination_id(picked, "tier2")]}}
        async with sm() as s:
            s.add(Task(type="t2", payload=payload, status="pending", priority=1000,
                       attempts=0, max_attempts=3))
            await setup_state.set_run_mode(s, "local", cycle)
            await s.commit()
        client = _Rec(cycle)
        await Worker(sm, client, cycle).drain_tier("t2", 1, handle_t2, TierStats())
        async with sm() as s:
            task = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().one()
        return client.sent, task, cycle.llm.tiers["tier2"].model

    with _client():
        sent, task, local_model = asyncio.run(go())
    assert sent == [(local_model, False)] and task.status == "done"


@pytest.mark.parametrize("choose_local", [False, True], ids=["in-use", "local-chosen"])
def test_a_signal_search_in_a_running_cycle_follows_the_choice_made_since(
    choose_local, key, monkeypatch
):
    """The classifier of a signal search runs on the cycle's settings. On an online model it is
    judged with the state as it is now: chosen after the cycle began, On this computer holds it."""
    from types import SimpleNamespace

    from kestrel.db.models import Source
    from kestrel.ingest import signal_runner
    from kestrel.pipeline.worker import Worker

    ran: list[tuple[int, bool]] = []

    async def run(_sm, source_id, *, client, settings, **_kw):
        ran.append((source_id, setup_state.online_send_held(client.settings) is None))
        return SimpleNamespace(status="ok", counts={})

    monkeypatch.setattr(signal_runner, "run_signal_source", run)

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **IN_USE)
        cycle = await _load(sm)
        assert cycle.llm.backends[cycle.llm.tiers["trigger"].backend].external
        async with sm() as s:
            source = Source(name="Invented signal", kind="rss", track="employment",
                            enabled=True, config={})
            s.add(source)
            await s.commit()
            sid = source.id
        worker = Worker(sm, _Rec(cycle), cycle)
        await worker._queue.enqueue(
            "scrape", {"source_id": sid, "online_approval": approval_for(cycle, ("trigger",))}
        )
        if choose_local:
            async with sm() as s:
                await setup_state.set_run_mode(s, "local", cycle)
                await s.commit()
        await asyncio.wait_for(worker.drain_scrapes(), timeout=30)
        async with sm() as s:
            task = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().one()
        return sid, task

    with _client():
        sid, task = asyncio.run(go())
    expected = {
        True: ([], "pending", REASON_APPROVAL),   # held in the queue, nothing classified
        False: ([(sid, True)], "done", None),     # the control: in use, so it runs
    }[choose_local]
    assert (ran, task.status, task.payload.get(DEFER_REASON_KEY)) == expected


# ------------------------------------------------------------- every other gate asks the owner
@pytest.mark.parametrize("state", sorted(NOT_IN_USE))
def test_the_route_gates_refuse_while_online_is_not_in_use(state, wire, key):
    """The consent check, the follow-up draft and a research job, asked with settings loaded
    while the step was online (a request already in flight) that carry the state as it is now."""
    from kestrel.research import jobs
    from kestrel.services import follow_up

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **IN_USE)
        allowed = await _load(sm)
        approval = approval_for(allowed, ("tier3",))
        form = {"confirmed": "1", "online_approval": approval}
        job = {"destination": jobs.destination(allowed), jobs.CONFIRMED_ONLINE_KEY: True}
        async with sm() as s:
            ok = await approval_problem(s, allowed, ("tier3",), form)
            await _put(s, **NOT_IN_USE[state])
        async with sm() as s:
            now = allowed.model_copy(deep=True)
            now.llm.hosted_mode = (await load_settings_with_db(s)).llm.hosted_mode
            return (
                approval, ok,
                await approval_problem(s, now, ("tier3",), form),
                await follow_up.send_problem(s, now),
                await jobs._approval_error(s, now, job),
                held_for_approval(now, ("tier3",), {"online_approval": approval}),
                now,
            )

    with _client():
        approval, ok, consent, draft, job, held, now = asyncio.run(go())
    assert approval and ok is None  # in use and confirmed: the consent check passes
    assert consent and draft and job and held is True, state
    assert job == "Online approval is needed. Start again to confirm the send."
    # The words match the cause: each route's own sentence for switched off or a stale tick,
    # and the run card's sentence when On this computer is chosen.
    local = f"Nothing was sent. {setup_state.LOCAL_CHOSEN}"
    words = {
        "local": (local, local),
        "off": (HOSTED_OFF, f"Nothing was sent. First, {ONLINE_CONSENT_HOW}."),
        "stale": (HOSTED_OFF, f"Nothing was sent. First, {ONLINE_CONSENT_HOW}."),
    }[state]
    assert (consent, draft) == words
    with pytest.raises(ExternalBackendError):
        asyncio.run(LLMClient(now).complete(tier="tier3", messages=ASK))
    assert _online(wire) == []


@pytest.mark.parametrize("state", sorted(NOT_IN_USE))
def test_a_mock_interview_already_routed_online_sends_nothing(state, wire, key):
    """The practice page confirms once and the answer route checks again on every answer. A
    session confirmed for an online model is refused on its next answer."""
    from kestrel.web.pages import mock

    form = {"confirmed": "1", "route_model": "hosted:" + MODEL}

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **NOT_IN_USE[state])
        async with sm() as s:
            return await mock._client_settings(s, form)

    with _client():
        settings, why_not, where = asyncio.run(go())
    assert settings is None and where == ""
    expected = {
        "local": f"Nothing was sent. {setup_state.LOCAL_CHOSEN}",
        "off": mock._HOSTED_OFF,
        "stale": mock._HOSTED_OFF,
    }[state]
    assert why_not == expected
    assert _online(wire) == []


def test_a_mock_answer_on_a_step_left_online_is_refused_by_the_client(wire, key):
    """Rows that disagree (the card on Local, the stored mode still online, as a restore can
    leave them): the page treats the answer as local and the client refuses the send."""
    from kestrel.web.pages import mock

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, enabled=True, choice="local", mode="hosted")
        async with sm() as s:
            settings, why_not, where = await mock._client_settings(s, {})
        assert settings is not None and why_not is None and where == "computer"
        assert settings.llm.backends[settings.llm.tiers[mock.TIER].backend].external
        await LLMClient(settings).complete(tier=mock.TIER, messages=ASK)

    with _client(), pytest.raises(ExternalBackendError, match="On this computer is chosen"):
        asyncio.run(go())
    assert _online(wire) == []


# ------------------------------------------------------------------- the card rides on settings
def test_settings_loaded_from_the_rows_carry_the_card_and_copies_keep_it(key):
    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, enabled=True, choice="both", mode="local")
        first = await _load(sm)
        async with sm() as s:
            await setup_state.set_run_mode(s, "local", first)
            await s.commit()
        return first, await _load(sm)

    with _client():
        first, second = asyncio.run(go())
    assert first.llm.hosted_mode._run_card == {"setup.run_choice": "both", "llm.mode": "local"}
    assert second.llm.hosted_mode._run_card == {"setup.run_choice": "local", "llm.mode": "local"}
    assert setup_state.online_send_held(first) is None
    assert setup_state.online_send_held(second) == setup_state.LOCAL_CHOSEN
    copy = second.model_copy(deep=True)
    assert setup_state.online_send_held(copy) == setup_state.LOCAL_CHOSEN
    assert "_run_card" not in second.llm.hosted_mode.model_dump()


def test_a_default_shown_on_the_card_is_not_a_choice():
    """No card choice stored and no key saved: the card shows Local as a default and the
    provider reads stay off, but a send the person confirms is not refused as "On this computer
    is chosen". Storing the choice is what holds it."""
    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, enabled=True)
        default = await _load(sm)
        async with sm() as s:
            await _put(s, enabled=True, choice="local", mode="local")
        return default, await _load(sm)

    with _client():
        default, chosen = asyncio.run(go())
    assert default.llm.hosted_mode._run_card == {}
    assert setup_state.run_choice(default, {}) == ("local", False)
    assert setup_state.online_not_in_use(default, {}) == setup_state.LOCAL_CHOSEN  # the reads
    assert setup_state.online_send_held(default) is None
    assert setup_state.online_send_held(chosen) == setup_state.LOCAL_CHOSEN


def test_a_stored_row_or_a_yaml_key_cannot_supply_the_card(key):
    """Only the card's own two rows feed it: a row named like the carrier is not configuration."""
    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **NOT_IN_USE["local"])
            await s.merge(Setting(key="llm.hosted_mode._run_card",
                                  value={"setup.run_choice": "hosted"}))
            await s.merge(Setting(key="__run_card__", value={"setup.run_choice": "hosted"}))
            await s.commit()
        return await _load(sm)

    with _client():
        settings = asyncio.run(go())
    assert settings.llm.hosted_mode._run_card == {"setup.run_choice": "local", "llm.mode": "local"}
    assert setup_state.online_send_held(settings) == setup_state.LOCAL_CHOSEN


# ----------------------------------------------------------------- one owner, by construction
# Where product code may still read the switch-and-tick flag directly. Each is a definition, a
# status shown to the person, or a doctor row: none decides whether a send may proceed. A new
# read anywhere else fails here: an online send asks setup_state.online_send_held instead.
ACTIVE_READS = {
    "doctor.py": 3,                    # the Hosted mode report rows and hosted_only: no sends
    "llm/client.py": 1,                # the refusal's wording, after the owner has decided
    "llm/providers.py": 2,             # assignment_status / tier_status rows on Settings
    "pipeline/worker.py": 1,           # the log line's wording, after the owner has decided
    "services/diagnostics.py": 1,      # the bundle's summary
    "services/hosted_balance.py": 1,   # eligible_backend, behind online_not_in_use_now
    "services/setup_state.py": 2,      # online_not_in_use and online_send_held: the rule
    "web/pages/documents.py": 1,       # the picker's "offered once Hosted mode is on" note
    "web/pages/rescore.py": 2,         # the same note, and the dialog's hosted_on flag
    "web/prep_view.py": 1,             # can_switch: whether the practice page offers a switch
}
_ACTIVE_READ = re.compile(r"\b(?:hosted_mode|hosted|hm|live)\.active\b")


def test_only_the_owning_function_decides_from_the_switch_and_tick():
    root = resources.files("kestrel")
    found: dict[str, int] = {}

    def walk(node, prefix=""):
        for child in node.iterdir():
            name = prefix + child.name
            if child.is_dir():
                walk(child, name + "/")
            elif name.endswith(".py"):
                hits = len(_ACTIVE_READ.findall(child.read_text(encoding="utf-8")))
                if hits:
                    found[name] = hits

    walk(root)
    assert found == ACTIVE_READS, (
        "a new read of hosted_mode.active: if it decides whether something is sent online, "
        "ask setup_state.online_send_held(settings) instead (D-PRIV-798)"
    )


# ------------------------------------------ round 2: a repair is a send like any other (KF-074)
# The gate was asked once per client call, of the client's own settings. The guard's repair runs
# on the same client, so a repair after the person chose On this computer still went online (the
# adversarial review of d6da5b23). The client now asks the stored state immediately before
# every request.
class _Verdict(BaseModel):
    verdict: str


def _reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "invented", "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    })


async def _guarded_call_with_a_choice_mid_flight(monkeypatch, state: str | None):
    """Online in use; the first reply fails validation, and while it is in flight the state
    changes the way the app changes it. Then the guard carries on to its repair."""
    from kestrel.llm.guard import run_guarded

    sm = get_sessionmaker()
    async with sm() as s:
        await _put(s, **IN_USE)
    settings = await _load(sm)
    sent: list[str] = []

    async def answer(self, request):
        if request.url.host in ("127.0.0.1", "localhost"):
            raise httpx.ConnectError("test: no local engine")
        sent.append(f"{request.method} {request.url}")
        if len(sent) == 1 and state == "local":
            async with sm() as s:
                await setup_state.set_run_mode(s, "local", settings)
                await s.commit()
        elif len(sent) == 1 and state is not None:
            async with sm() as s:
                await _put(s, **NOT_IN_USE[state])
        return _reply("this is not the JSON that was asked for")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", answer)
    result = await run_guarded(
        LLMClient(settings), tier="tier2", messages=ASK, model_cls=_Verdict,
        schema=_Verdict.model_json_schema(),
    )
    return sent, result


def test_the_repair_probe_can_fail_in_use_the_repair_is_sent(monkeypatch, key):
    with _client():
        sent, result = asyncio.run(_guarded_call_with_a_choice_mid_flight(monkeypatch, None))
    assert len(sent) == 2 and all(s.startswith("POST https://openrouter.ai/") for s in sent)
    assert result.ok is False


@pytest.mark.parametrize("state", sorted(NOT_IN_USE))
def test_a_repair_is_not_sent_once_online_is_not_in_use(state, monkeypatch, key):
    with _client():
        sent, result = asyncio.run(_guarded_call_with_a_choice_mid_flight(monkeypatch, state))
    assert len(sent) == 1, f"the repair went online after the change: {sent}"
    assert result.ok is False


def test_a_second_call_on_the_same_client_is_refused_after_the_local_choice(wire, key):
    """Any later request on a client built while online was in use: a retry, a second prompt."""
    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **IN_USE)
        settings = await _load(sm)
        client = LLMClient(settings)
        async with sm() as s:
            await setup_state.set_run_mode(s, "local", settings)
            await s.commit()
        assert setup_state.online_send_held(client.settings) is None  # the snapshot still says go
        assert await setup_state.online_send_held_now(client.settings) == setup_state.LOCAL_CHOSEN
        await client.complete(tier="tier2", messages=ASK)

    with _client(), pytest.raises(ExternalBackendError, match="On this computer is chosen"):
        asyncio.run(go())
    assert _online(wire) == []


def test_a_state_that_cannot_be_read_holds_the_send(wire, key, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken():
        raise OperationalError("select", {}, Exception("database is locked"))

    async def go():
        sm = get_sessionmaker()
        async with sm() as s:
            await _put(s, **IN_USE)
        settings = await _load(sm)
        monkeypatch.setattr(setup_state, "_stored_online_rows", broken)
        assert await setup_state.online_send_held_now(settings) == setup_state.STATE_UNREADABLE
        await LLMClient(settings).complete(tier="tier2", messages=ASK)

    with _client(), pytest.raises(ExternalBackendError):
        asyncio.run(go())
    assert _online(wire) == []
