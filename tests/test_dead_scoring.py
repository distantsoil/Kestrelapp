"""Issue 196 follow-ups in the worker (D-WORKER-5 to D-WORKER-9). Invented text only.

Invariants proven here:
(a) Uncertainty escalates, never rejects: a role whose scoring task died is never dropped or
    shown as rejected; it is quarantined with the error kept and is on Needs another try.
(b) Resumability: a process killed at any moment (a lease left behind) continues cleanly on the
    next start or the next cycle, with every unit of work a task row and nothing scored twice.
(c) The sweep runs on the worker loop, every cycle, before anything is leased.
"""

from __future__ import annotations

import asyncio
import html as htmllib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db.base import iso, utcnow
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Opportunity, Quarantine, Task
from kestrel.pipeline._shared import HandlerOutcome
from kestrel.pipeline.deadletter import QUARANTINED_KEY
from kestrel.pipeline.worker import TierStats, Worker
from kestrel.services import tier0_override
from kestrel.web import app as app_module
from kestrel.web.fit import DEAD_TASK_MARK
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

REFUSED = "httpx.ConnectError: [Errno 111] Connection refused"


@pytest.fixture
def unpaused():
    app_module.set_paused(False)
    yield
    app_module.set_paused(False)


async def _run(fn):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await fn(sm)
    finally:
        await engine.dispose()


def run(fn):
    return asyncio.run(_run(fn))


def _seed_role(**opp_fields) -> int:
    """The shared Pipeline seed's role d (no score yet), with `opp_fields` applied."""
    oid = asyncio.run(T._seed())["d"]

    async def go(sm):
        async with sm() as s:
            opp = await s.get(Opportunity, oid)
            for k, v in opp_fields.items():
                setattr(opp, k, v)
            await s.commit()

    run(go)
    return oid


def _add_task(oid: int, **fields) -> int:
    async def go(sm):
        async with sm() as s:
            payload = {"opportunity_id": oid, **fields.pop("payload", {})}
            t = Task(payload=payload, **fields)
            s.add(t)
            await s.commit()
            return t.id

    return run(go)


def _worker(sm) -> Worker:
    settings = get_settings()
    return Worker(sm, SimpleNamespace(settings=settings), settings)


def _state(oid: int) -> dict:
    async def go(sm):
        async with sm() as s:
            opp = await s.get(Opportunity, oid)
            tasks = (await s.execute(
                select(Task).where(Task.opportunity_id == oid).order_by(Task.id))).scalars().all()
            qs = (await s.execute(
                select(Quarantine).where(Quarantine.opportunity_id == oid))).scalars().all()
            return {
                "stage": opp.stage,
                "tasks": [(t.type, t.status, int(t.attempts or 0), dict(t.payload or {}))
                          for t in tasks],
                "quarantine": [(q.tier, q.error, q.attempts) for q in qs],
            }

    return run(go)


def _row(oid: int) -> str:
    with T._client() as c:
        html = T._get(c, "/opportunities")
    return htmllib.unescape(T._row(html, oid))


def _needs_another_try() -> str:
    with T._client() as c:
        return htmllib.unescape(c.get("/quarantine").text)


