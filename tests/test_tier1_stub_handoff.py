"""Tier 1 hands the operator's Score again on a thin posting to Tier 2 (issue #144): the follow-on
Tier 2 task carries `score_stub`, so Tier 2 scores it instead of skipping it as empty. The model
is faked; nothing reaches a network. Invented text only."""

from __future__ import annotations

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Task
from kestrel.pipeline.worker import Worker
from kestrel.queue.rescore import enqueue_rescore
from tests.test_score_again import _drain, _opp, _RecordingLLM, _tasks


async def _thin(sm, stage: str) -> int:
    oid = await _opp(sm, stage=stage, title="Head of Platform")
    async with sm() as s:
        (await s.get(Opportunity, oid)).description_md = ""
        await s.commit()
    return oid


async def test_tier1_passes_score_stub_to_its_tier2_task_and_the_thin_role_is_scored(sm):
    oid = await _thin(sm, "t0_pass")
    async with sm() as s:
        res = await enqueue_rescore(s, [oid], score_stubs=True)
        await s.commit()
    assert res.queued == [oid]
    settings = get_settings()
    llm = _RecordingLLM(settings)
    worker = Worker(sm, llm, settings)
    await _drain(worker, "t1")
    t2 = [t for t in await _tasks(sm) if t.type == "t2"]
    assert len(t2) == 1
    assert t2[0].payload["score_stub"] is True and t2[0].payload["rescore"] is True
    assert t2[0].payload["requested"] is True and t2[0].payload["requested_at"]

    await _drain(worker, "t2")
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        tiers = (await s.execute(
            select(Evaluation.tier).where(Evaluation.opportunity_id == oid)
        )).scalars().all()
    assert opp.stage == "t2_done" and 2 in tiers  # scored, not left Not scored


async def test_without_the_operator_flag_the_follow_on_task_has_no_score_stub(sm):
    oid = await _thin(sm, "t0_pass")
    async with sm() as s:
        await enqueue_rescore(s, [oid], score_stubs=False)
        await s.commit()
    settings = get_settings()
    await _drain(Worker(sm, _RecordingLLM(settings), settings), "t1")
    async with sm() as s:
        t2 = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().all()
    assert len(t2) == 1 and "score_stub" not in t2[0].payload
