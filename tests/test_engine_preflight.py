"""The worker's preflight before Tier 1 and Tier 2 (D-WORKER-6, issue 196 follow-up). With the
local AI engine down, or the tier's model not installed, the drain leases nothing, so no pending
task uses up its tries, and a waiting role says why. Once the engine is back the same task is
scored, once. Invented text only; the Ollama probe is faked (no network)."""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity, Task
from kestrel.pipeline import worker as worker_mod
from kestrel.pipeline._shared import HandlerOutcome
from kestrel.pipeline.worker import ENGINE_DOWN, MODEL_MISSING, TierStats, Worker, engine_wait
from kestrel.web import app as app_module
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def unpaused():
    app_module.set_paused(False)
    yield
    app_module.set_paused(False)


def _probe(monkeypatch, answer):
    seen: list[int] = []

    def probe(_settings, **_kw):
        seen.append(1)
        return answer

    monkeypatch.setattr(worker_mod, "probe_engine", probe)
    return seen


def run(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await fn(sm)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _role_with_task(task_type: str = "t1", stage: str = "t0_pass", **payload) -> int:
    oid = asyncio.run(T._seed())["d"]

    async def go(sm):
        async with sm() as s:
            (await s.get(Opportunity, oid)).stage = stage
            s.add(Task(type=task_type, payload={"opportunity_id": oid, **payload},
                       status="pending"))
            await s.commit()

    run(go)
    return oid


def _tasks(oid: int) -> list[tuple[str, str, int]]:
    async def go(sm):
        async with sm() as s:
            rows = (await s.execute(select(Task).where(Task.opportunity_id == oid))).scalars()
            return [(t.type, t.status, int(t.attempts or 0)) for t in rows]

    return run(go)


def _drain(task_type: str, calls: list[int]) -> None:
    async def handler(sm, client, settings, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        return HandlerOutcome("t1_pass" if task_type == "t1" else "t2_done", 1)

    async def go(sm):
        settings = get_settings()
        w = Worker(sm, SimpleNamespace(settings=settings), settings)
        await w.drain_tier(task_type, 2, handler, TierStats())

    run(go)


def _row(oid: int) -> str:
    with T._client() as c:
        html = T._get(c, "/opportunities")
    return htmllib.unescape(T._row(html, oid))


def _tier_model(tier: str) -> str:
    return str(get_settings().llm.tiers[tier].model)


async def _embed_done(sm, client, settings, *, opportunity_id, profiles, **kw):
    """Stands in for the embed handler in worker-loop tests whose subject is not embed: the real
    one would call the local address, which nothing answers in tests, and that real connect
    failure marks the engine lost for the cycle (D-WORKER-6 amended, round 5)."""
    return HandlerOutcome("done", 0)


def test_with_the_engine_down_nothing_is_leased_and_no_try_is_spent(monkeypatch, unpaused):
    oid = _role_with_task("t1")
    seen = _probe(monkeypatch, (None, []))
    calls: list[int] = []
    for _ in range(5):  # five cycles while Ollama is closed
        _drain("t1", calls)
    assert seen, "the preflight probed the engine"
    assert calls == []
    assert _tasks(oid) == [("t1", "pending", 0)], "no try was spent and the task was not leased"
    assert engine_wait() == {"tier1": ENGINE_DOWN}
    row = _row(oid)
    assert 'data-scoring="engine_down"' in row
    # General, not "not running": the probe knows only that nothing answered (D-WORKER-6).
    assert "Kestrel cannot reach the AI engine on this computer, so scoring waits." in row
    assert 'href="/models"' in row and "Open Models" in row
    assert "scores on its own" not in row


def test_when_the_engine_is_back_the_same_task_is_scored_once(monkeypatch, unpaused):
    oid = _role_with_task("t1")
    _probe(monkeypatch, (None, []))
    calls: list[int] = []
    _drain("t1", calls)
    _probe(monkeypatch, ("0.0-invented", [_tier_model("tier1")]))
    _drain("t1", calls)
    assert calls == [oid]
    assert _tasks(oid) == [("t1", "done", 1)]
    assert engine_wait() == {}
    assert 'data-scoring="engine_down"' not in _row(oid)


def test_a_missing_model_waits_and_says_so(monkeypatch, unpaused):
    oid = _role_with_task("t2", stage="t1_pass")
    _probe(monkeypatch, ("0.0-invented", ["invented-other-model:latest"]))
    calls: list[int] = []
    _drain("t2", calls)
    assert calls == [] and _tasks(oid) == [("t2", "pending", 0)]
    assert engine_wait() == {"tier2": MODEL_MISSING}
    row = _row(oid)
    assert 'data-scoring="model_missing"' in row
    assert "The model for this step is not installed, so scoring waits." in row


def test_a_tier_that_is_not_on_ollama_is_never_probed(monkeypatch, unpaused):
    """An online tier (or another local server) is not judged by the Ollama probe."""
    oid = _role_with_task("t1")
    seen = _probe(monkeypatch, (None, []))
    settings = get_settings()
    assert "frontier" in settings.llm.backends  # the shipped OpenRouter backend
    settings.llm.tiers["tier1"].backend = "frontier"
    calls: list[int] = []

    async def handler(sm, client, s, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        return HandlerOutcome("t1_pass", 1)

    async def go(sm):
        await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
            "t1", 1, handler, TierStats())

    run(go)
    assert seen == [] and calls == [oid] and engine_wait() == {}


def test_the_role_page_names_the_engine_wait(monkeypatch, unpaused):
    oid = _role_with_task("t1")
    _probe(monkeypatch, (None, []))
    _drain("t1", [])
    with T._client() as c:
        page = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
    assert 'data-unscored="engine_down"' in page
    assert "cannot reach the AI engine on this computer, so scoring waits" in page


def test_idle_cycles_while_the_engine_is_down_open_no_run_and_hold_no_settings(
    monkeypatch, unpaused
):
    """D-WORKER-6 amended: a tier the preflight waits on is skipped by the cycle's work check,
    like an out-of-credit tier. Only the first cycle (before the preflight has spoken) may open
    a Run row; every later idle cycle opens none, so Runs and Today never flash "running" and the
    run's settings are not held while the person fixes Models."""
    from kestrel.pipeline import runlog

    oid = _role_with_task("t1")
    _probe(monkeypatch, (None, []))
    stop = asyncio.Event()
    cycle = {"n": 0}
    opened: list[int] = []
    held: list[int] = []
    real_sweep = Worker.sweep
    real_open = runlog.open_run
    real_hold = app_module._hold_if_free

    async def sweep(self) -> int:
        cycle["n"] += 1
        if cycle["n"] > 5:
            stop.set()
        return await real_sweep(self)

    async def open_run(*a, **k):
        opened.append(cycle["n"])
        return await real_open(*a, **k)

    def hold(*a, **k):
        held.append(cycle["n"])
        return real_hold(*a, **k)

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(Worker, "sweep", sweep)
    monkeypatch.setattr(runlog, "open_run", open_run)
    monkeypatch.setattr(app_module, "_hold_if_free", hold)
    monkeypatch.setattr(app_module, "_wait", no_wait)

    async def drive():
        await asyncio.wait_for(app_module._worker_loop(stop), timeout=120.0)

    asyncio.run(drive())
    assert cycle["n"] > 5
    assert [n for n in opened if n > 1] == [], "a later idle cycle opened a Run row"
    assert [n for n in held if n > 1] == [], "a later idle cycle held the run's settings"
    assert app_module.confirmed_run_settings() is None
    assert [t for t in _tasks(oid) if t[0] == "t1"] == [("t1", "pending", 0)], \
        "and still no Tier 1 try was spent"
    # The embed tier runs on the same Ollama by default: its tasks wait too (owner's Windows
    # check, D-WORKER-6 amended twice).
    embeds = [t for t in _tasks(oid) if t[0] == "embed"]
    assert embeds and all(t[2] == 0 and t[1] == "pending" for t in embeds), embeds

    async def waits(sm):  # a long outage: nothing was even leased, so no wait was counted
        async with sm() as s:
            return [t.payload.get("engine_waits") for t in (await s.execute(
                select(Task).where(Task.opportunity_id == oid))).scalars()]

    assert run(waits) == [None] * len(_tasks(oid)), "round 8: only transitions count"

    async def runs(sm):
        from sqlalchemy import func

        from kestrel.db.models import Run

        async with sm() as s:
            return (await s.execute(select(func.count()).select_from(Run))).scalar_one()

    assert run(runs) == 0, "the one opened in the first cycle had nothing to do and was removed"


def test_the_cycle_ollama_comes_back_in_opens_its_run_before_scoring(monkeypatch, unpaused):
    """D-WORKER-6 amended twice: the work check probes a waiting tier again before deciding, so
    in the first cycle after Ollama is back the Run opens and the run's settings are held before
    the first task is leased and scored (Today and Runs show the run, Stop is there, saves are
    held), never a whole queue scored under an idle status."""
    from kestrel.pipeline import runlog, tier1

    oid = _role_with_task("t1")
    state = {"answer": (None, [])}
    monkeypatch.setattr(worker_mod, "probe_engine", lambda _s, **_k: state["answer"])
    stop = asyncio.Event()
    cycle = {"n": 0}
    events: list[tuple[int, str]] = []
    real_sweep = Worker.sweep
    real_open = runlog.open_run
    real_hold = app_module._hold_if_free

    async def sweep(self) -> int:
        cycle["n"] += 1
        if cycle["n"] == 3:
            state["answer"] = ("0.0-invented",
                               [_tier_model(t) for t in ("tier1", "tier2", "tier3", "embed")])
        if cycle["n"] > 3:
            stop.set()
        return await real_sweep(self)

    async def open_run(*a, **k):
        events.append((cycle["n"], "open"))
        return await real_open(*a, **k)

    def hold(*a, **k):
        events.append((cycle["n"], "hold"))
        return real_hold(*a, **k)

    async def handle_t1(sm, client, settings, *, opportunity_id, profiles, **kw):
        events.append((cycle["n"], "scored"))
        return HandlerOutcome("t1_pass", 1)

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(Worker, "sweep", sweep)
    monkeypatch.setattr(runlog, "open_run", open_run)
    monkeypatch.setattr(app_module, "_hold_if_free", hold)
    monkeypatch.setattr(worker_mod, "handle_embed", _embed_done)
    monkeypatch.setattr(app_module, "_wait", no_wait)
    monkeypatch.setattr(tier1, "handle_t1", handle_t1)

    async def drive():
        await asyncio.wait_for(app_module._worker_loop(stop), timeout=120.0)

    asyncio.run(drive())
    assert [e for e in events if e[0] == 2] == [], "the second cycle (still down) opened nothing"
    third = [name for n, name in events if n == 3]
    assert "scored" in third, "the role was scored once Ollama was back"
    first_scored = third.index("scored")
    assert "open" in third[:first_scored] and "hold" in third[:first_scored], third
    # The original task ran once. (The fake handler leaves the stage at t0_pass, so a later
    # cycle's orphan sweep may queue one more; that is the sweep, not a second run of this one.)
    assert [t for t in _tasks(oid) if t[0] == "t1"][0] == ("t1", "done", 1)
    assert third.count("scored") == 1
    assert engine_wait() == {}


def test_engine_waiting_types_reads_the_record_per_task_type():
    worker_mod._ENGINE_WAIT.clear()
    assert worker_mod.engine_waiting_types() == ()
    worker_mod._ENGINE_WAIT["tier2"] = MODEL_MISSING
    assert worker_mod.engine_waiting_types() == ("t2",)
    worker_mod._ENGINE_WAIT["tier1"] = ENGINE_DOWN
    assert worker_mod.engine_waiting_types() == ("t1", "t2")
    worker_mod._ENGINE_WAIT["embed"] = ENGINE_DOWN
    assert worker_mod.engine_waiting_types() == ("t1", "t2", "embed")


def test_a_score_again_on_an_installed_model_still_opens_the_run(monkeypatch, unpaused):
    """Tier 1's own model is missing, but the waiting task is a Score again on another model
    that is installed: the lease filter lets it through, so the cycle must not skip Tier 1 (its
    Run opens and its settings are held like any other)."""
    other = "kestrel-tier3"
    oid = _role_with_task("t1", rescore=True, requested=True,
                          model_override={"backend": "local", "model": other})
    _probe(monkeypatch, ("0.0-invented", [other]))
    worker_mod._ENGINE_WAIT["tier1"] = MODEL_MISSING

    async def go(sm):
        settings = get_settings()
        return await Worker(sm, SimpleNamespace(settings=settings), settings).engine_skip_types()

    assert run(go) == ()
    assert engine_wait() == {"tier1": MODEL_MISSING}, "the configured model is still missing"
    assert _tasks(oid) == [("t1", "pending", 0)]


def test_a_tier_still_down_is_skipped_after_probing_again(monkeypatch, unpaused):
    oid = _role_with_task("t1")
    seen = _probe(monkeypatch, (None, []))
    worker_mod._ENGINE_WAIT["tier1"] = ENGINE_DOWN

    async def go(sm):
        settings = get_settings()
        return await Worker(sm, SimpleNamespace(settings=settings), settings).engine_skip_types()

    assert run(go) == ("t1",) and seen, "probed again, still down, so skipped"
    assert _tasks(oid) == [("t1", "pending", 0)]


def _drain_embed(calls: list[int], settings=None) -> None:
    async def handler(sm, client, s, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        return HandlerOutcome("done", 0)

    async def go(sm):
        cfg = settings or get_settings()
        w = Worker(sm, SimpleNamespace(settings=cfg), cfg)
        await w.drain_tier("embed", 2, handler, TierStats())

    run(go)


def test_embed_on_ollama_waits_without_spending_tries_and_resumes(monkeypatch, unpaused):
    """Owner's Windows check: with Ollama closed, every embed task went from 1 to 2 tries. The
    embed tier on Ollama is judged by the same preflight: nothing is leased while it is down or
    its model is missing, and the same tasks embed once it is back."""
    oid = _role_with_task("embed")
    _probe(monkeypatch, (None, []))
    calls: list[int] = []
    for _ in range(4):
        _drain_embed(calls)
    assert calls == [] and _tasks(oid) == [("embed", "pending", 0)]
    assert engine_wait() == {"embed": ENGINE_DOWN}
    _probe(monkeypatch, ("0.0-invented", ["invented-other:latest"]))
    _drain_embed(calls)
    assert calls == [] and engine_wait() == {"embed": MODEL_MISSING}
    _probe(monkeypatch, ("0.0-invented", [_tier_model("embed")]))
    _drain_embed(calls)
    assert calls == [oid] and engine_wait() == {}


def test_the_in_process_embedder_is_never_judged_by_the_probe(monkeypatch, unpaused):
    oid = _role_with_task("embed")
    seen = _probe(monkeypatch, (None, []))
    settings = get_settings()
    settings.llm.tiers["embed"].backend = "inprocess"
    calls: list[int] = []
    _drain_embed(calls, settings)
    assert seen == [] and calls == [oid] and engine_wait() == {}


def test_a_waiting_embed_tier_is_skipped_by_the_cycle(monkeypatch, unpaused):
    _role_with_task("embed")
    _probe(monkeypatch, (None, []))
    worker_mod._ENGINE_WAIT["embed"] = ENGINE_DOWN

    async def go(sm):
        settings = get_settings()
        return await Worker(sm, SimpleNamespace(settings=settings), settings).engine_skip_types()

    assert run(go) == ("embed",)


# Every other drain that sends to Ollama (D-WORKER-6 amended, siblings): (task type, how to run
# its drain once). Each is checked with Ollama down (nothing leased, no try spent) and up
# (the same drain leases as before), so a guard that always says "wait" fails too.
def _drains():
    from kestrel.interview.question_task import drain_interview_questions
    from kestrel.interview.story_task import drain_story_builds
    from kestrel.research.org_summary import drain_org_summaries
    from kestrel.services import follow_up
    from kestrel.services.title_suggest import drain_title_suggestions

    def with_client(fn):
        async def go(sm):
            settings = get_settings()
            return await fn(sm, SimpleNamespace(settings=settings), settings)
        return go

    # Called the way the worker cycle calls them: with the cycle's own settings (round 5).
    async def follow(sm):
        return await follow_up.drain(sm, lambda cfg: SimpleNamespace(settings=cfg),
                                     settings=get_settings())

    async def letters(sm):
        from kestrel.web.pages.role import run_letter_drafts

        return await run_letter_drafts(settings=get_settings())

    async def research(sm):
        from kestrel.research.jobs import drain as drain_research

        return await drain_research(sm, settings=get_settings())

    async def checks(sm):
        from kestrel.bench.check import drain_checks

        settings = get_settings()
        return await drain_checks(sm, settings, make_client=lambda s: SimpleNamespace(settings=s))

    return {
        "org_research": ("tier3", research),
        "org_contacts": ("tier3", research),
        "bench": ("tier1", checks),
        "build_stories": ("tier2", with_client(drain_story_builds)),
        "interview_questions": ("tier2", with_client(drain_interview_questions)),
        "title_suggestions": ("tier2", with_client(drain_title_suggestions)),
        "org_summary": ("tier2", with_client(drain_org_summaries)),
        "follow_up": ("tier3", follow),
        "letter_draft": ("tier3", letters),
    }


_SIBLINGS = ["build_stories", "interview_questions", "title_suggestions", "org_summary",
             "follow_up", "letter_draft", "org_research", "org_contacts"]


def _run_sibling(monkeypatch, task_type: str, engine_up: bool) -> tuple[int, str, list[str]]:
    """Seed one pending task of `task_type`, answer the probe up or down, run that drain once.
    Returns (role id, its tier, the leases of `task_type` the drain asked for)."""
    from kestrel.queue.queue import TaskQueue

    tier, drain = _drains()[task_type]
    oid = _role_with_task(task_type)
    models = [_tier_model(t) for t in ("tier1", "tier2", "tier3", "embed")]
    _probe(monkeypatch, ("0.0-invented", models) if engine_up else (None, []))
    leased: list[str] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        if types and task_type in types:
            leased.append(task_type)
        return await real_lease(self, worker_id, types=types, **kw)

    monkeypatch.setattr(TaskQueue, "lease", lease)
    run(drain)
    return oid, tier, leased


@pytest.mark.parametrize("task_type", _SIBLINGS)
def test_every_drain_on_ollama_waits_while_it_is_down(monkeypatch, unpaused, task_type):
    oid, tier, leased = _run_sibling(monkeypatch, task_type, engine_up=False)
    assert leased == [], f"{task_type}: leased while Ollama was down"
    assert [t for t in _tasks(oid) if t[0] == task_type] == [(task_type, "pending", 0)]
    assert engine_wait().get(tier) == ENGINE_DOWN


@pytest.mark.parametrize("task_type", _SIBLINGS)
def test_every_drain_on_ollama_leases_as_before_once_it_answers(monkeypatch, unpaused,
                                                                task_type):
    """The control: a guard that always said "wait" would fail here."""
    _oid, tier, leased = _run_sibling(monkeypatch, task_type, engine_up=True)
    assert leased, f"{task_type}: with Ollama up the drain leases as before"
    assert tier not in engine_wait()


def test_one_engine_answer_per_cycle_with_the_real_probe_and_its_cache(
    monkeypatch, unpaused, real_engine_probe
):
    """D-WORKER-6 amended, round 4. The real probe caches "down" for 15 s and "up" for 5 s, so
    a work check and a later drain in the same cycle could read different answers. Each cycle
    now takes one fresh answer at its start and every check in it reads that one: Ollama asked
    once per cycle, and in the cycle it comes back (with "down" still in the probe's cache) the
    Run opens and the settings are held before the first task is scored. Only the network is
    faked; probe_engine, probe_ollama and the cache are the production code."""
    import httpx

    from kestrel.llm.client import OllamaClient
    from kestrel.pipeline import runlog, tier1

    oid = _role_with_task("t1")
    up = {"on": False}
    asked: list[int] = []
    cycle = {"n": 0}

    def version(self, timeout=5.0):
        asked.append(cycle["n"])
        if not up["on"]:
            raise httpx.ConnectError("invented: connection refused")
        return "0.0-invented"

    def list_models(self, timeout=10.0):
        return [_tier_model(t) for t in ("tier1", "tier2", "tier3", "embed")]

    monkeypatch.setattr(OllamaClient, "version", version)
    monkeypatch.setattr(OllamaClient, "list_models", list_models)
    stop = asyncio.Event()
    events: list[tuple[int, str]] = []
    real_sweep = Worker.sweep
    real_open = runlog.open_run
    real_hold = app_module._hold_if_free

    async def sweep(self) -> int:
        cycle["n"] += 1
        if cycle["n"] == 3:
            up["on"] = True  # back, while "down" is still in the probe's 15 s cache
        if cycle["n"] > 3:
            stop.set()
        return await real_sweep(self)

    async def open_run(*a, **k):
        events.append((cycle["n"], "open"))
        return await real_open(*a, **k)

    def hold(*a, **k):
        events.append((cycle["n"], "hold"))
        return real_hold(*a, **k)

    async def handle_t1(sm, client, settings, *, opportunity_id, profiles, **kw):
        events.append((cycle["n"], "scored"))
        return HandlerOutcome("t1_pass", 1)

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(Worker, "sweep", sweep)
    monkeypatch.setattr(runlog, "open_run", open_run)
    monkeypatch.setattr(app_module, "_hold_if_free", hold)
    monkeypatch.setattr(worker_mod, "handle_embed", _embed_done)
    monkeypatch.setattr(app_module, "_wait", no_wait)
    monkeypatch.setattr(tier1, "handle_t1", handle_t1)

    async def drive():
        await asyncio.wait_for(app_module._worker_loop(stop), timeout=120.0)

    asyncio.run(drive())
    for n in (1, 2, 3):
        assert asked.count(n) == 1, f"cycle {n} asked Ollama {asked.count(n)} times"
    assert [e for e in events if e[0] == 2] == []
    third = [name for n, name in events if n == 3]
    assert "scored" in third, third
    first = third.index("scored")
    assert "open" in third[:first] and "hold" in third[:first], third
    assert [t for t in _tasks(oid) if t[0] == "t1"][0] == ("t1", "done", 1)
    from kestrel.llm import engine_cycle

    assert engine_cycle.answer(get_settings().llm.ollama_native_url) == (False, None), \
        "no cycle's answer is left for what runs between"


def test_every_check_in_a_cycle_reads_the_one_answer_even_past_the_probe_cache(
    monkeypatch, unpaused, real_engine_probe
):
    """A cycle can outlast the probe's cache (5 s up, 15 s down). With the cache at zero, every
    check in the cycle (the work check, Tier 1, Tier 2, embed, the other drains) still reads the
    cycle's one answer: Ollama is asked once per cycle, never once per drain."""
    import httpx

    from kestrel.llm.client import OllamaClient
    from kestrel.services import setup_state

    _role_with_task("t1")
    monkeypatch.setattr(setup_state, "PROBE_TTL_UP_S", 0.0)
    monkeypatch.setattr(setup_state, "PROBE_TTL_DOWN_S", 0.0)
    asked: list[int] = []
    cycle = {"n": 0}

    def version(self, timeout=5.0):
        asked.append(cycle["n"])
        raise httpx.ConnectError("invented: connection refused")

    monkeypatch.setattr(OllamaClient, "version", version)
    stop = asyncio.Event()
    real_sweep = Worker.sweep

    async def sweep(self) -> int:
        cycle["n"] += 1
        if cycle["n"] > 2:
            stop.set()
        return await real_sweep(self)

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(Worker, "sweep", sweep)
    monkeypatch.setattr(app_module, "_wait", no_wait)
    asyncio.run(asyncio.wait_for(app_module._worker_loop(stop), timeout=120.0))
    assert asked.count(1) == 1 and asked.count(2) == 1, asked


def test_a_stale_wait_is_cleared_when_the_tier_leaves_ollama(monkeypatch, unpaused):
    """A tier moved to an online model no longer waits on Ollama: its old record is cleared, so
    no row keeps saying Kestrel cannot reach the engine for a step that no longer uses it."""
    worker_mod._ENGINE_WAIT["tier3"] = ENGINE_DOWN
    settings = get_settings()
    settings.llm.tiers["tier3"].backend = "frontier"
    assert asyncio.run(worker_mod.local_engine_ready(settings, "tier3")) is True
    assert "tier3" not in engine_wait()


def test_the_model_check_waits_while_ollama_does_not_answer(monkeypatch, unpaused):
    """A check's models are chosen per task, so it records no tier; while Ollama does not
    answer and a step runs on it, nothing is leased and no try is spent."""
    oid, _tier, leased = _run_sibling(monkeypatch, "bench", engine_up=False)
    assert leased == []
    assert [t for t in _tasks(oid) if t[0] == "bench"] == [("bench", "pending", 0)]


def test_the_model_check_leases_as_before_once_ollama_answers(monkeypatch, unpaused):
    _oid, _tier, leased = _run_sibling(monkeypatch, "bench", engine_up=True)
    assert leased


def test_a_model_check_with_every_step_online_runs_while_ollama_is_down(monkeypatch, unpaused):
    """Nothing in the check would reach Ollama, so Ollama being closed holds nothing back."""
    from kestrel.bench.check import drain_checks
    from kestrel.queue.queue import TaskQueue

    _role_with_task("bench")
    _probe(monkeypatch, (None, []))
    settings = get_settings()
    for tier in ("tier1", "tier2", "tier3"):
        settings.llm.tiers[tier].backend = "frontier"
    leased: list[str] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        if types and "bench" in types:
            leased.append("bench")
        return await real_lease(self, worker_id, types=types, **kw)

    monkeypatch.setattr(TaskQueue, "lease", lease)
    run(lambda sm: drain_checks(sm, settings, make_client=lambda s: SimpleNamespace(settings=s)))
    assert leased


def _dead_embed(key: str = "d", **opp_fields) -> int:
    """A seeded role (d: no score yet; a: scored) whose embed task died after its tries."""
    oid = asyncio.run(T._seed())[key]

    async def go(sm):
        async with sm() as s:
            opp = await s.get(Opportunity, oid)
            for k, v in opp_fields.items():
                setattr(opp, k, v)
            s.add(Task(type="embed", status="dead", attempts=3,
                       last_error="httpx.ConnectError: All connection attempts failed",
                       payload={"opportunity_id": oid}))
            await s.commit()

    run(go)
    return oid


def test_a_role_whose_embed_task_died_says_so_on_its_page_and_on_runs(unpaused):
    """D-WORKER-6 amended, round 4: scoring does not wait for the vector, and a role scored
    without one is never silent about it: the role page and Runs give the reason. Role a has a
    score, so its page says it was still scored."""
    oid = _dead_embed("a")
    with T._client() as c:
        page = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
        runs = htmllib.unescape(c.get("/runs").text)
    assert 'data-testid="eval-vector"' in page
    assert "Kestrel could not prepare this role for sorting. Kestrel could not reach the model." \
        in page and "It was still scored, though its place in your lists" in page
    i = runs.index('data-testid="rn-why-vector"')
    block = runs[i:i + 1500]
    assert "1 role could not be prepared for sorting" in block
    assert f'href="/opportunities/{oid}"' in block and "could not reach the model" in block


def test_no_vector_note_when_the_role_has_one_or_is_hidden_or_was_retried(unpaused):
    from kestrel.db.models import OpportunityEmbedding
    from kestrel.llm.embeddings import current_model
    from kestrel.web.unscored import vector_failures

    with_vector = _dead_embed()

    async def add(sm):
        async with sm() as s:
            s.add(OpportunityEmbedding(opportunity_id=with_vector, vector=b"\0" * 4, dims=1,
                                       model=current_model(get_settings())))
            await s.commit()

    run(add)

    async def others(sm):
        async with sm() as s:
            hidden = Opportunity(track="employment", opp_type="job_posting",
                                 title="Invented Clerk", stage="t2_done",
                                 archived_at="2026-01-01T00:00:00+00:00")
            retried = Opportunity(track="employment", opp_type="job_posting",
                                  title="Invented Steward", stage="t2_done")
            s.add_all([hidden, retried])
            await s.flush()
            for o in (hidden, retried):
                s.add(Task(type="embed", status="dead", attempts=3, last_error="boom",
                           payload={"opportunity_id": o.id}))
            s.add(Task(type="embed", status="pending", payload={"opportunity_id": retried.id}))
            await s.commit()
            return await vector_failures(s)

    assert run(others) == {}


@pytest.mark.parametrize("task_type", ["org_research", "follow_up", "letter_draft"])
def test_a_caller_without_settings_still_gets_the_engine_check(monkeypatch, unpaused, task_type):
    """Outside the worker cycle (a page, a script) these drains read the settings themselves,
    and the engine check still holds."""
    from kestrel.queue.queue import TaskQueue
    from kestrel.research.jobs import drain as drain_research
    from kestrel.services import follow_up
    from kestrel.web.pages.role import run_letter_drafts

    oid = _role_with_task(task_type)
    _probe(monkeypatch, (None, []))
    leased: list[str] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        if types and task_type in types:
            leased.append(task_type)
        return await real_lease(self, worker_id, types=types, **kw)

    monkeypatch.setattr(TaskQueue, "lease", lease)
    calls = {
        "org_research": lambda sm: drain_research(sm),
        "follow_up": lambda sm: follow_up.drain(sm, lambda cfg: SimpleNamespace(settings=cfg)),
        "letter_draft": lambda sm: run_letter_drafts(),
    }
    run(calls[task_type])
    assert leased == []
    assert [t for t in _tasks(oid) if t[0] == task_type] == [(task_type, "pending", 0)]


def test_runs_lists_the_newest_roles_that_could_not_be_prepared_with_their_titles(unpaused):
    """Round 5, finding 2: with more dead preparations than the card shows, the roles listed are
    the newest five, each with its own title and reason, and the rest are counted."""
    asyncio.run(T._seed())

    async def seed(sm):
        async with sm() as s:
            ids = []
            for n in range(12):
                o = Opportunity(track="employment", opp_type="job_posting",
                                title=f"Invented Role {n:02d}", stage="t2_done")
                s.add(o)
                await s.flush()
                ids.append(o.id)
                s.add(Task(type="embed", status="dead", attempts=3, last_error="boom",
                           payload={"opportunity_id": o.id}))
            await s.commit()
            return ids

    ids = run(seed)
    with T._client() as c:
        runs = htmllib.unescape(c.get("/runs").text)
    i = runs.index('data-testid="rn-why-vector"')
    block = runs[i:runs.index("</div>", i)]
    shown = [int(x) for x in re.findall(r'href="/opportunities/(\d+)"', block)]
    assert shown == sorted(ids, reverse=True)[:5]
    for oid in shown:
        assert f"Invented Role {ids.index(oid):02d}" in block
    assert "and 7 more." in block and "12 roles could not be prepared for sorting" in block


def test_a_role_still_waiting_for_its_score_is_not_told_it_was_scored(unpaused):
    """Round 5, finding 3: the sentence comes from the role's state: a role waiting for Tier 1
    reads that scoring goes ahead without it, never that it was scored."""
    oid = _dead_embed(stage="t0_pass")
    with T._client() as c:
        page = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
    assert "Scoring goes ahead without it" in page
    assert "It was still scored" not in page


def test_a_check_of_a_local_model_waits_while_ollama_is_down_with_every_tier_online(
    monkeypatch, unpaused
):
    """Round 5, finding 4: every tier is online, so checks run, but this one names a model on
    this computer: it waits with its try given back instead of recording the model as missing."""
    from kestrel.bench.check import drain_checks

    oid = _role_with_task("bench", steps=["quick_screen"],
                          models={"quick_screen": [{"model": "kestrel-tier3",
                                                    "backend": "local"}]})
    _probe(monkeypatch, (None, []))
    settings = get_settings()
    for tier in ("tier1", "tier2", "tier3"):
        settings.llm.tiers[tier].backend = "frontier"
    ran: list[int] = []

    async def runner(*a, **k):
        ran.append(1)
        raise AssertionError("the check must not run")

    run(lambda sm: drain_checks(sm, settings, make_client=lambda s: SimpleNamespace(settings=s),
                                runner=runner))
    assert ran == []
    assert [t for t in _tasks(oid) if t[0] == "bench"] == [("bench", "pending", 0)]


def test_ollama_lost_mid_cycle_spends_no_try_and_every_later_drain_waits(monkeypatch, unpaused):
    """D-WORKER-6 amended, round 5. The cycle starts with Ollama answering; then the Tier 1 call
    cannot connect (a real connect failure from the real client to the local address). From that
    moment the cycle's answer is "down": the Tier 1 task spends no try, and nothing later in the
    cycle leases (Tier 2, story builds, research with its public fetches)."""
    import httpx

    from kestrel.pipeline import tier1
    from kestrel.queue.queue import TaskQueue

    oid = _role_with_task("t1")
    for task_type in ("t2", "build_stories", "org_research"):
        async def add(sm, task_type=task_type):
            async with sm() as s:
                s.add(Task(type=task_type, status="pending", payload={"opportunity_id": oid}))
                await s.commit()
        run(add)
    models = [_tier_model(t) for t in ("tier1", "tier2", "tier3", "embed")]
    _probe(monkeypatch, ("0.0-invented", models))  # answering when the cycle starts
    failures: list[str] = []

    async def handle_t1(sm, client, settings, *, opportunity_id, profiles, **kw):
        _probe(monkeypatch, (None, []))  # Ollama goes away now; the fresh probe will see it
        try:
            await client.complete(tier="tier1", messages=[{"role": "user", "content": "x"}])
        except httpx.ConnectError as exc:  # nothing listens on the local address in tests
            failures.append(type(exc).__name__)
            raise
        raise AssertionError("expected the local engine to be unreachable")

    leased: list[str] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        task = await real_lease(self, worker_id, types=types, **kw)
        if task is not None:
            leased.append(task.type)
        return task

    stop = asyncio.Event()
    cycle = {"n": 0}
    real_sweep = Worker.sweep

    async def sweep(self) -> int:
        cycle["n"] += 1
        if cycle["n"] > 1:
            stop.set()
        return await real_sweep(self)

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(tier1, "handle_t1", handle_t1)
    monkeypatch.setattr(TaskQueue, "lease", lease)
    monkeypatch.setattr(Worker, "sweep", sweep)
    monkeypatch.setattr(worker_mod, "handle_embed", _embed_done)
    monkeypatch.setattr(app_module, "_wait", no_wait)
    asyncio.run(asyncio.wait_for(app_module._worker_loop(stop), timeout=120.0))
    assert failures == ["ConnectError"]
    assert [t for t in leased if t != "embed"] == ["t1"], leased
    tasks = {t[0]: t for t in _tasks(oid)}
    assert tasks["t1"] == ("t1", "pending", 0), "the lost engine cost the task no try"
    for task_type in ("t2", "build_stories", "org_research"):
        assert tasks[task_type] == (task_type, "pending", 0)


def test_a_failure_the_caller_will_not_retry_is_never_given_back(unpaused):
    """An online send that may have left is failed with retry=False; a lost local engine must
    never turn that into a second send (the consent invariant)."""
    from kestrel.llm import engine_cycle
    from kestrel.queue.queue import TaskQueue

    oid = _role_with_task("letter_draft")
    engine_cycle.start(get_settings().llm.ollama_native_url, ("0.0-invented", []))
    engine_cycle._LOST.add(get_settings().llm.ollama_native_url)

    async def go(sm):
        q = TaskQueue(sm)
        task = await q.lease("w", types=["letter_draft"])
        return await q.fail(task.id, "invented: the send may have left", worker_id="w",
                            retry=False)

    assert run(go) == "dead"
    assert _tasks(oid) == [("letter_draft", "dead", 1)]


def test_an_embed_call_that_cannot_connect_marks_the_engine_lost_for_the_cycle(
    monkeypatch, unpaused
):
    """The embed client is the other door to Ollama: its connect failure counts the same."""
    import httpx

    from kestrel.llm import engine_cycle
    from kestrel.llm.client import LLMClient

    _probe(monkeypatch, (None, []))  # the fresh probe after the failure finds it down
    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))

    async def go():
        async with httpx.AsyncClient() as http, httpx.AsyncClient(trust_env=False) as local:
            with pytest.raises(httpx.ConnectError):
                await LLMClient(settings, http=http, http_local=local).embed(["x"])

    asyncio.run(go())
    assert engine_cycle.lost()
    assert engine_cycle.answer(settings.llm.ollama_native_url) == (True, (None, []))


def test_only_the_task_whose_own_call_could_not_reach_ollama_gets_its_try_back(
    monkeypatch, unpaused
):
    """Round 6, finding 1: decided per task. In a cycle where one task's call could not reach
    Ollama, a genuine error of another task (an HTTP 5xx, a model that crashed loading) and an
    online backend's failure each still spend their try."""
    import httpx

    from kestrel.llm import engine_cycle

    _probe(monkeypatch, (None, []))  # the fresh probe after the failure finds it down
    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    lost_role = _role_with_task("t1")
    status = _lease_and_fail("t1", lost_role, unreachable_backend=settings.llm.backends["local"],
                             exc=httpx.ConnectError("invented"))
    assert engine_cycle.lost() and status == "pending"
    assert _tasks(lost_role)[0][2] == 0, "its own call was unreachable: no try spent"

    async def more(sm):
        async with sm() as s:
            ids = []
            for title in ("Invented Genuine", "Invented Online"):
                o = Opportunity(track="employment", opp_type="job_posting", title=title,
                                stage="t1_pass")
                s.add(o)
                await s.flush()
                ids.append(o.id)
            s.add(Task(type="t2", status="pending", payload={"opportunity_id": ids[0]}))
            s.add(Task(type="build_stories", status="pending",
                       payload={"opportunity_id": ids[1]}))
            await s.commit()
            return ids

    genuine, online = run(more)
    _lease_and_fail("t2", genuine)  # a genuine error in the same cycle
    _lease_and_fail("build_stories", online, unreachable_backend=settings.llm.backends["frontier"],
                    exc=httpx.ConnectError("invented"))  # the online lane's own failure
    assert _tasks(genuine)[0][1:] == ("pending", 1), "a genuine error spends its try"
    assert _tasks(online)[0][1:] == ("pending", 1), "an online failure spends its try"


def test_every_loopback_spelling_is_the_same_engine():
    """Round 6, finding 4."""
    from kestrel.llm.engine_cycle import same_engine

    assert same_engine("http://localhost:11434/v1", "http://127.0.0.1:11434")
    assert same_engine("http://[::1]:11434", "http://127.0.0.1:11434")
    assert not same_engine("http://127.0.0.1:11435", "http://127.0.0.1:11434")
    assert not same_engine("https://openrouter.ai/api/v1", "http://127.0.0.1:11434")
    settings = get_settings()
    settings.llm.backends["local"].base_url = "http://localhost:11434/v1"
    assert worker_mod._ollama_model(settings, "tier1") is not None


def test_after_the_first_unreachable_call_no_drain_leases_again_this_cycle(monkeypatch, unpaused):
    """Round 6, finding 2: checked before every lease. Two research tasks: the first one's call
    cannot reach Ollama; the second is never leased, so no public page is fetched for it."""
    import httpx

    from kestrel.llm import engine_cycle
    from kestrel.research import jobs
    from kestrel.research.jobs import drain as drain_research

    oid = _role_with_task("org_research")  # and a second research task for the same role

    async def second(sm):
        async with sm() as s:
            s.add(Task(type="org_contacts", status="pending", payload={"opportunity_id": oid}))
            await s.commit()

    run(second)
    _probe(monkeypatch, (None, []))  # the fresh probe after the failure finds it down
    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url,
                       ("0.0-invented", [_tier_model(t) for t in ("tier1", "tier2", "tier3")]))
    ran: list[int] = []

    async def fake_run(sm, task):
        ran.append(task.id)  # stands for the fetch and the model call
        raise engine_cycle.classify(settings, settings.llm.backends["local"],
                                    httpx.ConnectError("invented"))

    monkeypatch.setattr(jobs, "_run", fake_run)
    run(lambda sm: drain_research(sm, settings=settings))
    assert len(ran) == 1, "the second task was not leased, so nothing was fetched for it"
    assert sorted(t[1:] for t in _tasks(oid)) == [("pending", 0), ("pending", 0)]


