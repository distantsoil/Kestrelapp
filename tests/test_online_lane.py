"""The online lane (D-WORKER-2): story builds (Tier 2) and cover letter drafts (Tier 3) run on a
second coroutine when their model is online, concurrently with the worker loop's board work; with
a local model the worker loop keeps origin's order (D-WORKER-1: the stories and letters the worker
owns run right after scoring) and the lane drains nothing.

INVARIANTS each test pins (I1-I6 from the brief): consent still gates every online send through
the same `check` and consent payload, unchanged; both loops live against one task and the model
is called exactly once; local behavior and drain order are byte-identical; a lane that dies at
setup retries and a fresh start completes the draft exactly once; a tier switch strands nothing;
an exception in either loop never stops the other. Only `LLMClient.complete` and the story
consolidation are faked. Nothing reaches the network or a real model.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from sqlalchemy import select

from kestrel.config import HostedMode
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import DocRender, Run, Setting, Task
from kestrel.interview import stories as stories_mod
from kestrel.interview import story_task
from kestrel.llm.client import ChatResult, LLMClient
from kestrel.pipeline.worker import Worker
from kestrel.web.pages import role as role_page
from tests import role_seed

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MODEL = "vendor/letter-model"
BODY = (
    "Dear Hiring Team,\n\n"
    "Your three centers report on three different calendars; I have spent two years making "
    "them agree. I would like thirty minutes next week to walk through the forecast."
)


@pytest.fixture
def ids():
    return asyncio.run(role_seed.seed_async())


def _app():
    from kestrel.web import app as app_mod

    return app_mod


def _go_online(tier: str) -> None:
    async def seed() -> None:
        async with get_sessionmaker()() as session:
            for row in (await session.execute(select(Setting).where(
                Setting.key.like("llm.hosted_mode.%")))).scalars():
                await session.delete(row)
            for row in (await session.execute(select(Setting).where(
                Setting.key.like(f"llm.tiers.{tier}.%")))).scalars():
                await session.delete(row)
            session.add_all([
                Setting(key="llm.hosted_mode.enabled", value=True),
                Setting(key="llm.hosted_mode.acknowledged_at", value="2026-10-15T00:00:00Z"),
                Setting(key="llm.hosted_mode.acknowledged_terms",
                        value=HostedMode().disclosure_terms()),
                Setting(key=f"llm.tiers.{tier}.backend", value="frontier"),
                Setting(key=f"llm.tiers.{tier}.model", value=MODEL),
            ])
            await session.commit()

    asyncio.run(seed())


def _go_local(tier: str) -> None:
    async def seed() -> None:
        async with get_sessionmaker()() as s:
            rows = (await s.execute(select(Setting).where(
                Setting.key.like(f"llm.tiers.{tier}.%")))).scalars().all()
            for row in rows:
                await s.delete(row)
            enabled = await s.get(Setting, "llm.hosted_mode.enabled")
            if enabled is not None:
                enabled.value = False
            await s.commit()

    asyncio.run(seed())


def _consent(tier: str) -> dict:
    """The ask-first confirmation for the model and destination configured now."""
    from kestrel.services.online_consent import configured_model, destination_id
    from kestrel.services.setting_overrides import load_settings_with_db

    async def go() -> dict:
        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
        return {"consented_model": configured_model(settings, tier),
                "consented_destination": destination_id(settings, tier)}

    return asyncio.run(go())


def _queue_letter(opp_id: int, consent: dict | None = None) -> None:
    payload: dict = {"opportunity_id": opp_id, "requested": True, "kind": "cover_letter"}
    if consent is not None:
        payload.update(consent)

    async def go() -> None:
        async with get_sessionmaker()() as s:
            s.add(Task(type="letter_draft", max_attempts=3, payload=payload))
            await s.commit()

    asyncio.run(go())


def _queue_story(consent: dict | None = None) -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            assert await story_task.request_build(s, consent=consent)

    asyncio.run(go())


def _newest_task() -> Task | None:
    async def go() -> Task | None:
        async with get_sessionmaker()() as s:
            stmt = select(Task).order_by(Task.id.desc()).limit(1)
            return (await s.execute(stmt)).scalars().first()

    return asyncio.run(go())


def _task_of(task_type: str) -> Task | None:
    """The newest task row of `task_type`: the worker loop queues its own tier rows beside the
    lane's, so 'newest row overall' is not a stable pointer to the draft or build under test."""
    async def go() -> Task | None:
        async with get_sessionmaker()() as s:
            stmt = select(Task).where(Task.type == task_type).order_by(Task.id.desc()).limit(1)
            return (await s.execute(stmt)).scalars().first()

    return asyncio.run(go())


