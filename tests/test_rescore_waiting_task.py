"""Score again on a role that already has a live scoring task (issues #144 and #143).

A pending ordinary task is turned into the operator's request (rescore, requested_at, score_stub,
model pick, an approval confirmed in this request), as a Tier 0 "score anyway" already did. A
leased task is running on its own payload and is reported as running. Nothing here changes a
stage, and an upgrade never adds an online approval the operator did not confirm in the request.
The model is faked; nothing reaches a network. Invented text only."""

from __future__ import annotations

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Task
from kestrel.pipeline.worker import Worker
from kestrel.queue.rescore import enqueue_rescore
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from tests.test_score_again import _drain, _opp, _RecordingLLM, _tasks

_OVERRIDE = {"backend": "local", "model": "kestrel-tier3", "json_mode": "schema"}
_APPROVAL = {"tier2": ["service-a|model-a"]}


async def _stub(sm, *, stage: str = "t1_pass") -> int:
    """A thin copy: a posting with no description, parked as Not scored."""
    oid = await _opp(sm, stage=stage, title="Head of Platform")
    async with sm() as s:
        o = await s.get(Opportunity, oid)
        o.description_md = ""
        if stage == "t1_pass":
            o.stage_reason = EMPTY_POSTING_STAGE_REASON
        await s.commit()
    return oid


async def _waiting(sm, oid: int, task_type: str = "t2", **payload) -> int:
    """An ordinary automatic task, as the pipeline leaves it: no operator flags."""
    async with sm() as s:
        t = Task(type=task_type, payload={"opportunity_id": oid, **payload}, priority=30)
        s.add(t)
        await s.commit()
        return t.id


async def _payload(sm, task_id: int) -> dict:
    async with sm() as s:
        return dict((await s.get(Task, task_id)).payload)


async def test_score_again_upgrades_a_waiting_task_and_the_stub_is_then_scored(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.queued == [oid] and res.upgraded == [oid] and res.already == []
    p = await _payload(sm, tid)
    assert p["rescore"] is True and p["score_stub"] is True and p["requested"] is True
    assert p["requested_at"]
    assert len(await _tasks(sm)) == 1  # the same task, not a second one

    llm = _RecordingLLM(get_settings())
    await _drain(Worker(sm, llm, get_settings()), "t2")
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        evals = (await s.execute(
            select(Evaluation).where(Evaluation.opportunity_id == oid, Evaluation.tier == 2)
        )).scalars().all()
    assert len(evals) == 1 and opp.stage == "t2_done"  # scored, not skipped as empty
    assert [t for t, _m in llm.models] == ["tier2"]


async def test_a_waiting_tier1_task_is_upgraded_too_and_carries_the_stub_flag(sm):
    oid = await _opp(sm, stage="t0_pass")
    tid = await _waiting(sm, oid, "t1")
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.upgraded == [oid]
    p = await _payload(sm, tid)
    assert p["rescore"] is True and p["score_stub"] is True


async def test_a_leased_task_reports_already_running_and_is_left_alone(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        t = await s.get(Task, tid)
        t.status, t.worker_id = "leased", "w1"
        await s.commit()
    before = await _payload(sm, tid)
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True, model_override=_OVERRIDE)
        await s.commit()
    assert res.running == [oid] and res.queued == [] and res.already == []
    assert any("already running" in m for m in res.messages())
    after = await _payload(sm, tid)
    # enqueue_once may mark a live task `requested`; nothing else of the request lands on it.
    assert {k: v for k, v in after.items() if k != "requested"} == before
    assert "rescore" not in after and "score_stub" not in after and "model_override" not in after


async def test_a_pending_score_again_is_already_waiting_and_takes_the_newer_pick(sm):
    oid = await _stub(sm)
    async with sm() as s:
        first = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert first.queued == [oid] and first.upgraded == []
    async with sm() as s:
        again = await enqueue_rescore(s, [oid], score_stubs=True, model_override=_OVERRIDE)
        await s.commit()
    assert again.already == [oid] and again.queued == []
    (task,) = await _tasks(sm)
    assert task.payload["model_override"]["model"] == "kestrel-tier3"


async def test_an_upgrade_never_adds_an_online_approval_the_request_did_not_carry(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=True, online_approval=None)
        await s.commit()
    assert "online_approval" not in await _payload(sm, tid)

    oid2 = await _stub(sm)
    tid2 = await _waiting(sm, oid2)
    async with sm() as s:  # malformed input reads as no approval
        await enqueue_rescore(s, [oid2], score_stubs=True, online_approval="not json")
        await s.commit()
    assert "online_approval" not in await _payload(sm, tid2)


async def test_an_upgrade_carries_the_approval_confirmed_in_this_request_and_no_other(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid, online_approval={"tier2": ["old-service|old-model"]})
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=True, online_approval=_APPROVAL)
        await s.commit()
    assert (await _payload(sm, tid))["online_approval"] == _APPROVAL