# ------------------------------------------------------------------ item 1: quarantine on dead
def test_a_task_that_uses_its_last_try_puts_the_role_in_needs_another_try(unpaused):
    """The real drain: the model cannot be reached on the last try, so the task dead-letters and,
    in the same pass, the role is quarantined with the error kept, never dropped or rejected."""
    oid = _seed_role(stage="t0_pass")
    tid = _add_task(oid, type="t1", status="pending", attempts=2, max_attempts=3)
    calls: list[int] = []

    async def handler(sm, client, settings, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        return HandlerOutcome("error", 1, error=REFUSED)

    async def go(sm):
        await _worker(sm).drain_tier("t1", 1, handler, TierStats())

    run(go)
    st = _state(oid)
    assert calls == [oid]
    assert ("t1", "dead", 3) == st["tasks"][0][:3] and QUARANTINED_KEY in st["tasks"][0][3]
    assert st["stage"] == "quarantined"
    assert len(st["quarantine"]) == 1
    tier, error, attempts = st["quarantine"][0]
    assert tier == 1 and attempts == 3
    assert error == DEAD_TASK_MARK + REFUSED  # the full error is kept, not a summary
    row = _row(oid)
    assert 'data-scoring="failed"' in row
    assert "Kestrel could not reach the model." in row
    assert f"/rescore/choose?opp_ids={oid}" in row  # Score again
    assert "Not a fit" not in row
    page = _needs_another_try()
    assert f"/opportunities/{oid}" in page
    assert tid  # the task row itself stays: the raw state is preserved
    assert st["tasks"][0][3]["opportunity_id"] == oid


def test_a_crash_on_the_last_try_is_quarantined_too(unpaused):
    oid = _seed_role(stage="t1_pass")
    _add_task(oid, type="t2", status="pending", attempts=2, max_attempts=3)

    async def handler(*a, **kw):
        raise RuntimeError("the invented handler fell over")

    async def go(sm):
        await _worker(sm).drain_tier("t2", 1, handler, TierStats())

    run(go)
    st = _state(oid)
    assert st["stage"] == "quarantined"
    assert [q[0] for q in st["quarantine"]] == [2]
    assert "the invented handler fell over" in st["quarantine"][0][1]


def test_a_retry_left_is_not_quarantined(unpaused):
    """Only the last try quarantines: with tries left the task backs off and waits."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="pending", attempts=0, max_attempts=3)

    async def handler(*a, **kw):
        return HandlerOutcome("error", 1, error=REFUSED)

    async def go(sm):
        await _worker(sm).drain_tier("t1", 1, handler, TierStats())

    run(go)
    st = _state(oid)
    assert st["tasks"][0][1] == "pending" and st["quarantine"] == []
    assert st["stage"] == "t0_pass"


def test_kill_on_the_last_try_then_restart_quarantines_once(unpaused):
    """Killed mid-task on its last try: the lease is left behind. The next start dead-letters
    it and quarantines the role; starting again (or every later cycle) adds nothing."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="leased", attempts=3, max_attempts=3,
              worker_id="pipeline-t1-0", lease_until=iso(utcnow() - timedelta(minutes=5)),
              last_error=REFUSED)

    async def go(sm):
        await _worker(sm).reclaim()  # the restart
        await _worker(sm).reclaim()  # and another
        await _worker(sm).sweep()    # and a cycle

    run(go)
    st = _state(oid)
    assert [t[1] for t in st["tasks"]] == ["dead"]
    assert st["stage"] == "quarantined"
    assert len(st["quarantine"]) == 1


def test_a_scored_role_whose_score_again_died_keeps_its_score(unpaused):
    """A role with a reading of its own keeps it; the failure is kept as a failed score-again."""
    from kestrel.pipeline._shared import parse_rescore_failure

    oid = _seed_role(stage="t2_done")
    tid = _add_task(oid, type="t2", status="dead", attempts=3, max_attempts=3,
                    last_error=REFUSED, payload={"rescore": True})

    run(lambda sm: _worker(sm).sweep())
    st = _state(oid)
    assert st["stage"] == "t2_done"
    assert len(st["quarantine"]) == 1
    is_rescore, task_id, rest = parse_rescore_failure(st["quarantine"][0][1])
    assert is_rescore and task_id == tid and rest == DEAD_TASK_MARK + REFUSED