def _letter_renders(opp_id: int) -> list:
    async def go() -> list:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(DocRender).where(
                DocRender.opportunity_id == opp_id,
                DocRender.template.in_(("cover_letter", "engagement_outline"))))).scalars())

    return asyncio.run(go())


def _runs() -> list:
    async def go() -> list:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(Run))).scalars())

    return asyncio.run(go())


class LetterModel:
    """Replaces the model call for letter drafts; counts every request."""

    def __init__(self, monkeypatch, stop: asyncio.Event | None = None):
        self.calls = 0
        payload = {"body_md": BODY, "word_count": len(BODY.split()), "claims_used": [],
                   "opening_alternatives": []}
        outer = self

        async def complete(self_client, **kwargs):
            outer.calls += 1
            if stop is not None:
                stop.set()
            return ChatResult(content=json.dumps(payload), model=MODEL)

        monkeypatch.setattr(LLMClient, "complete", complete)


class StoryModel:
    """Replaces the story consolidation; counts every build it was asked for."""

    def __init__(self, monkeypatch, stop: asyncio.Event | None = None):
        self.calls = 0
        outer = self

        async def fake(session, client, settings, **kwargs):
            outer.calls += 1
            if stop is not None:
                stop.set()
            return stories_mod.ConsolidationResult()

        monkeypatch.setattr(stories_mod, "consolidate_stories", fake)


def _run_lane(monkeypatch, stop: asyncio.Event, watchdog_s: float = 1.2) -> None:
    """Run the online lane until `stop` is set (by a fake or the watchdog), so a drain that has
    nothing to do still gets a pass and then exits."""
    app_mod = _app()

    async def wait(stop_event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)
        except TimeoutError:
            pass

    monkeypatch.setattr(app_mod, "_wait", wait)

    async def drive() -> None:
        async def watch() -> None:
            await asyncio.sleep(watchdog_s)
            stop.set()

        w = asyncio.create_task(watch())
        try:
            await app_mod._online_lane(stop)
        finally:
            w.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await w

    asyncio.run(drive())


def _run_both(monkeypatch, stop: asyncio.Event, timeout: float = 30.0) -> list:
    """Run the worker loop and the lane against the same queue, as the app does at startup.
    Returns [lane_result, worker_result]; a clean loop exit is None, an uncaught exception is
    the exception itself."""
    app_mod = _app()

    async def wait(stop_event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)
        except TimeoutError:
            pass

    monkeypatch.setattr(app_mod, "_wait", wait)

    async def drive() -> list:
        lane = asyncio.create_task(
            app_mod._online_lane(stop), name="lane"
        )
        worker = asyncio.create_task(app_mod._worker_loop(stop), name="worker")
        return await asyncio.wait_for(
            asyncio.gather(lane, worker, return_exceptions=True), timeout=timeout
        )

    return asyncio.run(drive())


def test_i1_lane_holds_a_stale_letter_confirmation_and_sends_nothing(ids, monkeypatch):
    """I1: an online letter whose confirmation no longer matches the configured model is held by
    the same check the worker used; the lane runs it and nothing is sent."""
    from kestrel.docs.letter_task import APPROVAL_HOLD

    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))
    _set("llm.tiers.tier3.model", "vendor/other-model")  # the person switched models since
    model = LetterModel(monkeypatch)
    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    task = _task_of("letter_draft")
    assert model.calls == 0, "nothing was sent on a stale confirmation"
    assert task.status == "pending" and task.last_error == APPROVAL_HOLD