def test_an_all_online_setup_never_probes_ollama_for_model_checks(monkeypatch, unpaused):
    """Round 6, finding 5: with no tier on Ollama, a check never asks it."""
    from kestrel.bench.check import drain_checks

    _role_with_task("bench")
    seen = _probe(monkeypatch, (None, []))
    settings = get_settings()
    for tier in ("tier1", "tier2", "tier3"):
        settings.llm.tiers[tier].backend = "frontier"
    run(lambda sm: drain_checks(sm, settings, make_client=lambda s: SimpleNamespace(settings=s)))
    assert seen == []


def test_a_scoring_drain_stops_leasing_after_the_first_unreachable_call(monkeypatch, unpaused):
    import httpx

    from kestrel.llm import engine_cycle

    oid = _role_with_task("t1")

    async def another(sm):
        async with sm() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="Invented Two",
                            stage="t0_pass")
            s.add(o)
            await s.flush()
            s.add(Task(type="t1", status="pending", payload={"opportunity_id": o.id}))
            await s.commit()

    run(another)
    _probe(monkeypatch, (None, []))  # the fresh probe after the failure finds it down
    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url,
                       ("0.0-invented", [_tier_model(t) for t in ("tier1", "tier2")]))
    calls: list[int] = []

    async def handler(sm, client, s, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        raise engine_cycle.classify(settings, settings.llm.backends["local"],
                                    httpx.ConnectError("invented"))

    async def go(sm):
        await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
            "t1", 1, handler, TierStats())

    run(go)
    assert len(calls) == 1, "the second task was not leased this cycle"
    assert _tasks(oid)[0][1:] == ("pending", 0)