def test_hidden_roles_and_older_dead_tasks_are_left_alone(unpaused):
    hidden = _seed_role(stage="t0_pass", archived_at="2026-01-01T00:00:00+00:00",
                        archive_reason="not_for_me")
    _add_task(hidden, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def other(sm):
        async with sm() as s:
            opp = Opportunity(track="employment", opp_type="job_posting",
                              title="Invented Planner", stage="t1_pass")
            s.add(opp)
            await s.commit()
            return opp.id

    later = run(other)
    _add_task(later, type="t1", status="dead", attempts=3, last_error=REFUSED)
    # A live score-again of another type: the role is still in hand, so nothing is quarantined.
    _add_task(later, type="rescore", status="pending")

    # An old dead Tier 1 task with a newer Tier 2 task that finished: the role moved on.
    moved = run(other)
    _add_task(moved, type="t1", status="dead", attempts=3, last_error=REFUSED)
    _add_task(moved, type="t2", status="done", attempts=1)

    run(lambda sm: _worker(sm).sweep())
    assert _state(hidden)["quarantine"] == [] and _state(hidden)["stage"] == "t0_pass"
    assert _state(later)["quarantine"] == [] and _state(later)["stage"] == "t1_pass"
    assert _state(moved)["quarantine"] == [] and _state(moved)["stage"] == "t1_pass"


# --------------------------------------------- item 4: an overridden rules role whose t1 died
def test_an_overridden_rules_role_whose_tier1_died_reads_scoring_failed(unpaused):
    """Score this role anyway (D-UI-74), then its Tier 1 task died. It used to read as set aside
    by the rules (and dead_final elsewhere) while the startup sweep re-queued it. Now the read is
    what happens: it is in Needs another try, nothing re-queues it behind the person's back, and
    it never reads as rejected."""
    reason = "blocker:title: invented title rule"
    oid = _seed_role(stage="t0_reject", stage_reason=reason)

    async def mark(sm):
        async with sm() as s:
            await tier0_override.record(s, oid, reason)
            await s.commit()

    run(mark)
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED,
              payload={"rescore": True, "requested": True, "tier0_override": True})

    run(lambda sm: _worker(sm).sweep())
    st = _state(oid)
    assert st["stage"] == "quarantined"
    assert [t[1] for t in st["tasks"]] == ["dead"], "the orphan sweep queued nothing more"
    row = _row(oid)
    assert 'data-scoring="failed"' in row and 'data-scoring="dead_final"' not in row
    assert "rule" not in row.split('data-testid="row-why"')[-1].split("</div>")[0]
    assert 'data-group="not_fit"' not in row
    assert f"/opportunities/{oid}" in _needs_another_try()


# ------------------------------------------------ item 3: the orphan sweep runs every cycle
def test_a_lease_left_by_a_killed_worker_is_requeued_by_the_cycle_sweep_and_scored_once(
    unpaused,
):
    """A worker that died mid-task left its lease (tries remain). The cycle sweep returns it to
    the queue without a restart; a second sweep changes nothing; the drain then scores it once.
    A lease that has not expired (a drain still working) is never taken."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="leased", attempts=1, max_attempts=3,
              worker_id="pipeline-t1-0", lease_until=iso(utcnow() - timedelta(minutes=5)))

    async def other(sm):
        async with sm() as s:
            opp = Opportunity(track="employment", opp_type="job_posting",
                              title="Invented Steward", stage="t0_pass")
            s.add(opp)
            await s.commit()
            return opp.id

    busy = run(other)
    _add_task(busy, type="t1", status="leased", attempts=1, worker_id="pipeline-t1-1",
              lease_until=iso(utcnow() + timedelta(minutes=5)))
    calls: list[int] = []

    async def handler(sm, client, settings, *, opportunity_id, profiles, **kw):
        calls.append(opportunity_id)
        return HandlerOutcome("t1_pass", 1)

    async def go(sm):
        w = _worker(sm)
        assert await w.sweep() == 1
        assert await w.sweep() == 0  # idempotent
        await w.drain_tier("t1", 1, handler, TierStats())

    run(go)
    assert calls == [oid], "scored exactly once, and the live lease was not taken"
    assert [t[1] for t in _state(oid)["tasks"]] == ["done"]
    assert [t[1] for t in _state(busy)["tasks"]] == ["leased"]


def test_the_worker_loop_sweeps_every_cycle_not_only_at_startup(monkeypatch, unpaused):
    """The app's loop calls the sweep at the start of each cycle (two cycles, two sweeps), on
    its own event loop, after the startup reclaim."""
    from kestrel.pipeline import worker as worker_mod

    asyncio.run(T._seed())  # a migrated database, as the app has
    stop = asyncio.Event()
    order: list[str] = []

    async def reclaim(self) -> int:
        order.append("reclaim")
        return 0

    async def sweep(self) -> int:
        order.append("sweep")
        assert asyncio.get_running_loop() is loop[0]
        if order.count("sweep") >= 2:
            stop.set()
        return 0

    async def no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(worker_mod.Worker, "reclaim", reclaim)
    monkeypatch.setattr(worker_mod.Worker, "sweep", sweep)
    monkeypatch.setattr(app_module, "_wait", no_wait)
    loop: list = []

    async def drive():
        loop.append(asyncio.get_running_loop())
        await asyncio.wait_for(app_module._worker_loop(stop), timeout=60.0)

    asyncio.run(drive())
    assert order[0] == "reclaim" and order.count("reclaim") == 1
    assert order.count("sweep") >= 2


def test_counts_hold_after_the_sweep(unpaused):
    """Nothing is added for a role already in Needs another try: no second quarantine row, no
    task re-queued, across many cycles."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def go(sm):
        w = _worker(sm)
        for _ in range(4):
            await w.sweep()
        async with sm() as s:
            return (
                await s.execute(select(func.count()).select_from(Quarantine)
                                .where(Quarantine.opportunity_id == oid))
            ).scalar_one(), (
                await s.execute(select(func.count()).select_from(Task)
                                .where(Task.opportunity_id == oid))
            ).scalar_one()

    assert run(go) == (1, 1)