def _set(key: str, value: object) -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value
            await s.commit()

    asyncio.run(go())


def test_i1_lane_runs_a_confirmed_story_build_exactly_once(ids, monkeypatch):
    """I1: a story build queued with a confirmation for the model configured now runs in the lane
    through the unchanged consent path."""
    _go_online("tier2")
    _queue_story(_consent("tier2"))
    story = StoryModel(monkeypatch)
    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    assert story.calls == 1
    task = _task_of("build_stories")
    assert task.status == "done"


def test_i1_lane_holds_an_unconfirmed_story_build(ids, monkeypatch):
    """I1: a build queued locally is held by the lane after its tier goes online; nothing is sent
    and the page shows the hold, not a silent send."""
    _queue_story(None)  # tier 2 is local now, so no confirmation is needed or stored
    _go_online("tier2")
    story = StoryModel(monkeypatch)
    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    assert story.calls == 0, "an unconfirmed build is never sent online"
    task = _task_of("build_stories")
    assert task.status == "done"  # the hold is recorded, not left "building" forever


def test_i2_letter_drafted_exactly_once_with_both_loops_running(ids, monkeypatch):
    """I2: the worker loop and the lane are both live against one queued online letter. Exactly
    one lease and exactly one draft (attempts 1, one render) -- never a second run by either loop.
    One draft may ask the model more than once (the plain-writing re-ask is part of the same
    draft), so the send is counted by leases and renders, not by raw model calls."""
    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))
    stop = asyncio.Event()
    model = LetterModel(monkeypatch, stop=stop)
    _run_both(monkeypatch, stop)
    task = _task_of("letter_draft")
    assert model.calls >= 1, "the draft reached the (fake) online model"
    assert task.attempts == 1, "both loops ran; exactly one lease, never a second run"
    assert len(_letter_renders(ids["strong"])) == 1, "drafted exactly once"
    assert task.status == "done"
    assert _runs() == [], "the lane drives the draft and opens no Run rows (D-WORKER-2 req 5)"


def test_i2_story_built_exactly_once_with_both_loops_running(ids, monkeypatch):
    """I2: both loops live against one queued online story build; it is built exactly once
    (one lease, one consolidation, the row ends done -- not pending or re-run by the other loop)."""
    _go_online("tier2")
    _queue_story(_consent("tier2"))
    stop = asyncio.Event()
    story = StoryModel(monkeypatch, stop=stop)
    _run_both(monkeypatch, stop)
    task = _task_of("build_stories")
    assert story.calls == 1, "both loops ran; exactly one build"
    assert task.attempts == 1, "exactly one lease"
    assert task.status == "done"
    assert _runs() == [], "the lane drives the build and opens no Run rows (D-WORKER-2 req 5)"