def test_a_lost_engine_never_stops_a_drain_whose_tier_is_online(unpaused):
    from kestrel.llm import engine_cycle

    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    engine_cycle._LOST.add(settings.llm.ollama_native_url)
    assert worker_mod.engine_lost_for(settings, "tier2") is True
    settings.llm.tiers["tier2"].backend = "frontier"
    assert worker_mod.engine_lost_for(settings, "tier2") is False


@pytest.mark.parametrize("task_type", _SIBLINGS + ["bench"])
def test_every_drain_stops_leasing_once_the_engine_is_lost_mid_drain(monkeypatch, unpaused,
                                                                     task_type):
    """Round 6, finding 2, for every drain on Ollama: it starts with Ollama answering, the engine
    is lost right after its first lease (as a failed call would mark it), and its second task is
    never leased this cycle."""
    from kestrel.llm import engine_cycle
    from kestrel.queue.queue import TaskQueue

    _tier, drain = _drains()[task_type]
    oid = _role_with_task(task_type)

    async def second(sm):
        async with sm() as s:
            s.add(Task(type=task_type, status="pending", payload={"opportunity_id": oid}))
            await s.commit()

    run(second)
    settings = get_settings()
    models = [_tier_model(t) for t in ("tier1", "tier2", "tier3", "embed")]
    _probe(monkeypatch, ("0.0-invented", models))
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", models))
    leased: list[int] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        task = await real_lease(self, worker_id, types=types, **kw)
        if task is not None and task.type == task_type:
            leased.append(task.id)
            engine_cycle._LOST.add(settings.llm.ollama_native_url)  # its call fails next
        return task

    monkeypatch.setattr(TaskQueue, "lease", lease)
    run(drain)
    assert len(leased) == 1, f"{task_type}: leased again after the engine was lost"