def test_two_sweeps_at_once_write_one_quarantine_row(unpaused):
    """The mark is a conditional UPDATE in the same commit as the row: two sweeps that both read
    the dead task before either writes still quarantine it once."""
    from kestrel.pipeline.deadletter import quarantine_dead

    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def go(sm):
        await asyncio.gather(quarantine_dead(sm), quarantine_dead(sm), quarantine_dead(sm))

    run(go)
    assert len(_state(oid)["quarantine"]) == 1


def test_the_cycle_sweep_leaves_other_leases_to_their_owners(unpaused):
    """The per-cycle reclaim touches only Tier 1 and Tier 2: a letter draft the online lane holds
    (it renews its own lease and runs beside the worker loop) is never taken from it."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="letter_draft", status="leased", attempts=1, worker_id="lane-0",
              lease_until=iso(utcnow() - timedelta(minutes=5)))
    run(lambda sm: _worker(sm).sweep())
    letters = [t[:2] for t in _state(oid)["tasks"] if t[0] == "letter_draft"]
    assert letters == [("letter_draft", "leased")]


def _between_read_and_write(monkeypatch, act) -> None:
    """Run `act(sm)` on a second connection after the sweep has read its candidates and before
    it writes, the moment a Score again (or a hide) from a page could commit."""
    from kestrel.pipeline import deadletter

    real_one = deadletter._one
    done = {"n": 0}

    async def one(s, task, model_for):
        if not done["n"]:
            done["n"] += 1

            async def other(sm):
                await act(sm)

            engine = make_async_engine(paths.db_path())
            try:
                await other(async_sessionmaker(engine, expire_on_commit=False))
            finally:
                await engine.dispose()
        return await real_one(s, task, model_for)

    monkeypatch.setattr(deadletter, "_one", one)


def test_a_score_again_committed_after_the_read_is_never_overridden(monkeypatch, unpaused):
    """D-WORKER-5 amended: the person presses Score again after the sweep read the dead task and
    before it wrote. The write re-checks "nothing live for this role", so the role is not moved
    to quarantined over the new request and no row is added."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def score_again(sm):
        async with sm() as s:
            s.add(Task(type="t1", status="pending",
                       payload={"opportunity_id": oid, "rescore": True, "requested": True}))
            await s.commit()

    _between_read_and_write(monkeypatch, score_again)
    run(lambda sm: _worker(sm).sweep())
    st = _state(oid)
    assert st["quarantine"] == [] and st["stage"] == "t0_pass"
    assert [(t[0], t[1]) for t in st["tasks"]] == [("t1", "dead"), ("t1", "pending")]
    assert QUARANTINED_KEY not in st["tasks"][0][3]