def test_the_lane_drives_a_multi_pass_story_build_to_completion(ids, monkeypatch):
    """The pass mechanism (write one group, give the task back, finish it on a later pass)
    works under the lane, not only when a test drives the drain by hand: with tier 2 online
    and passes forced on, the lane's repeated drains write one group per pass and hand the
    lease back, and a LATER lane pass finishes the build -- the lane never completes it in a
    single pass and never strands it. This is the same give-back path origin's
    test_story_build_passes exercises manually; here it is driven by the lane, each group
    sent exactly once (the watermark in the real consolidate_stories prevents a resend)."""
    from tests.test_interview_stories import _FakeClient
    from tests.test_stories_no_lock import _three_events

    _go_online("tier2")  # tier 3 stays local, so the lane drains only story builds here
    consent = _consent("tier2")  # confirmed for the online tier; computed outside the loop

    async def seed() -> None:
        sm = get_sessionmaker()
        await _three_events(sm)  # real sketches on 3 distinct axes: the build has 3 groups
        async with sm() as s:
            await story_task.request_build(s, consent=consent)

    asyncio.run(seed())

    # force the multi-pass path: every pass stops after one group and gives the task back
    monkeypatch.setattr(story_task, "_use_passes", lambda settings: True)
    monkeypatch.setattr(story_task, "PASS_SECONDS", 0.0)

    # the REAL consolidate_stories runs; only the model (embeddings + merges) is faked, so
    # the give-back / re-lease / watermark path is the production one, driven by the lane.
    base = _FakeClient()
    model = {"merges": 0}

    async def fake_embed(texts, **kw):
        return await base.embed(texts, **kw)

    async def fake_complete(**kw):
        model["merges"] += 1
        return await base.complete(**kw)

    monkeypatch.setattr(LLMClient, "embed", staticmethod(fake_embed))
    monkeypatch.setattr(LLMClient, "complete", staticmethod(fake_complete))

    stop = asyncio.Event()
    _run_lane(monkeypatch, stop, watchdog_s=8.0)
    task = _task_of("build_stories")
    assert task.status == "done", "a later lane pass finishes the build the earlier one gave back"
    # The watermark in the real consolidate_stories (verified by test_story_build_passes)
    # sends each group exactly once; the NEW behavior here is that the LANE is what drives
    # those passes: the first pass gave the lease back, and a later lane pass finished it.
    assert model["merges"] >= 2, "the build was split across 2+ lane passes, not done in one"


def test_the_worker_loop_skips_online_drains_for_the_lane(ids, monkeypatch):
    """The worker loop's skip is what hands online drafts to the lane: with tier 2 and tier 3
    online, a worker cycle alone drains neither the story build nor the letter (the lane owns
    them); with them local, the same worker cycle drains both, in today's place. The worker's
    own wait helper is patched, so the cycle ends on a watchdog rather than a real 8 s wait."""
    app_mod = _app()
    _go_online("tier2")
    _go_online("tier3")
    _queue_story(_consent("tier2"))
    _queue_letter(ids["strong"], _consent("tier3"))

    story = {"n": 0}
    letter = {"n": 0}

    async def story_spy(sm, client, settings, **k):
        story["n"] += 1

    async def letter_spy(*, stop=None, **k):
        letter["n"] += 1

    monkeypatch.setattr(story_task, "drain_story_builds", story_spy)
    monkeypatch.setattr(role_page, "run_letter_drafts", letter_spy)

    async def wait(stop_event: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)

    monkeypatch.setattr(app_mod, "_wait", wait)

    def run_cycle(timeout: float) -> None:
        stop = asyncio.Event()

        async def drive() -> None:
            async def watch() -> None:
                await asyncio.sleep(timeout)
                stop.set()

            w = asyncio.create_task(watch())
            try:
                await app_mod._worker_loop(stop)
            finally:
                w.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await w

        asyncio.run(drive())

    # online: the worker must not drain either, so the watchdog ends the cycle
    run_cycle(1.0)
    assert story["n"] == 0, "an online story build is left for the lane"
    assert letter["n"] == 0, "an online letter is left for the lane"
    assert _task_of("build_stories").status == "pending"
    assert _task_of("letter_draft").status == "pending"

    # local: the same worker cycle drains both, in today's place
    _go_local("tier2")
    _go_local("tier3")
    run_cycle(1.0)
    assert story["n"] >= 1, "a local story build still drains in the worker loop"
    assert letter["n"] >= 1, "a local letter still drains in the worker loop"


