"""Issue #250 (D-SCORE-919 as amended): the legacy lane recheck decides from the task row and
the role as they are AT THE WRITE, never from a copy read earlier.

The lane sweep lists the waiting tasks once and the worker holds the payload it leased. A
person's Score again or Try again can commit after that read. Each test here pauses the recheck
right after its read, commits the person's request from another session, lets the recheck go
on, and requires that the request survives: the role is not set aside, the task is not
finished, and nothing the request wrote is overwritten. All text is invented; no model, no
network, no sleeping: the pause is a hook at the exact point, not a timer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Task
from kestrel.pipeline import lane
from kestrel.pipeline._shared import HandlerOutcome
from kestrel.pipeline.tasks import enqueue_once
from kestrel.pipeline.worker import TierStats, Worker
from kestrel.queue.rescore import enqueue_rescore

pytestmark = pytest.mark.usefixtures("example_operator_profile")

MISMATCH, MATCH = "Pastry Chef", "Operations Analyst"


async def _lane_role(
    sm, title: str = MISMATCH, stage: str = "t0_pass", **task_kw
) -> tuple[int, int]:
    """A role that went through the lane, with its waiting lane Quick sort task."""
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=title,
                        org_name="Fictional Org", stage=stage, stage_reason="seeded",
                        seen_count=1, lane_local_only=1)
        s.add(o)
        await s.flush()
        task = Task(type="t1", payload={"opportunity_id": o.id, **lane.lane_extra(0.5)},
                    priority=lane.LANE_PRIORITY, **task_kw)
        s.add(task)
        await s.commit()
        return o.id, task.id


async def _score_again(sm, oid: int) -> None:
    """The person presses Score again: its own session, committed."""
    async with sm() as s:
        result = await enqueue_rescore(s, [oid])
        await s.commit()
    assert result.queued == [oid]  # the page says queued


async def _try_again(sm, oid: int) -> None:
    """A retry that finds the task already live: the plain re-queue stamps it requested."""
    async with sm() as s:
        assert await enqueue_once(s, "t1", oid, extra={"requested": True}) is False
        await s.commit()


async def _state(sm, oid: int, tid: int) -> tuple[Opportunity, Task]:
    async with sm() as s:
        return await s.get(Opportunity, oid), await s.get(Task, tid)


def _after(monkeypatch, name: str, then):
    """Run `then()` once, right after `lane.<name>` returns: the pause the review described."""
    real = getattr(lane, name)
    fired = []

    async def hooked(*args, **kwargs):
        out = await real(*args, **kwargs)
        if not fired:
            fired.append(1)
            await then()
        return out

    monkeypatch.setattr(lane, name, hooked)
    return fired


# ------------------------------------------------------------------ the sweep
@pytest.mark.parametrize("click", [_score_again, _try_again], ids=["score_again", "try_again"])
@pytest.mark.parametrize("pause_after", ["_pending_lane_tasks", "_fresh_task"])
async def test_a_request_that_lands_during_the_sweep_is_never_swallowed(
        sm, monkeypatch, click, pause_after):
    """`_pending_lane_tasks`: after the sweep listed the task (the review's check).
    `_fresh_task`: later still, between the fresh read and the compare that takes the lock."""
    oid, tid = await _lane_role(sm)

    async def person():
        await click(sm, oid)

    fired = _after(monkeypatch, pause_after, person)
    await lane.release_due(sm, hosted=True)  # hosted: the recheck runs, nothing is released
    assert fired
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, opp.stage_reason) == ("t0_pass", "seeded")
    assert task.status == "pending"
    assert lane.asked_by_person(task.payload)  # the request is still on the row


async def test_a_change_that_is_not_a_request_does_not_stop_the_recheck(sm, monkeypatch):
    """The other side of the compare: something edits the listed row without asking for a score
    (here its lane priority). The compare is lost, the row is read again as stored, and the
    mismatch is still set aside in the same pass, decided from the new payload."""
    oid, tid = await _lane_role(sm)

    async def edit():
        async with sm() as s:
            row = await s.get(Task, tid)
            row.payload = {**row.payload, "lane_priority": 77}
            await s.commit()

    fired = _after(monkeypatch, "_pending_lane_tasks", edit)
    await lane.release_due(sm, hosted=True)
    assert fired
    opp, task = await _state(sm, oid, tid)
    assert opp.stage == "t0_reject" and opp.stage_reason.startswith("unrelated_title")
    assert task.status == "done" and task.payload["lane_priority"] == 77


async def test_notes_written_during_the_sweep_protect_the_role(sm, monkeypatch):
    from kestrel.interview import notes

    oid, tid = await _lane_role(sm)

    async def person():
        async with sm() as s:
            await notes.save_notes(s, oid, "Invented notes.")
            await s.commit()

    _after(monkeypatch, "_pending_lane_tasks", person)
    await lane.release_due(sm, hosted=True)
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, task.status) == ("t0_pass", "pending")


async def test_without_a_request_the_sweep_still_sets_a_mismatch_aside(sm):
    oid, tid = await _lane_role(sm)
    await lane.release_due(sm, hosted=True)
    opp, task = await _state(sm, oid, tid)
    assert opp.stage == "t0_reject" and opp.stage_reason.startswith("unrelated_title")
    assert task.status == "done"


async def test_a_release_never_overwrites_a_request_that_landed_since_the_list(sm, monkeypatch):
    """A role that matches is released by editing the stored payload in place. A Score again that
    committed after the list was read took the task out of the lane: it is left exactly as the
    request wrote it, and is not counted as released."""
    asked, asked_tid = await _lane_role(sm, title=MATCH)
    plain, plain_tid = await _lane_role(sm, title=MATCH)
    written: dict = {}

    async def person():
        await _score_again(sm, asked)
        async with sm() as s:
            written.update((await s.get(Task, asked_tid)).payload)

    _after(monkeypatch, "_pending_lane_tasks", person)
    freed = await lane.release_all(sm)
    assert freed == 1  # only the one still waiting in the lane
    _, task = await _state(sm, asked, asked_tid)
    assert task.payload == written and task.payload["rescore"] is True
    assert "lane" not in task.payload and lane.LOCAL_ONLY not in task.payload
    assert task.priority >= 1000  # the request's priority, not the lane's
    _, other = await _state(sm, plain, plain_tid)
    assert other.payload == {"opportunity_id": plain, "lane": 0, lane.LOCAL_ONLY: True}
    assert other.priority == 50  # the priority it carried into the lane (0.5 * 100)


async def test_a_daily_release_stamps_the_day_and_keeps_to_its_room(sm):
    first, first_tid = await _lane_role(sm, title=MATCH)
    second, second_tid = await _lane_role(sm, title=MATCH)
    assert await lane.release_due(sm, cap=1) == 1
    _, a = await _state(sm, first, first_tid)
    _, b = await _state(sm, second, second_tid)
    assert a.payload["lane"] == 0 and a.payload["lane_released_on"] == lane._today()
    assert b.payload["lane"] == 1 and "lane_released_on" not in b.payload


# ------------------------------------------------------------------ the worker
async def _drain(sm, handler, task_type: str = "t1", **worker_kw) -> None:
    settings = get_settings()
    worker = Worker(sm, SimpleNamespace(settings=settings), settings)
    for key, value in worker_kw.items():
        setattr(worker, key, value)
    await worker.drain_tier(task_type, 1, handler, TierStats())


async def _released_role(sm, title: str = MISMATCH) -> tuple[int, int]:
    """A lane role whose task has been released: an ordinary pending Quick sort task."""
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=title,
                        org_name="Fictional Org", stage="t0_pass", stage_reason="seeded",
                        seen_count=1, lane_local_only=1)
        s.add(o)
        await s.flush()
        # No lane key left on the payload: the worker learns the role's lane origin from the
        # role itself (lane.prior_lane), which is where the tests below pause it.
        task = Task(type="t1", payload={"opportunity_id": o.id}, priority=10)
        s.add(task)
        await s.commit()
        return o.id, task.id


@pytest.mark.parametrize("pause_after", ["prior_lane", "_fresh_task"])
async def test_a_request_that_lands_after_the_lease_is_never_swallowed_by_the_worker(
        sm, monkeypatch, pause_after):
    """The worker leased an ordinary task of a mismatched lane role; the person's retry stamps
    that leased row before the recheck writes. The handler must run, with the request."""
    oid, tid = await _released_role(sm)

    async def person():
        await _try_again(sm, oid)

    fired = _after(monkeypatch, pause_after, person)
    handler = AsyncMock(return_value=HandlerOutcome("skip", 1))
    await _drain(sm, handler)
    assert fired
    handler.assert_awaited_once()
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, opp.stage_reason) == ("t0_pass", "seeded")
    assert task.status == "done"  # finished by the handler's run, not by the recheck
    assert task.payload.get("requested") is True  # not wiped by the worker's own stamp
    assert task.payload.get(lane.LOCAL_ONLY) is True


async def test_the_handler_is_told_of_a_request_that_landed_after_the_lease(sm, monkeypatch):
    """A failed lane role with an ordinary Fit score task; the person's retry stamps the leased
    row. The handler runs on the payload as stored now, so it is told the person asked."""
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=MISMATCH,
                        org_name="Fictional Org", stage="quarantined", stage_reason="seeded",
                        seen_count=1, lane_local_only=1)
        s.add(o)
        await s.flush()
        task = Task(type="t2", payload={"opportunity_id": o.id}, priority=10)
        s.add(task)
        await s.commit()
        oid, tid = o.id, task.id

    async def person():
        async with sm() as s:
            assert await enqueue_once(s, "t2", oid, extra={"requested": True}) is False
            await s.commit()

    _after(monkeypatch, "prior_lane", person)
    handler = AsyncMock(return_value=HandlerOutcome("skip", 2))
    await _drain(sm, handler, "t2")
    handler.assert_awaited_once()
    assert handler.await_args.kwargs.get("requested") is True
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, opp.stage_reason) == ("quarantined", "seeded")
    assert task.payload.get("requested") is True


async def test_the_workers_stamps_keep_what_a_request_wrote_after_the_lease(sm, monkeypatch):
    """A role that matches: no rejection, but the worker still stamps the row (local only, the
    run's online approval). Both are written in place over whatever the row holds by then."""
    oid, tid = await _released_role(sm, title=MATCH)

    async def person():
        await _try_again(sm, oid)

    _after(monkeypatch, "prior_lane", person)
    seen: dict = {}

    async def handler(sm_, client, settings, **kwargs):
        async with sm() as s:
            seen.update((await s.get(Task, tid)).payload)
        return HandlerOutcome("skip", 1)

    approval = {"tier2": ["local", "invented-model"]}
    await _drain(sm, handler, _online_approval=approval)
    assert seen.get("requested") is True
    assert seen.get(lane.LOCAL_ONLY) is True
    assert seen.get("online_approval") == approval
    assert seen.get("opportunity_id") == oid


async def test_without_a_request_the_worker_still_sets_a_mismatch_aside(sm):
    oid, tid = await _released_role(sm)
    handler = AsyncMock(side_effect=AssertionError("a mismatch must never be scored"))
    await _drain(sm, handler)
    handler.assert_not_awaited()
    opp, task = await _state(sm, oid, tid)
    assert opp.stage == "t0_reject" and task.status == "done"
    assert task.worker_id is None and task.lease_until is None


# ------------------------------------------------------------------ the one owning place
async def test_settle_recheck_reports_a_row_that_is_no_longer_the_callers(sm):
    oid, tid = await _lane_role(sm)
    await _score_again(sm, oid)  # out of the lane, asked for
    async with sm() as s:
        outcome, payload = await lane.settle_recheck(s, tid)
        await s.commit()
    assert outcome == lane.CHANGED and lane.asked_by_person(payload)
    async with sm() as s:
        assert await lane.settle_recheck(s, tid, worker_id="someone-else") == (
            lane.CHANGED, payload)
        assert await lane.settle_recheck(s, 10_000) == (lane.CHANGED, None)
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, task.status) == ("t0_pass", "pending")


async def test_settle_recheck_decides_nothing_from_a_read_that_never_matches_the_row(
        sm, monkeypatch):
    """Whatever the read returns, the compare is against the stored row. A read that does not
    match it (here a stale copy, every time) decides nothing and writes nothing."""
    oid, tid = await _lane_role(sm)
    reads = []

    async def stale(session, task_id):
        reads.append(task_id)
        return SimpleNamespace(status="pending", worker_id=None,
                               payload={"opportunity_id": oid, "lane": 1, "lane_priority": 1})

    monkeypatch.setattr(lane, "_fresh_task", stale)
    async with sm() as s:
        assert await lane.settle_recheck(s, tid) == (lane.CHANGED, None)
        await s.commit()
    assert len(reads) == lane._COMPARE_TRIES
    opp, task = await _state(sm, oid, tid)
    assert (opp.stage, opp.stage_reason, task.status) == ("t0_pass", "seeded", "pending")


async def test_stamp_leased_writes_only_a_row_the_worker_holds(sm):
    oid, tid = await _released_role(sm, title=MATCH)
    async with sm() as s:
        assert await lane.stamp_leased(s, tid, "w-1", "note", True) is None  # pending: not held
        task = await s.get(Task, tid)
        task.status, task.worker_id = "leased", "w-1"
        await s.commit()
    async with sm() as s:
        assert await lane.stamp_leased(s, tid, "w-2", "note", True) is None  # another worker's
        got = await lane.stamp_leased(s, tid, "w-1", "note", {"a": [1, 2]})
        await s.commit()
    assert got == {"opportunity_id": oid, "note": {"a": [1, 2]}}
