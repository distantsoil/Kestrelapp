"""Issue #250 (D-SCORE-919): the recheck of the legacy background lane (`lane.reject_mismatch`,
D-SRC-13) judges a waiting role by today's title filter and search countries. It must never set
aside a role that is the person's own: one they took into a pursuit, labeled, saved, chose to
score anyway or hid, nor one that already has a fit score. Those keep their stage whatever the
filter says now. A plain waiting role is still set aside on a clear mismatch. All text is
invented; no model and no network."""

from __future__ import annotations

import pytest

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Label, Opportunity, Pursuit
from kestrel.pipeline import lane
from kestrel.services import saved_roles, tier0_override

# The shipped fictional profile: its target titles do not match "Pastry Chef", and it searches
# the US only.
pytestmark = pytest.mark.usefixtures("example_operator_profile")


async def _role(sm, *, stage: str = "t0_pass", title: str = "Pastry Chef",
                location: str | None = None, **extra) -> int:
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=title,
                        org_name="Fictional Org", stage=stage, stage_reason="seeded",
                        seen_count=1, location_raw=location, **extra)
        s.add(o)
        await s.commit()
        return o.id


async def _pursue(s, oid):
    s.add(Pursuit(opportunity_id=oid, track="employment", status="applied"))


async def _label(s, oid):
    s.add(Label(opportunity_id=oid, track="employment", verdict="consider"))


async def _save(s, oid):
    await saved_roles.save(s, oid)


async def _score_anyway(s, oid):
    await tier0_override.record(s, oid, "blocker:geo: seeded")


async def _hide(s, oid):
    (await s.get(Opportunity, oid)).archived_at = utcnow_iso()


async def _recheck(sm, oid: int) -> tuple[bool, str, str | None]:
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        rejected = await lane.reject_mismatch(s, opp)
        await s.commit()
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        return rejected, opp.stage, opp.stage_reason


async def test_a_plain_waiting_role_is_still_set_aside_for_its_title(sm):
    rejected, stage, reason = await _recheck(sm, await _role(sm))
    assert rejected and stage == "t0_reject" and reason.startswith("unrelated_title")


async def test_a_plain_waiting_role_is_still_set_aside_for_its_country(sm):
    oid = await _role(sm, title="Operations Analyst", location="Berlin, Germany")
    rejected, stage, reason = await _recheck(sm, oid)
    assert rejected and stage == "t0_reject" and reason.startswith("outside_area")


@pytest.mark.parametrize("mark", [_pursue, _label, _save, _score_anyway, _hide],
                         ids=["pursued", "labeled", "saved", "score_anyway", "hidden"])
@pytest.mark.parametrize("mismatch", ["title", "country"])
async def test_the_persons_own_role_is_never_set_aside_by_the_recheck(sm, mark, mismatch):
    if mismatch == "title":
        oid = await _role(sm)
    else:
        oid = await _role(sm, title="Operations Analyst", location="Berlin, Germany")
    async with sm() as s:
        await mark(s, oid)
        await s.commit()
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")


@pytest.mark.parametrize("stage", ["t1_pass", "t2_done", "t3_done"])
async def test_a_role_past_quick_sort_is_never_set_aside_by_the_recheck(sm, stage):
    """A role that went through the lane and was kept or scored is rechecked again whenever a
    scoring task for it runs (kestrel.pipeline.worker): it keeps its stage and its score."""
    assert await _recheck(sm, await _role(sm, stage=stage)) == (False, stage, "seeded")


@pytest.mark.parametrize("status", ["shortlisted", "applied", "interviewing", "closed"])
async def test_a_pursuit_in_any_state_protects_the_role(sm, status):
    oid = await _role(sm)
    async with sm() as s:
        s.add(Pursuit(opportunity_id=oid, track="employment", status=status))
        await s.commit()
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")


async def test_the_lane_sweep_leaves_a_pursued_waiting_role_and_its_task_alone(sm):
    """The path the reviewers named: the sweep over waiting lane tasks (`_eligible_tasks`, run by
    release_due and release_all) must not restage a pursued role or finish its task."""
    from sqlalchemy import select

    from kestrel.db.models import Task

    mine, plain = await _role(sm), await _role(sm)
    async with sm() as s:
        await _pursue(s, mine)
        for oid in (mine, plain):
            s.add(Task(type="t1", payload={"opportunity_id": oid, **lane.lane_extra(0.0)},
                       priority=lane.LANE_PRIORITY))
        await s.commit()
    await lane.release_due(sm, hosted=True)  # hosted: the recheck runs, nothing is released
    async with sm() as s:
        stages = {oid: (await s.get(Opportunity, oid)).stage for oid in (mine, plain)}
        status = {t.payload["opportunity_id"]: t.status
                  for t in (await s.execute(select(Task))).scalars()}
    assert stages == {mine: "t0_pass", plain: "t0_reject"}
    assert status == {mine: "pending", plain: "done"}


async def test_one_general_word_does_not_set_aside_the_persons_own_role(sm):
    """The new title rule (D-SCORE-914) reaches the recheck too: the same guard holds."""
    oid = await _role(sm, title="Enterprise Sales Representative")
    async with sm() as s:
        await _pursue(s, oid)
        await s.commit()
    assert await _recheck(sm, oid) == (False, "t0_pass", "seeded")