def test_i3_local_lane_drains_nothing_and_the_worker_order_is_untouched(ids, monkeypatch):
    """I3: with tiers 2 and 3 local the lane drains nothing, opens no Run rows, and the worker
    loop keeps origin's order (D-WORKER-1): the stories and letters it owns run right after
    scoring, before the background lane."""
    _queue_story(None)
    _queue_letter(ids["strong"])
    story_calls = {"n": 0}
    letter_calls = {"n": 0}

    async def story_spy(sm, client, settings, **k):
        story_calls["n"] += 1
        return 0

    async def letter_spy(*, stop=None, **k):
        letter_calls["n"] += 1
        return 0

    monkeypatch.setattr(story_task, "drain_story_builds", story_spy)
    monkeypatch.setattr(role_page, "run_letter_drafts", letter_spy)

    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    assert story_calls["n"] == 0 and letter_calls["n"] == 0, "a local lane drains nothing"
    assert _runs() == [], "the lane opens no Run rows"
    assert _newest_task().status == "pending", "the local drafts wait for the worker loop"

    # The worker loop keeps origin's order (D-WORKER-1): the drafts it owns run right after
    # scoring, before the background lane.
    order: list[str] = []

    async def tier(self, name, *a, **k):
        order.append(name)

    async def embed(self, stats, *a, **k):
        pass

    async def letters(*, stop=None, **k):
        order.append("letters")
        letter_calls["n"] += 1
        stop.set()
        return 0

    monkeypatch.setattr(Worker, "drain_tier", tier)
    monkeypatch.setattr(Worker, "drain_embed", embed)
    monkeypatch.setattr(role_page, "run_letter_drafts", letters)
    stop2 = asyncio.Event()
    asyncio.run(asyncio.wait_for(_app()._worker_loop(stop2), timeout=60.0))
    assert order == ["t1", "t2", "letters"], order
    assert story_calls["n"] == 1, "the local story build still drains in the worker loop"
    assert letter_calls["n"] == 1


def test_i4_a_lane_that_fails_setup_retries_until_it_recovers(ids, monkeypatch):
    """I4: the lane's setup (settings and client) is retried like the worker loop's: a setup
    that fails twice and then succeeds still drafts the letter in the same pass -- a lane that
    died on the first failure would never reach it."""
    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))

    attempts = {"n": 0}
    real_init = LLMClient.__init__

    def flaky_init(self, *a, **k):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("the settings file was unreadable")
        real_init(self, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(LLMClient, "__init__", flaky_init)
        m.setattr(_app(), "_SETUP_RETRY_SECONDS", 0.1)
        model = LetterModel(m)
        stop = asyncio.Event()
        _run_lane(m, stop, watchdog_s=3.0)
    assert attempts["n"] >= 3, "setup was retried, not given up on"
    assert model.calls >= 1, "the recovered pass drafted the letter"
    assert _task_of("letter_draft").status == "done"


def test_i4_a_lane_that_dies_at_setup_never_sends_and_a_restart_completes_once(ids, monkeypatch):
    """I4: a setup that keeps failing neither leases nor sends, so the letter waits, and the
    restart drafts it exactly once."""
    _go_online("tier3")
    _queue_letter(ids["second"], _consent("tier3"))

    def boom_init(self, *a, **k):
        raise RuntimeError("the settings file was unreadable")

    with monkeypatch.context() as m:
        m.setattr(LLMClient, "__init__", boom_init)
        m.setattr(_app(), "_SETUP_RETRY_SECONDS", 0.1)
        model = LetterModel(m)
        stop = asyncio.Event()
        _run_lane(m, stop, watchdog_s=0.8)
    assert model.calls == 0, "the dead pass never leased or sent"
    assert _newest_task().status == "pending"

    model2 = LetterModel(monkeypatch)
    stop2 = asyncio.Event()
    _run_lane(monkeypatch, stop2)
    task = _task_of("letter_draft")
    assert model2.calls >= 1, "the restart drafted it"
    assert task.attempts == 1, "the dead pass never leased it; the restart did, once"
    assert len(_letter_renders(ids["second"])) == 1
    assert task.status == "done"


def test_i5_online_to_local_letter_is_picked_up_by_the_worker_loop(ids, monkeypatch):
    """I5: a letter queued online, whose tier then went local, is not stranded by the loop that
    now skips it: the worker loop drafts it on its next cycle, exactly once, with the lane live."""
    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))
    _go_local("tier3")
    stop = asyncio.Event()
    model = LetterModel(monkeypatch, stop=stop)
    _run_both(monkeypatch, stop)
    task = _task_of("letter_draft")
    assert model.calls >= 1, "the local switch stranded nothing"
    assert task.attempts == 1, "drafted exactly once, by the loop that now owns it"
    assert len(_letter_renders(ids["strong"])) == 1
    assert task.status == "done"