def test_a_timeout_on_every_attempt_while_ollama_answers_reaches_dead(monkeypatch, unpaused):
    """Round 7, finding 1: a slow model that always passes its timeout is the task's own
    failure (Ollama answers the probe), so it spends its tries and ends visible, never waits."""
    import httpx

    from kestrel.llm import engine_cycle

    oid = _role_with_task("t1")
    _probe(monkeypatch, ("0.0-invented", [_tier_model("tier1")]))
    engine_cycle.start(get_settings().llm.ollama_native_url, ("0.0-invented", []))
    statuses = [_attempt(oid, "t1", httpx.ReadTimeout("invented")) for _ in range(3)]
    assert statuses[-1] == "dead" and not engine_cycle.lost()


def test_a_real_outage_gives_the_try_back(monkeypatch, unpaused):
    import httpx

    from kestrel.llm import engine_cycle

    oid = _role_with_task("t1")
    _probe(monkeypatch, (None, []))
    engine_cycle.start(get_settings().llm.ollama_native_url, ("0.0-invented", []))
    assert _attempt(oid, "t1", httpx.ConnectError("invented")) == "pending"
    assert _tasks(oid)[0][2] == 0


def test_after_a_loss_a_score_again_on_ollama_is_not_leased_but_an_online_one_is(
    monkeypatch, unpaused
):
    """Round 7, finding 3: judged by each task's own model. Tier 2 runs online; one task is a
    Score again on a model on this computer, the other is ordinary. After the loss only the
    ordinary (online) one is leased."""
    from kestrel.llm import engine_cycle
    from kestrel.queue.queue import TaskQueue

    on_ollama = _role_with_task("t2", stage="t1_pass", rescore=True, requested=True,
                                model_override={"backend": "local", "model": "kestrel-tier3"})

    async def plain(sm):
        async with sm() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="Invented Online",
                            stage="t1_pass")
            s.add(o)
            await s.flush()
            s.add(Task(type="t2", status="pending", payload={"opportunity_id": o.id}))
            await s.commit()
            return o.id

    online = run(plain)
    settings = get_settings()
    settings.llm.tiers["tier2"].backend = "frontier"
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    engine_cycle._LOST.add(settings.llm.ollama_native_url)
    leased: list[int] = []
    real_lease = TaskQueue.lease

    async def lease(self, worker_id, *, types=None, **kw):
        task = await real_lease(self, worker_id, types=types, **kw)
        if task is not None:
            leased.append(int(task.payload["opportunity_id"]))
        return task

    async def handler(*a, **k):
        return HandlerOutcome("t2_done", 2)

    monkeypatch.setattr(TaskQueue, "lease", lease)
    run(lambda sm: Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
        "t2", 1, handler, TierStats()))
    assert online in leased and on_ollama not in leased, leased