# ------------------------------------------------------------------ the person's own request
# The worker rechecks every Tier 1 and Tier 2 task of a role that ever went through the lane
# (kestrel.pipeline.worker._score_one). A task the person asked for (Score this role anyway,
# Score again, a retry) carries that on its payload only, so the recheck must read the payload:
# their click is never finished unrun and the role is never restaged under it.
_ANYWAY = {"rescore": True, "requested": True, "tier0_override": True,
           "tier0_reason": "blocker:geo: seeded"}
_AGAIN = {"rescore": True, "requested": True}
_ASKED = [
    pytest.param("t0_reject", "t1", _ANYWAY, id="score_anyway_on_a_set_aside_role"),
    pytest.param("t0_pass", "t1", _AGAIN, id="score_again_on_a_waiting_role"),
    pytest.param("quarantined", "t2", _AGAIN, id="retry_of_a_failed_role"),
    pytest.param("t1_reject", "t1", _AGAIN, id="score_again_after_quick_sort"),
    pytest.param("t0_reject", "t2", {"requested": True}, id="requested_t2_on_a_set_aside_role"),
]


async def _lane_role_with_task(sm, stage: str, task_type: str, extra: dict) -> tuple[int, int]:
    from kestrel.db.models import Task

    oid = await _role(sm, stage=stage, lane_local_only=1)
    async with sm() as s:
        task = Task(type=task_type, payload={"opportunity_id": oid, **extra}, priority=1000)
        s.add(task)
        await s.commit()
        return oid, task.id


async def _drain(sm, task_type: str, handler):
    from types import SimpleNamespace

    from kestrel.config import get_settings
    from kestrel.pipeline.worker import TierStats, Worker

    settings = get_settings()
    await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
        task_type, 1, handler, TierStats())


@pytest.mark.parametrize(("stage", "task_type", "extra"), _ASKED)
async def test_a_task_the_person_asked_for_reaches_the_handler_on_a_lane_role(
        sm, stage, task_type, extra):
    from unittest.mock import AsyncMock

    from kestrel.db.models import Task
    from kestrel.pipeline._shared import HandlerOutcome

    oid, tid = await _lane_role_with_task(sm, stage, task_type, extra)
    handler = AsyncMock(return_value=HandlerOutcome("skip", int(task_type[1])))
    await _drain(sm, task_type, handler)
    handler.assert_awaited_once()  # the click ran: it was not finished unrun
    kwargs = handler.await_args.kwargs
    assert kwargs["opportunity_id"] == oid
    # The handler is told exactly what the person asked for, no more and no less.
    assert kwargs.get("rescore", False) is bool(extra.get("rescore"))
    assert kwargs.get("tier0_override", False) is bool(extra.get("tier0_override"))
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        task = await s.get(Task, tid)
    assert (opp.stage, opp.stage_reason) == (stage, "seeded")  # the recheck restaged nothing
    assert task.status == "done"


@pytest.mark.parametrize(("stage", "task_type"), [("t0_pass", "t1"), ("quarantined", "t2")])
async def test_an_ordinary_task_on_a_mismatched_lane_role_is_still_rechecked(
        sm, stage, task_type):
    """The control: with no request on the task and no mark of the person's, the recheck still
    sets a clear mismatch aside without a model call."""
    from unittest.mock import AsyncMock

    from kestrel.db.models import Task

    oid, tid = await _lane_role_with_task(sm, stage, task_type, {})
    handler = AsyncMock(side_effect=AssertionError("a mismatch must never be scored"))
    await _drain(sm, task_type, handler)
    handler.assert_not_awaited()
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        task = await s.get(Task, tid)
    assert opp.stage == "t0_reject" and opp.stage_reason.startswith("unrelated_title")
    assert task.status == "done"


async def test_score_anyway_on_a_lane_role_is_scored_and_recorded(sm):
    """The sequence the review reproduced, with the real Quick sort handler and a faked model:
    a set-aside lane role, the person's Score this role anyway, one worker pass."""
    from sqlalchemy import func, select

    from kestrel.config import get_settings
    from kestrel.db.models import Evaluation, Task
    from kestrel.pipeline.profile import load_profile
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.worker import TierStats, Worker
    from tests.pipeline_fakes import FakeLLM

    oid, tid = await _lane_role_with_task(sm, "t0_reject", "t1", _ANYWAY)
    async with sm() as s:
        (await s.get(Opportunity, oid)).description_md = "An invented posting. " * 40
        await s.commit()
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)
    worker._profiles = {t: load_profile(t) for t in ("employment", "consulting")}
    await worker.drain_tier("t1", 1, handle_t1, TierStats())
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
        task = await s.get(Task, tid)
        t1_evals = (await s.execute(select(func.count()).select_from(Evaluation).where(
            Evaluation.opportunity_id == oid, Evaluation.tier == 1))).scalar_one()
        recorded = await tier0_override.get(s, oid)
    assert t1_evals == 1  # the model read it, as the person asked
    assert recorded is not None  # "You asked to score this" is on record
    assert opp.stage in ("t1_pass", "t1_reject")
    assert not (opp.stage_reason or "").startswith("unrelated_title")
    assert task.status == "done"


def test_asked_by_person_reads_the_same_keys_as_the_queue():
    """One meaning of "the person asked": payload `requested` or `rescore`, as
    kestrel.queue.queue.requested_by_operator, which the lane sweep uses in SQL."""
    assert lane.asked_by_person({"requested": True}) and lane.asked_by_person({"rescore": True})
    assert lane.asked_by_person({"requested": 1})
    for payload in ({}, None, {"requested": False}, {"rescore": 0}, {"lane": 1},
                    {"tier0_override": True}, {"changed": True}):
        assert not lane.asked_by_person(payload)