def test_i5_local_to_online_letter_is_taken_by_the_lane_and_held(ids, monkeypatch):
    """I5: a letter queued locally, whose tier then went online, is taken up by the lane and held
    for a fresh confirmation instead of being stranded or sent."""
    from kestrel.docs.letter_task import APPROVAL_HOLD

    _queue_letter(ids["strong"])  # tier 3 local: no confirmation needed
    _go_online("tier3")
    model = LetterModel(monkeypatch)
    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    assert model.calls == 0, "a locally queued draft is never sent online by itself"
    task = _task_of("letter_draft")
    assert task.status == "pending" and task.last_error == APPROVAL_HOLD


def test_i6_an_exception_in_the_lane_does_not_stop_the_worker_loop(ids, monkeypatch):
    """I6: a failure in the lane's story drain is logged and the lane keeps going; the worker loop
    still completes its cycle and drains the local letter at its place."""
    _go_online("tier2")  # tier 3 stays local: its letter belongs to the worker loop
    _queue_story(_consent("tier2"))
    _queue_letter(ids["strong"])

    stop = asyncio.Event()
    calls = {"n": 0}

    async def boom(sm, client, settings, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        raise RuntimeError("the model process died")

    monkeypatch.setattr(story_task, "drain_story_builds", boom)
    _run_lane(monkeypatch, stop, watchdog_s=3.0)
    assert calls["n"] >= 2, "the lane kept running after its own exception"

    order: list[str] = []

    async def tier(self, name, *a, **k):
        order.append(name)

    async def embed(self, stats, *a, **k):
        pass

    async def letters(*, stop=None, **k):
        order.append("letters")
        stop.set()
        return 0

    monkeypatch.setattr(Worker, "drain_tier", tier)
    monkeypatch.setattr(Worker, "drain_embed", embed)
    monkeypatch.setattr(role_page, "run_letter_drafts", letters)
    stop2 = asyncio.Event()
    asyncio.run(asyncio.wait_for(_app()._worker_loop(stop2), timeout=60.0))
    assert order == ["t1", "t2", "letters"], order


def test_i6_an_exception_in_the_worker_loop_does_not_stop_the_lane(ids, monkeypatch):
    """I6: a failure in the worker loop's cycle never takes the lane with it; the lane drafts the
    queued online letter exactly once while the worker loop keeps cycling."""
    stop = asyncio.Event()
    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))

    scrapes = {"n": 0}

    async def boom(self, *a, **k):
        scrapes["n"] += 1
        raise RuntimeError("the board source died")

    monkeypatch.setattr(Worker, "drain_scrapes", boom)
    model = LetterModel(monkeypatch, stop=stop)
    _lane, worker = _run_both(monkeypatch, stop)
    task = _task_of("letter_draft")
    assert model.calls >= 1, "the lane sent the letter"
    assert task.attempts == 1, "drafted exactly once, by the lane"
    assert task.status == "done"
    assert worker is None, (
        "the worker loop exited cleanly: its own cycle exception was caught and the loop kept "
        "running (or stopped by the stop event), never crashed"
    )
    assert scrapes["n"] >= 1, "the failing board pass really ran"