def _mock_client(settings, replies):
    """A real LLMClient whose HTTP goes to an httpx mock transport: each request takes the next
    reply (an httpx exception to raise, or an httpx.Response)."""
    import httpx

    from kestrel.llm.client import LLMClient

    queue = list(replies)

    def answer(request):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    transport = httpx.MockTransport(answer)
    http = httpx.AsyncClient(transport=transport)
    local = httpx.AsyncClient(transport=transport)
    return LLMClient(settings, http=http, http_local=local), (http, local)


def _ok_chat():
    import httpx

    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                              "content": "ok"},
                                                  "finish_reason": "stop"}], "usage": {}})


def _drain_once_with(client, settings, task_type: str = "t1") -> None:
    async def handler(sm, c, s, *, opportunity_id, profiles, **kw):
        for _ in range(2):  # a first call and a second one, as a repair or a retry would
            try:
                await c.complete(tier="tier1", messages=[{"role": "user", "content": "x"}])
            except Exception:  # noqa: BLE001 - the second call's outcome is what counts
                if _ == 1:
                    raise
        return HandlerOutcome("t1_pass", 1)

    async def go(sm):
        async with sm() as s:
            for t in (await s.execute(select(Task).where(Task.type == task_type))).scalars():
                t.retry_at = None
            await s.commit()
        await Worker(sm, client, settings).drain_tier(task_type, 1, handler, TierStats())

    run(go)