def test_a_role_hidden_after_the_read_is_left_alone(monkeypatch, unpaused):
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def hide(sm):
        async with sm() as s:
            opp = await s.get(Opportunity, oid)
            opp.archived_at, opp.archive_reason = "2026-01-01T00:00:00+00:00", "not_for_me"
            await s.commit()

    _between_read_and_write(monkeypatch, hide)
    run(lambda sm: _worker(sm).sweep())
    assert _state(oid)["quarantine"] == [] and _state(oid)["stage"] == "t0_pass"


def test_a_hidden_roles_dead_task_is_not_read_again_each_cycle(monkeypatch, unpaused):
    """D-WORKER-5 amended: the sweep runs every cycle, so a hidden role's dead task is filtered
    in the query and never handed to the per-task step at all."""
    from kestrel.pipeline import deadletter

    hidden = _seed_role(stage="t0_pass", archived_at="2026-01-01T00:00:00+00:00",
                        archive_reason="not_for_me")
    _add_task(hidden, type="t1", status="dead", attempts=3, last_error=REFUSED)
    seen: list[int] = []
    real_one = deadletter._one

    async def one(s, task, model_for):
        seen.append(task.id)
        return await real_one(s, task, model_for)

    monkeypatch.setattr(deadletter, "_one", one)

    async def go(sm):
        w = _worker(sm)
        await w.sweep()
        await w.sweep()

    run(go)
    assert seen == []
    assert _state(hidden)["quarantine"] == []


def test_a_dead_task_behind_a_live_score_again_is_not_read_each_cycle(monkeypatch, unpaused):
    """While a Score again waits, the old dead task is filtered in the query, not handed to the
    per-task step every cycle only to be refused at the write."""
    from kestrel.pipeline import deadletter

    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)
    _add_task(oid, type="rescore", status="pending")
    seen: list[int] = []
    real_one = deadletter._one

    async def one(s, task, model_for):
        seen.append(task.id)
        return await real_one(s, task, model_for)

    monkeypatch.setattr(deadletter, "_one", one)
    run(lambda sm: _worker(sm).sweep())
    assert seen == [] and _state(oid)["quarantine"] == []


def test_a_score_again_that_already_finished_after_the_read_is_never_overridden(
    monkeypatch, unpaused
):
    """D-WORKER-5 amended twice: a Score again from another process (the CLI) ran to the end
    between the sweep's read and its write. Nothing is live by then, but a newer scoring task
    exists, so the old dead task is history: no row, no stage change."""
    oid = _seed_role(stage="t0_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)

    async def finished_elsewhere(sm):
        async with sm() as s:
            s.add(Task(type="t1", status="done", attempts=1,
                       payload={"opportunity_id": oid, "rescore": True, "requested": True}))
            (await s.get(Opportunity, oid)).stage = "t1_pass"
            await s.commit()

    _between_read_and_write(monkeypatch, finished_elsewhere)
    run(lambda sm: _worker(sm).sweep())
    st = _state(oid)
    assert st["quarantine"] == [] and st["stage"] == "t1_pass"
    assert QUARANTINED_KEY not in st["tasks"][0][3]


def test_an_old_dead_task_with_a_newer_one_is_not_read_each_cycle(monkeypatch, unpaused):
    """The role moved on (a newer scoring task ran): its old dead task is filtered in the query,
    not handed to the per-task step every cycle only to be refused at the write."""
    from kestrel.pipeline import deadletter

    oid = _seed_role(stage="t1_pass")
    _add_task(oid, type="t1", status="dead", attempts=3, last_error=REFUSED)
    _add_task(oid, type="t2", status="done", attempts=1)
    seen: list[int] = []
    real_one = deadletter._one

    async def one(s, task, model_for):
        seen.append(task.id)
        return await real_one(s, task, model_for)

    monkeypatch.setattr(deadletter, "_one", one)
    run(lambda sm: _worker(sm).sweep())
    assert seen == [] and _state(oid)["quarantine"] == []