async def test_an_upgrade_changes_no_stage_and_a_stub_is_never_rejected(sm):
    oid = await _stub(sm)
    await _waiting(sm, oid)
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
    assert opp.stage == "t1_pass" and opp.stage_reason == EMPTY_POSTING_STAGE_REASON
    assert opp.archived_at is None


async def test_an_upgrade_drops_a_stale_pick_and_leaves_the_background_lane(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid, model_override=_OVERRIDE, lane=1, lane_priority=7)
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    p = await _payload(sm, tid)
    assert "model_override" not in p and "lane" not in p and "lane_priority" not in p
    async with sm() as s:
        assert (await s.get(Task, tid)).priority >= 1000


async def test_a_lease_between_the_read_and_the_write_is_never_edited_under(sm, monkeypatch):
    """The worker leases the task after Score again read it as pending. The lease is written
    without syncing the loaded objects (synchronize_session=False), so the code under test still
    holds a stale "pending" row and only the guarded UPDATE can notice: it must match no row, the
    role is read again, and the payload stays untouched. A guard reduced to the task id fails
    this test (the stale pending row would be upgraded under the running worker)."""
    from sqlalchemy import update

    from kestrel.queue import rescore as rq

    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    real = rq._live_rows
    reads: list[list[str]] = []

    async def read_then_lease(session, task_type, opportunity_id):
        rows = await real(session, task_type, opportunity_id)
        reads.append([t.status for t in rows])
        if len(reads) == 1:  # a worker wins the race right after our first read
            await session.execute(update(Task).where(Task.id == tid).values(
                status="leased", worker_id="w1", lease_until="2999-01-01T00:00:00+00:00"
            ).execution_options(synchronize_session=False))
        return rows

    monkeypatch.setattr(rq, "_live_rows", read_then_lease)
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True, model_override=_OVERRIDE)
        await s.commit()
    assert reads[0] == ["pending"]  # the first read saw it waiting: only the guard can catch it
    assert len(reads) == 2 and reads[1] == ["leased"]  # the guarded UPDATE matched 0 rows: re-read
    assert res.running == [oid] and res.queued == [] and res.upgraded == []
    p = await _payload(sm, tid)
    assert "rescore" not in p and "score_stub" not in p and "model_override" not in p


async def test_a_leased_task_whose_lease_ran_out_is_waiting_not_running(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        t = await s.get(Task, tid)
        t.status, t.worker_id, t.lease_until = "leased", "gone", "2000-01-01T00:00:00+00:00"
        await s.commit()
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.upgraded == [oid] and res.running == []
    p = await _payload(sm, tid)
    assert p["rescore"] is True and p["score_stub"] is True
    async with sm() as s:
        t = await s.get(Task, tid)
        assert t.status == "leased" and t.lease_until == "2000-01-01T00:00:00+00:00"


async def test_an_expired_lease_on_the_last_attempt_is_not_waiting_it_will_never_run(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        t = await s.get(Task, tid)
        t.status, t.worker_id, t.lease_until = "leased", "gone", "2000-01-01T00:00:00+00:00"
        t.attempts, t.max_attempts = 3, 3
        await s.commit()
    before = await _payload(sm, tid)
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.running == [oid] and res.upgraded == [] and res.queued == []
    assert {k: v for k, v in (await _payload(sm, tid)).items() if k != "requested"} == before


async def test_an_expired_lease_with_attempts_left_is_still_upgraded(sm):
    oid = await _stub(sm)
    tid = await _waiting(sm, oid)
    async with sm() as s:
        t = await s.get(Task, tid)
        t.status, t.worker_id, t.lease_until = "leased", "gone", "2000-01-01T00:00:00+00:00"
        t.attempts, t.max_attempts = 2, 3
        await s.commit()
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.upgraded == [oid]
