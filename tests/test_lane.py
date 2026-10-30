"""The background lane for roles from found company boards (D-SRC-8, D-SRC-9). Cloud-safe."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Source, Task
from kestrel.ingest import board_discovery
from kestrel.pipeline import lane
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.queue.queue import TaskQueue

_RECENT_KW = dict(
    track="employment", opp_type="job_posting", remote_type="remote", seen_count=1,
    ghost_score=0.1, stage="new", country="US",
)


async def _source(sm, origin: str, kind: str = "greenhouse") -> int:
    async with sm() as s:
        src = Source(track="employment", kind=kind, name=f"b-{origin}-{kind}", enabled=True,
                     origin=origin)
        s.add(src)
        await s.commit()
        return src.id


async def _opp(sm, sid: int, title: str, n: int) -> int:
    async with sm() as s:
        o = Opportunity(source_id=sid, title=title, external_id=f"e{n}", **_RECENT_KW)
        s.add(o)
        await s.commit()
        return o.id


async def _t1(sm, oid: int) -> Task:
    async with sm() as s:
        return (await s.execute(
            select(Task).where(Task.type == "t1", Task.payload["opportunity_id"].as_integer()
                               == oid)
        )).scalar_one()


async def test_tier0_no_longer_creates_lane_tasks(sm):
    sid = await _source(sm, board_discovery.DISCOVERED)
    off = await _opp(sm, sid, "Pastry Chef", 1)
    on = await _opp(sm, sid, "Operations Analyst II", 2)
    await run_tier0_batch(sm, settings=get_settings())
    t_off, t_on = await _t1(sm, off), await _t1(sm, on)
    assert "lane" not in t_off.payload
    assert "lane" not in t_on.payload and t_on.priority >= 0
    async with sm() as s:
        o = await s.get(Opportunity, off)
        assert o.stage == "t0_pass" and lane.LANE_REASON not in o.stage_reason
        assert await lane.waiting_count(s) == 0


async def test_tier0_does_not_lane_existing_board_or_search_rows(sm):
    """Title filtering now happens before storage for both source types."""
    board = await _source(sm, "manual")
    off = await _opp(sm, board, "Pastry Chef", 1)
    search = await _source(sm, "manual", kind="jobspy")
    asked = await _opp(sm, search, "Pastry Chef", 2)
    await run_tier0_batch(sm, settings=get_settings())
    assert "lane" not in (await _t1(sm, off)).payload
    assert "lane" not in (await _t1(sm, asked)).payload


async def test_lane_task_is_never_leased_until_released_or_requested(sm):
    await _lane_tasks(sm, 1)
    q = TaskQueue(sm)
    assert await q.lease("w", types=["t1"]) is None
    # The operator asking for the role (Score anyway / Score again) sets `requested`.
    async with sm() as s:
        t = (await s.scalars(select(Task))).one()
        t.payload = {**t.payload, "requested": True}
        await s.commit()
    assert (await q.lease("w", types=["t1"])) is not None


async def _lane_tasks(sm, n: int) -> None:
    sid = await _source(sm, board_discovery.DISCOVERED)
    for i in range(n):
        oid = await _opp(sm, sid, f"Operations Analyst {i}", i)
        async with sm() as s:
            opp = await s.get(Opportunity, oid)
            opp.stage = "t0_pass"
            opp.stage_reason = lane.LANE_REASON
            s.add(Task(type="t1", priority=lane.LANE_PRIORITY,
                       payload={"opportunity_id": oid, **lane.lane_extra(0)}))
            await s.commit()


async def test_release_waits_for_idle_and_honors_the_daily_cap(sm):
    await _lane_tasks(sm, 5)
    # a normal t1 task is pending: Kestrel is busy, nothing is freed
    async with sm() as s:
        s.add(Task(type="t1", payload={"opportunity_id": 9999}))
        await s.commit()
    assert await lane.release_due(sm, cap=3) == 0
    async with sm() as s:
        for t in (await s.execute(select(Task).where(Task.payload["opportunity_id"]
                                                     .as_integer() == 9999))).scalars():
            t.status = "done"
        await s.commit()
    assert await lane.release_due(sm, cap=3) == 3
    assert await lane.release_due(sm, cap=3) == 0  # the day's cap is spent
    q = TaskQueue(sm)
    assert (await q.lease("w", types=["t1"])) is not None  # freed tasks run normally
    async with sm() as s:
        assert await lane.waiting_count(s) == 2


async def test_score_everything_now_frees_all_once(sm):
    await _lane_tasks(sm, 4)
    assert await lane.release_all(sm) == 4
    assert await lane.release_all(sm) == 0
    async with sm() as s:
        assert await lane.waiting_count(s) == 0


@pytest.mark.parametrize("n", [0, 3])
async def test_seconds_per_role_falls_back_when_unmeasured(sm, n):
    async with sm() as s:
        secs, measured = await lane.seconds_per_role(s)
    assert measured is False and secs == lane.ROUGH_SECONDS_PER_ROLE


async def test_hosted_scoring_never_releases_on_its_own(sm):
    await _lane_tasks(sm, 3)
    assert await lane.release_due(sm, cap=10, hosted=True) == 0
    async with sm() as s:
        assert await lane.waiting_count(s) == 3
    assert await lane.release_all(sm, hosted=True) == 0  # confirmation cannot allow hosted scoring
    assert await lane.release_all(sm) == 3  # local settings can release the backlog


def test_scores_on_hosted_detects_mode_and_external_tier():
    s = get_settings()
    assert lane.scores_on_hosted(s) is False
    s.llm.mode = "hosted"
    assert lane.scores_on_hosted(s) is True
    s.llm.mode = "local"
    name = s.llm.tiers["tier1"].backend
    s.llm.backends[name].external = True
    assert lane.scores_on_hosted(s) is True


async def test_archived_and_requested_tasks_neither_count_nor_use_the_cap(sm):
    await _lane_tasks(sm, 4)
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == "t1"))).scalars().all()
        tasks.sort(key=lambda t: t.id)
        opp = await s.get(Opportunity, tasks[0].payload["opportunity_id"])
        opp.archived_at = "2026-01-01T00:00:00+00:00"
        tasks[1].payload = {**tasks[1].payload, "requested": True}
        tasks[1].status = "leased"  # running on its own, so Kestrel can be idle below
        tasks[1].lease_until = "2999-01-01T00:00:00+00:00"
        await s.commit()
        assert await lane.waiting_count(s) == 2
    assert await lane.release_due(sm, cap=2) == 2
    async with sm() as s:
        assert await lane.waiting_count(s) == 0


async def test_daily_cap_rolls_over_at_the_local_midnight(sm):
    from datetime import UTC, datetime, timedelta

    await _lane_tasks(sm, 3)
    t0 = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    assert await lane.release_due(sm, cap=1, now=t0) == 1
    assert await lane.release_due(sm, cap=1, now=t0 + timedelta(minutes=5)) == 0  # same day
    async with sm() as s:  # the freed task ran, so Kestrel is idle again
        for t in (await s.execute(select(Task).where(Task.status == "pending"))).scalars():
            if not (t.payload or {}).get("lane"):
                t.status = "done"
        await s.commit()
    assert await lane.release_due(sm, cap=1, now=t0 + timedelta(days=1)) == 1  # next local day
    assert lane._today(t0) == t0.astimezone().date().isoformat()


@pytest.mark.parametrize("release", ["due", "all"])
async def test_existing_mismatches_are_rejected_without_release_even_when_hosted(sm, release):
    await _lane_tasks(sm, 3)
    async with sm() as s:
        roles = (await s.scalars(select(Opportunity).order_by(Opportunity.id))).all()
        roles[0].location_raw = "Toronto, Ontario, CA"
        roles[1].title = "Pastry Chef"
        ids = [o.id for o in roles]
        await s.commit()
    if release == "due":
        assert await lane.release_due(sm, hosted=True) == 0
    else:
        assert await lane.release_all(sm, hosted=True) == 0
    async with sm() as s:
        for oid, reason in zip(ids[:2], ["outside_area", "unrelated_title"], strict=True):
            opp = await s.get(Opportunity, oid)
            assert opp.stage == "t0_reject" and reason in opp.stage_reason
            assert (await _t1(sm, oid)).status == "done"
        assert (await s.get(Opportunity, ids[2])).stage == "t0_pass"
        assert await lane.waiting_count(s) == 1


@pytest.mark.parametrize("task_type", ["t1", "t2"])
@pytest.mark.parametrize("routing", ["hosted", "external", "client_external", "override"])
async def test_released_lane_never_calls_a_hosted_handler(sm, monkeypatch, task_type, routing):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kestrel.config import BackendConfig
    from kestrel.pipeline.worker import TierStats, Worker

    await _lane_tasks(sm, 1)
    assert await lane.release_all(sm) == 1
    settings = get_settings()
    client_settings = settings.model_copy(deep=True)
    async with sm() as s:
        task = (await s.scalars(select(Task))).one()
        task.type = task_type
        oid = task.payload["opportunity_id"]
        if routing == "override":
            task.payload = {**task.payload, "model_override": {"model": "paid", "backend": "paid"}}
        await s.commit()
    if routing == "hosted":
        settings.llm.mode = "hosted"
    elif routing == "external":
        tier = "tier1" if task_type == "t1" else "tier2"
        settings.llm.backends[settings.llm.tiers[tier].backend].external = True
    else:
        tier = "tier1" if task_type == "t1" else "tier2"
        client_settings.llm.backends[client_settings.llm.tiers[tier].backend].external = True
    worker = Worker(sm, SimpleNamespace(settings=client_settings), settings)
    if routing == "override":
        picked = settings.model_copy(deep=True)
        picked.llm.backends["paid"] = BackendConfig(external=True)
        picked.llm.tiers["tier2"].backend = "paid"
        monkeypatch.setattr(
            worker, "_task_settings", AsyncMock(return_value=(picked, worker._client))
        )
    handler = AsyncMock(side_effect=AssertionError("lane must never call a hosted handler"))
    await worker.drain_tier(task_type, 1, handler, TierStats())
    handler.assert_not_awaited()
    async with sm() as s:
        task = (await s.scalars(select(Task))).one()
        assert task.status == "pending" and task.attempts == 0
        assert "local models only" in task.last_error
        assert (await s.get(Opportunity, oid)).stage == "t0_pass"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("now_matches", [False, True])
async def test_rejected_lane_origin_stays_local_after_profile_recheck(sm, legacy, now_matches):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kestrel.pipeline.recheck import recheck_track
    from kestrel.pipeline.worker import TierStats, Worker

    await _lane_tasks(sm, 1)
    await lane.release_all(sm)
    async with sm() as s:
        task = (await s.scalars(select(Task))).one()
        if legacy:
            task.payload = {"opportunity_id": task.payload["opportunity_id"], "lane": 0}
        opp = await s.get(Opportunity, task.payload["opportunity_id"])
        opp.title = "Pastry Chef"
        oid = opp.id
        await s.commit()
    settings = get_settings()
    async with sm() as s:
        assert await lane.reject_mismatch(s, await s.get(Opportunity, oid))
        task = (await s.scalars(select(Task))).one()
        task.status = "done"
        await s.commit()
    result = await recheck_track(sm, "employment", settings=settings)
    assert result["released"] == 1  # the ordinary rules re-release it, preserving its lane origin
    await run_tier0_batch(sm, settings=settings)
    async with sm() as s:
        tasks = (await s.scalars(select(Task))).all()
        ordinary = next(t for t in tasks if t.type == "t1" and t.status == "pending")
        assert (await s.get(Opportunity, oid)).stage == "t0_pass"
        assert ordinary.status == "pending"
        if now_matches:
            opp = await s.get(Opportunity, oid)
            opp.title = "Operations Analyst"
            await s.commit()
    settings.llm.mode = "hosted"
    handler = AsyncMock(side_effect=AssertionError("restored lane origin must stay local"))
    await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
        "t1", 1, handler, TierStats()
    )
    handler.assert_not_awaited()
    async with sm() as s:
        ordinary = await s.get(Task, ordinary.id)
        assert ordinary.status == ("pending" if now_matches else "done")
        if now_matches:
            assert "local models only" in ordinary.last_error
        else:
            assert (await s.get(Opportunity, oid)).stage == "t0_reject"


@pytest.mark.parametrize("task_type", ["t1", "t2"])
async def test_worker_rechecks_a_lane_role_after_release(sm, task_type):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kestrel.pipeline.worker import TierStats, Worker

    await _lane_tasks(sm, 1)
    await lane.release_all(sm)
    async with sm() as s:
        task = (await s.scalars(select(Task))).one()
        task.type = task_type
        opp = await s.get(Opportunity, task.payload["opportunity_id"])
        opp.title = "Pastry Chef"
        oid = opp.id
        await s.commit()
    settings = get_settings()
    handler = AsyncMock(side_effect=AssertionError("mismatch must never be scored"))
    await Worker(sm, SimpleNamespace(settings=settings), settings).drain_tier(
        task_type, 1, handler, TierStats()
    )
    handler.assert_not_awaited()
    async with sm() as s:
        assert (await s.get(Opportunity, oid)).stage == "t0_reject"
        assert (await s.scalars(select(Task))).one().status == "done"


async def test_lane_tier1_carries_local_only_to_tier2_across_restart(sm):
    from unittest.mock import AsyncMock

    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.worker import TierStats, Worker
    from tests.pipeline_fakes import FakeLLM

    await _lane_tasks(sm, 1)
    assert await lane.release_all(sm) == 1
    settings = get_settings()
    await Worker(sm, FakeLLM(settings), settings).drain_tier("t1", 1, handle_t1, TierStats())
    async with sm() as s:
        task = (await s.scalars(select(Task).where(Task.type == "t2"))).one()
        assert task.payload[lane.LOCAL_ONLY] is True
    settings.llm.mode = "hosted"
    handler = AsyncMock(side_effect=AssertionError("Tier 2 must remain local after restart"))
    await Worker(sm, FakeLLM(settings), settings).drain_tier("t2", 1, handler, TierStats())
    handler.assert_not_awaited()
    async with sm() as s:
        task = await s.get(Task, task.id)
        assert task.status == "pending" and "local models only" in task.last_error


async def test_held_lane_does_not_starve_other_tier2_model_groups(sm):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kestrel.pipeline._shared import HandlerOutcome
    from kestrel.pipeline.worker import TierStats, Worker

    await _lane_tasks(sm, 1)
    await lane.release_all(sm)
    settings = get_settings()
    settings.llm.mode = "hosted"
    async with sm() as s:
        task = (await s.scalars(select(Task))).one()
        task.type = "t2"
        other = Opportunity(track="employment", opp_type="job_posting", title="Operations Analyst",
                            stage="t1_pass")
        s.add(other)
        await s.flush()
        oid = other.id
        s.add(Task(type="t2", payload={"opportunity_id": oid, "model_override": {
            "backend": "local", "model": "other-local-model"}}))
        await s.commit()
    worker = Worker(sm, SimpleNamespace(settings=settings), settings)
    handler = AsyncMock(return_value=HandlerOutcome("skip", 2))
    await worker.drain_tier("t2", 1, handler, TierStats())
    handler.assert_awaited_once()
    assert handler.call_args.kwargs["opportunity_id"] == oid