def test_a_release_failure_never_stops_the_lane(ids, monkeypatch):
    """D-QUEUE-3 guard: the lane's own release pass can fail (a locked database); the failure
    is logged and the lane keeps draining, exactly as the worker loop guards its own cycle, so
    one bad write never stops online builds and drafts for the life of the process."""
    import kestrel.queue.queue as qmod

    releases = []

    async def _boom(sm, settings):
        releases.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(qmod, "release_holds", _boom)
    _go_online("tier2")
    _queue_story(_consent("tier2"))
    story = StoryModel(monkeypatch)
    stop = asyncio.Event()
    _run_lane(monkeypatch, stop)
    assert releases, "the lane really ran the release pass"
    assert story.calls == 1, "the release failure must not take the drain with it"
    task = _task_of("build_stories")
    assert task.status == "done"


def test_the_lane_honors_the_pause_switch(ids, monkeypatch):
    """A paused app queues but does not send: while paused the lane claims nothing, and the same
    task drains once the operator resumes."""
    app_mod = _app()
    _go_online("tier3")
    _queue_letter(ids["strong"], _consent("tier3"))

    app_mod.set_paused(True)
    try:
        model = LetterModel(monkeypatch)
        stop = asyncio.Event()
        _run_lane(monkeypatch, stop, watchdog_s=1.5)
        task = _task_of("letter_draft")
        assert model.calls == 0 and task.status == "pending"
    finally:
        app_mod.set_paused(False)

    model2 = LetterModel(monkeypatch)
    stop2 = asyncio.Event()
    _run_lane(monkeypatch, stop2)
    task = _task_of("letter_draft")
    assert model2.calls >= 1
    assert task.attempts == 1, "the paused pass never leased it; the resumed one did, once"
    assert task.status == "done"


def test_the_lane_builds_no_story_while_paused(ids, monkeypatch):
    """The story build has no pause check of its own: while paused the lane must not build it,
    and the same build runs exactly once after the operator resumes."""
    app_mod = _app()
    _go_online("tier2")
    _queue_story(_consent("tier2"))

    app_mod.set_paused(True)
    try:
        story = StoryModel(monkeypatch)
        _run_lane(monkeypatch, asyncio.Event(), watchdog_s=1.5)
        task = _task_of("build_stories")
        assert story.calls == 0 and task.status == "pending"
    finally:
        app_mod.set_paused(False)

    story2 = StoryModel(monkeypatch)
    stop2 = asyncio.Event()
    _run_lane(monkeypatch, stop2)
    task = _task_of("build_stories")
    assert story2.calls >= 1, "the resumed lane built it"
    assert task.attempts == 1, "the paused pass never leased it; the resumed one did, once"
    assert task.status == "done"


def test_the_lane_backs_off_when_idle_and_returns_to_two_seconds_when_online(ids, monkeypatch):
    """Idle back-off (D-WORKER-3): with neither Tier 2 nor Tier 3 online the lane has nothing to
    drain, so it waits ~15 s between settings reads; the moment either is online it returns to
    2 s so a queued draft is picked up quickly. Recorded on the lane's own wait helper."""
    app_mod = _app()
    waits: list[float] = []

    async def wait(stop_event: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=0.05)

    monkeypatch.setattr(app_mod, "_wait", wait)

    def run_lane() -> None:
        stop = asyncio.Event()

        async def drive() -> None:
            async def watch() -> None:
                await asyncio.sleep(1.0)
                stop.set()

            w = asyncio.create_task(watch())
            try:
                await app_mod._online_lane(stop)
            finally:
                w.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await w

        asyncio.run(drive())

    # neither online: the lane drains nothing, so it backs off to 15 s
    _go_local("tier2")
    _go_local("tier3")
    waits.clear()
    run_lane()
    assert waits and waits[-1] == pytest.approx(15.0), f"idle lane should wait ~15 s, got {waits}"

    # Tier 3 online: the lane has work, so it returns to 2 s
    _go_online("tier3")
    waits.clear()
    run_lane()
    assert waits and waits[-1] == pytest.approx(2.0), f"online lane should wait ~2 s, got {waits}"