def test_a_transport_error_then_a_genuine_failure_in_one_task_spends(monkeypatch, unpaused):
    """Round 8, finding 1: the task's first call cannot connect (Ollama does not answer the
    probe), its second gets HTTP 500. The final call decides: the try is spent."""
    import httpx

    from kestrel.llm import engine_cycle

    oid = _role_with_task("t1")
    settings = get_settings()
    _probe(monkeypatch, (None, []))
    engine_cycle.start(settings.llm.ollama_native_url,
                           ("0.0-invented", [_tier_model("tier1")]))
    client, _held = _mock_client(settings, [httpx.ConnectError("invented"),
                                            httpx.Response(500, json={"error": "invented"})])
    _drain_once_with(client, settings)
    assert _tasks(oid)[0][1:] == ("pending", 1)


def test_a_timeout_through_the_real_client_reaches_dead_while_ollama_answers(
    monkeypatch, unpaused
):
    """Round 8, finding 4, end to end: the real client raises httpx.ReadTimeout through the
    handler, the worker fails the task through TaskQueue.fail, and with Ollama answering the
    probe each attempt spends a try until the task is dead."""
    import httpx

    from kestrel.llm import engine_cycle

    oid = _role_with_task("t1")
    settings = get_settings()
    _probe(monkeypatch, ("0.0-invented", [_tier_model("tier1")]))
    for _ in range(3):
        engine_cycle.start(settings.llm.ollama_native_url,
                           ("0.0-invented", [_tier_model("tier1")]))
        client, _held = _mock_client(settings, [httpx.ReadTimeout("invented")])
        _drain_once_with(client, settings)
    assert _tasks(oid)[0][1] == "dead"


