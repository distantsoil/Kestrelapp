"""Re-check set-aside roles after a profile change (D-RULES-RECHECK). Rules only; no model."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Label, Opportunity, Source, Task
from kestrel.pipeline import profile as profile_mod
from kestrel.pipeline import recheck
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.services import tier0_override

_KW = dict(track="employment", opp_type="job_posting", remote_type="remote", seen_count=1,
           ghost_score=0.1, country="US")


@pytest.fixture
def prof(monkeypatch):
    """A profile whose seniority floor the test can change between saves."""
    state = {"floor": "director"}
    real = profile_mod._load_yaml

    def load() -> dict:
        data = real()
        terms = dict(data["employment"]["employment_terms"])
        terms.update(seniority_floor=state["floor"])
        data["employment"] = {**data["employment"], "employment_terms": terms}
        return data

    monkeypatch.setattr(profile_mod, "_load_yaml", load)
    monkeypatch.setattr(recheck, "_load_yaml", load)
    return state


async def _seed(sm) -> dict[str, int]:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
        s.add(src)
        await s.flush()
        ids = {}
        for name in ("plain", "anyway", "labeled", "archived"):
            o = Opportunity(source_id=src.id, title="Support Engineer", external_id=name,
                            stage="t0_reject", stage_reason="blocker:seniority_floor: low",
                            **_KW)
            s.add(o)
            await s.flush()
            ids[name] = o.id
        (await s.get(Opportunity, ids["archived"])).archived_at = "2026-01-01T00:00:00+00:00"
        s.add(Label(opportunity_id=ids["labeled"], track="employment"))
        await tier0_override.record(s, ids["anyway"], "blocker:seniority_floor: low")
        await s.commit()
        return ids


async def _stage(sm, oid) -> str:
    async with sm() as s:
        return (await s.get(Opportunity, oid)).stage


async def test_lowering_the_floor_releases_only_untouched_set_aside_roles(sm, prof):
    ids = await _seed(sm)
    assert await recheck.queue_after_save(sm, "employment") == 1  # first save: baseline, 1 eligible
    prof["floor"] = ""
    assert await recheck.queue_after_save(sm, "employment") == 1
    got = await recheck.drain(sm, settings=get_settings())
    assert got["released"] == 1
    assert await _stage(sm, ids["plain"]) == "new"
    for name in ("anyway", "labeled", "archived"):
        assert await _stage(sm, ids[name]) == "t0_reject"
    await run_tier0_batch(sm, settings=get_settings())  # the ordinary sweep carries it on
    assert await _stage(sm, ids["plain"]) == "t0_pass"


async def test_a_role_that_still_fails_stays_set_aside(sm, prof):
    ids = await _seed(sm)
    await recheck.queue_after_save(sm, "employment")
    prof["floor"] = "senior director"  # a different change, still above this role
    assert await recheck.queue_after_save(sm, "employment") == 1
    got = await recheck.drain(sm, settings=get_settings())
    assert got["released"] == 0 and await _stage(sm, ids["plain"]) == "t0_reject"


async def test_same_inputs_queue_nothing_and_task_is_not_stacked(sm, prof):
    await _seed(sm)
    assert await recheck.queue_after_save(sm, "employment") == 1
    assert await recheck.queue_after_save(sm, "employment") == 0  # nothing changed
    prof["floor"] = ""
    await recheck.queue_after_save(sm, "employment")
    prof["floor"] = "lead"
    await recheck.queue_after_save(sm, "employment")
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == recheck.TASK_TYPE))).scalars()
        live = [t for t in tasks if t.status == "pending"]
    assert len(live) == 1


async def test_rerunning_the_drain_changes_nothing(sm, prof):
    await _seed(sm)
    await recheck.queue_after_save(sm, "employment")
    prof["floor"] = ""
    await recheck.queue_after_save(sm, "employment")
    assert (await recheck.drain(sm, settings=get_settings()))["released"] == 1
    assert (await recheck.drain(sm, settings=get_settings())) == {"seen": 0, "released": 0}


async def test_a_save_during_a_running_recheck_queues_a_follow_up(sm, prof):
    await _seed(sm)
    await recheck.queue_after_save(sm, "employment")
    async with sm() as s:  # the first pass is running on the old inputs
        (await s.execute(select(Task).where(Task.type == recheck.TASK_TYPE))).scalar_one() \
            .status = "leased"
        await s.commit()
    prof["floor"] = ""
    assert await recheck.queue_after_save(sm, "employment") == 1
    prof["floor"] = "lead"
    await recheck.queue_after_save(sm, "employment")
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == recheck.TASK_TYPE))).scalars()
        status = sorted(t.status for t in tasks)
    assert status == ["leased", "pending"]  # one follow-up, never stacked


async def test_a_dead_recheck_does_not_block_the_next_save(sm, prof):
    await _seed(sm)
    await recheck.queue_after_save(sm, "employment")
    async with sm() as s:
        (await s.execute(select(Task).where(Task.type == recheck.TASK_TYPE))).scalar_one() \
            .status = "dead"
        await s.commit()
    assert await recheck.queue_after_save(sm, "employment") == 1  # same inputs, nothing completed
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == recheck.TASK_TYPE))).scalars()
        assert sorted(t.status for t in tasks) == ["dead", "pending"]
    await recheck.drain(sm, settings=get_settings())
    assert await recheck.queue_after_save(sm, "employment") == 0  # completed: now remembered
