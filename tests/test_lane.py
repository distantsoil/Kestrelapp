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


async def _source(sm, origin: str) -> int:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name=f"b-{origin}", enabled=True,
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


async def test_off_field_role_from_found_board_waits_in_the_lane(sm):
    sid = await _source(sm, board_discovery.DISCOVERED)
    off = await _opp(sm, sid, "Pastry Chef", 1)
    on = await _opp(sm, sid, "Operations Analyst II", 2)
    await run_tier0_batch(sm, settings=get_settings())
    t_off, t_on = await _t1(sm, off), await _t1(sm, on)
    assert t_off.payload["lane"] == 1 and t_off.priority == lane.LANE_PRIORITY
    assert "lane" not in t_on.payload and t_on.priority >= 0
    async with sm() as s:
        o = await s.get(Opportunity, off)
        assert o.stage == "t0_pass" and lane.LANE_REASON in o.stage_reason
        assert await lane.waiting_count(s) == 1


async def test_hand_added_board_is_not_filtered(sm):
    sid = await _source(sm, "manual")
    off = await _opp(sm, sid, "Pastry Chef", 1)
    await run_tier0_batch(sm, settings=get_settings())
    assert "lane" not in (await _t1(sm, off)).payload


async def test_lane_task_is_never_leased_until_released_or_requested(sm):
    sid = await _source(sm, board_discovery.DISCOVERED)
    off = await _opp(sm, sid, "Pastry Chef", 1)
    await run_tier0_batch(sm, settings=get_settings())
    q = TaskQueue(sm)
    assert await q.lease("w", types=["t1"]) is None
    # The operator asking for the role (Score anyway / Score again) sets `requested`.
    async with sm() as s:
        t = await s.get(Task, (await _t1(sm, off)).id)
        t.payload = {**t.payload, "requested": True}
        await s.commit()
    assert (await q.lease("w", types=["t1"])) is not None


async def _lane_tasks(sm, n: int) -> None:
    sid = await _source(sm, board_discovery.DISCOVERED)
    for i in range(n):
        await _opp(sm, sid, f"Pastry Chef {i}", i)
    await run_tier0_batch(sm, settings=get_settings())


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
    assert await lane.release_all(sm) == 3  # the operator's choice still works


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