def test_a_lost_engine_never_stops_an_in_process_embed(monkeypatch, unpaused):
    """Round 8, finding 3: the in-process embedder does not use Ollama, so a lost engine does
    not stop it."""
    from kestrel.llm import engine_cycle

    oid = _role_with_task("embed")
    settings = get_settings()
    settings.llm.tiers["embed"].backend = "inprocess"
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    engine_cycle._LOST.add(settings.llm.ollama_native_url)
    calls: list[int] = []
    _drain_embed(calls, settings)
    assert calls == [oid]




# ------------------------------------------------------- round 11: decided by type and the row
# docs/handoff/196-redesign.md. What a task is charged is read from the failure's type and the
# task's stored row: no probe in the error path, no per-task memory, no cap.


def _lease_and_fail(task_type: str, oid: int, *, unreachable_backend=None, exc=None) -> str:
    """Lease one task of `task_type`, optionally have its own model call fail against a backend
    (classified as the client does), then fail it as a drain would: engine_wait from the type."""
    from kestrel.llm import engine_cycle
    from kestrel.queue.queue import TaskQueue

    async def go(sm):
        q = TaskQueue(sm)
        task = await q.lease("w", types=[task_type])
        failure = exc or ValueError("invented failure")
        if unreachable_backend is not None:
            failure = engine_cycle.classify(get_settings(), unreachable_backend, exc) or exc
        return await q.fail(task.id, "invented failure", worker_id="w", retry=True,
                            engine_wait=engine_cycle.engine_unreachable(failure))

    return run(go)


def _attempt(oid: int, task_type: str, exc) -> str:
    """One lease and failure of the role's `task_type` task whose call to Ollama's address
    raised `exc`, classified as the client does; its retry wait skipped."""
    from kestrel.llm import engine_cycle
    from kestrel.queue.queue import TaskQueue

    settings = get_settings()

    async def go(sm):
        async with sm() as s:
            t = (await s.execute(select(Task).where(Task.opportunity_id == oid,
                                                    Task.type == task_type))).scalars().one()
            t.retry_at = None
            await s.commit()
        q = TaskQueue(sm)
        task = await q.lease("w", types=[task_type])
        failure = engine_cycle.classify(settings, settings.llm.backends["local"], exc) or exc
        return await q.fail(task.id, "invented failure", worker_id="w", retry=True,
                            engine_wait=engine_cycle.engine_unreachable(failure))

    return run(go)


def _no_probe(monkeypatch) -> list[int]:
    """The probe answers "up" and counts its calls, so a test can prove the error path never
    asks it."""
    return _probe(monkeypatch, ("0.0-invented", [_tier_model("tier1")]))


def _row_state(oid: int) -> tuple[str, int, str | None, str | None]:
    from kestrel.queue.queue import WAIT_CAUSE_KEY, WAIT_SINCE_KEY

    async def go(sm):
        async with sm() as s:
            t = (await s.execute(select(Task).where(Task.opportunity_id == oid,
                                                    Task.type == "t1"))).scalars().one()
            p = t.payload or {}
            return t.status, int(t.attempts or 0), p.get(WAIT_CAUSE_KEY), p.get(WAIT_SINCE_KEY)

    return run(go)


def _cycles(oid: int, n: int, replies_for_cycle, *, up_at_start=lambda i: True) -> list:
    """Run `n` cycles of one Tier 1 drain through the real client: each cycle starts with the
    engine answering (or not, by `up_at_start`), and its calls get `replies_for_cycle(i)`.
    Returns the task's (status, attempts, wait cause) after each cycle."""
    from kestrel.llm import engine_cycle

    settings = get_settings()
    out = []
    for i in range(n):
        answer = ("0.0-invented", [_tier_model("tier1")]) if up_at_start(i) else (None, [])
        engine_cycle.start(settings.llm.ollama_native_url, answer)
        client, _held = _mock_client(settings, replies_for_cycle(i))
        _drain_once_with(client, settings)
        out.append(_row_state(oid)[:3])
    return out


@pytest.mark.parametrize("door", ["complete", "embed"])
def test_the_client_marks_a_lost_connection_to_ollama_by_type_only(monkeypatch, door):
    """Both client doors: a connection Ollama refused or dropped is re-raised marked
    EngineUnreachable, keeping its own class and name, so its sentence and code read as before.
    A timeout is not marked (Ollama took the call; the model was slow). No probe is asked."""
    import httpx

    from kestrel.errors import classify_model_failure
    from kestrel.llm import engine_cycle
    from kestrel.llm.client import LLMClient

    seen = _no_probe(monkeypatch)
    settings = get_settings()

    async def call(exc):
        def answer(request):
            raise exc

        transport = httpx.MockTransport(answer)
        async with httpx.AsyncClient(transport=transport) as http, \
                httpx.AsyncClient(transport=transport) as local:
            client = LLMClient(settings, http=http, http_local=local)
            try:
                if door == "embed":
                    await client.embed(["x"])
                else:
                    await client.complete(tier="tier1",
                                          messages=[{"role": "user", "content": "x"}])
            except Exception as raised:  # noqa: BLE001 - the raised failure is the subject
                return raised
        raise AssertionError("the call should have failed")

    for exc in (httpx.ConnectError("invented"), httpx.ReadError("invented"),
                httpx.RemoteProtocolError("invented")):
        engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
        raised = asyncio.run(call(exc))
        assert engine_cycle.engine_unreachable(raised), type(exc).__name__
        assert isinstance(raised, type(exc)) and type(raised).__name__ == type(exc).__name__
        assert classify_model_failure(raised) == classify_model_failure(exc)
        assert engine_cycle.lost(), "the rest of the cycle leases nothing on Ollama"
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    raised = asyncio.run(call(httpx.ReadTimeout("invented")))
    assert not engine_cycle.engine_unreachable(raised) and not engine_cycle.lost()
    assert seen == [], "no probe in the error path"


def test_an_online_backends_failure_is_never_an_engine_wait():
    import httpx

    from kestrel.llm import engine_cycle

    settings = get_settings()
    engine_cycle.start(settings.llm.ollama_native_url, ("0.0-invented", []))
    online = settings.llm.backends["frontier"]
    assert engine_cycle.classify(settings, online, httpx.ConnectError("invented")) is None
    assert not engine_cycle.lost()
    engine_cycle.stop()
    local = settings.llm.backends["local"]
    marked = engine_cycle.classify(settings, local, httpx.ConnectError("invented"))
    assert engine_cycle.engine_unreachable(marked)
    assert not engine_cycle.lost(), "outside a cycle no lease is stopped"


def test_a_dropped_connection_waits_even_when_ollama_answers_again(monkeypatch, unpaused):
    """Finding 2: Ollama dropped the call and was back before any later check (a restart, a
    wake from sleep). The cause is uncertain, so the task waits: no try is spent, ever."""
    import httpx

    oid = _role_with_task("t1")
    seen = _no_probe(monkeypatch)
    states = _cycles(oid, 4, lambda i: [httpx.ConnectError("invented")])
    assert states == [("pending", 0, "engine")] * 4
    assert seen == [], "the error path asked no probe"


def test_a_real_outage_spends_nothing_however_many_waits_came_before(monkeypatch, unpaused):
    """Finding 1: a task that has waited through many outages (the old counter on its row says
    20) still spends no try on the next one. There is no cap."""
    import httpx

    oid = _role_with_task("t1", engine_waits=20)
    _no_probe(monkeypatch)
    states = _cycles(oid, 25, lambda i: [httpx.ConnectError("invented")])
    assert states[-1] == ("pending", 0, "engine")
    assert {s[0] for s in states} == {"pending"}


def test_an_outage_that_starts_mid_call_and_lasts_waits_without_spending(monkeypatch, unpaused):
    """The engine goes away during the first cycle's call and stays away: the task waits, and
    the cycles that start with the engine down lease nothing."""
    import httpx

    oid = _role_with_task("t1")
    _no_probe(monkeypatch)
    states = _cycles(oid, 12, lambda i: [httpx.ConnectError("invented")],
                     up_at_start=lambda i: i == 0)
    assert states == [("pending", 0, "engine")] * 12


def test_an_engine_that_comes_and_goes_every_cycle_never_ends_the_task(monkeypatch, unpaused):
    """It answers each cycle's opening probe and drops each call: a visible wait, never dead,
    no try spent (the redesign's item 5)."""
    import httpx

    oid = _role_with_task("t1")
    _no_probe(monkeypatch)
    states = _cycles(oid, 8, lambda i: [httpx.RemoteProtocolError("invented")])
    assert states == [("pending", 0, "engine")] * 8


def test_a_timeout_spends_and_clears_the_wait_from_the_row(monkeypatch, unpaused):
    """A failure that is the task's own (a slow model: Ollama took the call) spends its try and
    clears the wait from the row; three of them end dead, in Needs another try."""
    import httpx

    oid = _role_with_task("t1")
    _no_probe(monkeypatch)
    states = _cycles(oid, 4, lambda i: [httpx.ConnectError("invented")] if i == 0
                     else [httpx.ReadTimeout("invented")])
    assert states == [("pending", 0, "engine"), ("pending", 1, None), ("pending", 2, None),
                      ("dead", 3, None)]


def test_a_wait_records_when_it_began_and_keeps_it(monkeypatch, unpaused):
    import httpx

    oid = _role_with_task("t1")
    _no_probe(monkeypatch)
    _cycles(oid, 1, lambda i: [httpx.ConnectError("invented")])
    first = _row_state(oid)[3]
    _cycles(oid, 2, lambda i: [httpx.ConnectError("invented")])
    assert first and _row_state(oid)[3] == first


def test_a_second_failure_for_the_same_lease_gives_nothing_back(unpaused):
    """A give-back never exceeds the one try the lease charged: once the row is no longer
    leased, a second wait for the same lease gives nothing back, with or without a worker id.
    The task starts with one try spent, so a second give-back could not hide at zero."""
    from kestrel.queue.queue import TaskQueue

    oid = _role_with_task("t1")

    async def go(sm):
        async with sm() as s:
            (await s.execute(select(Task).where(Task.opportunity_id == oid))).scalars().one(
            ).attempts = 1
            await s.commit()
        q = TaskQueue(sm)
        task = await q.lease("w", types=["t1"])  # charges its try: 2
        first = await q.fail(task.id, "invented", worker_id="w", engine_wait=True)  # back to 1
        second = await q.fail(task.id, "invented", worker_id="w", engine_wait=True)
        row = await q.get(task.id)
        after_second = (row.status, row.attempts, (row.payload or {}).get("wait_cause"))
        await q.fail(task.id, "invented", engine_wait=True)  # no worker id: still no give-back
        return first, second, after_second

    first, second, after_second = run(go)
    assert (first, second) == ("pending", "not_owned")
    assert after_second == ("pending", 1, "engine")
    assert _row_state(oid)[1] == 1, "never below the one try spent before this lease"


def test_a_lost_cycle_never_decides_another_tasks_charge(unpaused):
    """Through the worker (round 12): the engine is lost during a task's handler, and the task's
    own failure is genuine. The cycle-wide loss only stops new leases, so the task spends."""
    from kestrel.llm import engine_cycle

    settings = get_settings()
    oid = _role_with_task("t1")

    async def handler(sm, c, s, *, opportunity_id, profiles, **kw):
        engine_cycle.mark_lost(settings.llm.ollama_native_url)  # another call lost the engine
        raise ValueError("invented genuine failure")

    async def go(sm):
        engine_cycle.start(settings.llm.ollama_native_url,
                           ("0.0-invented", [_tier_model("tier1")]))
        await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
            "t1", 1, handler, TierStats())

    run(go)
    assert engine_cycle.lost()
    assert _row_state(oid)[:3] == ("pending", 1, None)


def test_a_waiting_row_reads_engine_down_with_the_preflight_clear(unpaused):
    """What the person sees comes from the row: a pending scoring task whose row says it waits
    for the engine reads as the engine wait on Runs and the role page, whatever the preflight
    saw last."""
    oid = _role_with_task("t1", wait_cause="engine", engine_wait_since="2026-11-16T00:00:00")
    worker_mod._ENGINE_WAIT.clear()
    assert 'data-scoring="engine_down"' in _row(oid)
    with T._client() as c:
        page = htmllib.unescape(c.get(f"/opportunities/{oid}").text)
    assert 'data-unscored="engine_down"' in page


def test_a_task_that_loses_the_engine_every_time_never_blocks_the_others(monkeypatch, unpaused):
    """Round 12, finding 1: the first task loses its connection on every call. Rows that wait
    for the engine are leased after fresh work, so the second task is scored on the next cycle
    while the first still spends nothing."""
    import httpx

    from kestrel.llm import engine_cycle

    settings = get_settings()
    poison = _role_with_task("t1")  # the lower task id: leased first while both are fresh

    async def second(sm):
        async with sm() as s:
            o = Opportunity(track="employment", opp_type="job_posting", title="Invented Other",
                            stage="t0_pass")
            s.add(o)
            await s.flush()
            s.add(Task(type="t1", status="pending", payload={"opportunity_id": o.id}))
            await s.commit()
            return o.id

    other = run(second)
    scored: list[int] = []

    async def handler(sm, c, s, *, opportunity_id, profiles, **kw):
        if opportunity_id == poison:
            raise engine_cycle.classify(settings, settings.llm.backends["local"],
                                        httpx.RemoteProtocolError("invented"))
        scored.append(opportunity_id)
        return HandlerOutcome("t1_pass", 1)

    async def cycle(sm):
        async with sm() as s:
            for t in (await s.execute(select(Task).where(Task.type == "t1"))).scalars():
                t.retry_at = None  # every rest has passed, as the suite's drain helper does
            await s.commit()
        engine_cycle.start(settings.llm.ollama_native_url,
                           ("0.0-invented", [_tier_model("tier1")]))
        await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
            "t1", 1, handler, TierStats())

    for _ in range(3):
        run(cycle)
    assert scored == [other]
    assert _row_state(poison)[:3] == ("pending", 0, "engine")


def test_the_rest_grows_with_the_wait_on_the_row():
    from datetime import UTC, datetime, timedelta

    from kestrel.queue.queue import (
        ENGINE_WAIT_REST_MAX_S,
        ENGINE_WAIT_REST_S,
        engine_wait_rest,
    )

    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    ago = lambda s: (now - timedelta(seconds=s)).isoformat()  # noqa: E731
    assert engine_wait_rest(None, now) == ENGINE_WAIT_REST_S
    assert engine_wait_rest(ago(10), now) == ENGINE_WAIT_REST_S
    assert engine_wait_rest(ago(600), now) == 600
    assert engine_wait_rest(ago(7200), now) == ENGINE_WAIT_REST_MAX_S
    assert engine_wait_rest("not a time", now) == ENGINE_WAIT_REST_S


def test_a_long_wait_rests_as_long_as_it_has_waited(monkeypatch, unpaused):
    """The wait written on the row sets the next rest: a task that has waited ten minutes is
    not asked for again for ten minutes. The queue's own clock is fixed (D-TEST-2)."""
    from datetime import UTC, datetime, timedelta

    from kestrel.queue import queue as queue_mod
    from kestrel.queue.queue import TaskQueue

    fixed = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(queue_mod, "utcnow", lambda: fixed)
    since = (fixed - timedelta(seconds=600)).isoformat()
    oid = _role_with_task("t1", wait_cause="engine", engine_wait_since=since)

    async def go(sm):
        q = TaskQueue(sm)
        task = await q.lease("w", types=["t1"])
        await q.fail(task.id, "invented", worker_id="w", engine_wait=True)
        return (await q.get(task.id)).retry_at

    assert run(go) == queue_mod.iso(fixed + timedelta(seconds=600))
    assert _row_state(oid)[3] == since, "the first wait's time is kept"


def test_an_error_raised_while_handling_a_lost_connection_is_the_drains_own():
    """Round 12: only an explicit `raise ... from` carries the lost engine; an error of the
    drain's own raised inside the handler (the implicit context) spends."""
    import httpx

    from kestrel.llm import engine_cycle

    settings = get_settings()
    marked = engine_cycle.classify(settings, settings.llm.backends["local"],
                                   httpx.ConnectError("invented"))

    def inside_handler(explicit: bool) -> BaseException:
        try:
            raise marked
        except Exception as lost:  # noqa: BLE001 - the subject of the test
            try:
                if explicit:
                    raise RuntimeError("invented wrapper") from lost
                raise ValueError("invented bug of the drain's own")
            except Exception as raised:  # noqa: BLE001
                return raised
        raise AssertionError("unreachable")

    assert engine_cycle.engine_unreachable(inside_handler(explicit=True))
    assert not engine_cycle.engine_unreachable(inside_handler(explicit=False))

