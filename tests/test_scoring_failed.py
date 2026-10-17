"""D-UI-56: parked failures are never labeled "being scored". The startup orphan sweep re-queues
a role waiting on a scoring step with nothing queued, once, and marks it stuck after that;
scoring_progress counts open failures whatever the latest run said; Build stories' lease renewal
rides out "database is locked". Cloud-safe: a migrated SQLite file, no model, no network."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from kestrel.analytics.progress import scoring_progress
from kestrel.config import get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Setting, Task
from kestrel.interview import story_task
from kestrel.pipeline.orphans import ORPHAN_KEY, requeue_orphans, stuck_ids
from kestrel.web.queries import scoring_failed_count


async def _opp(sm, key: str, stage: str, **kw) -> int:
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title=f"Analyst {key}",
                        url=f"https://x/{key}", stage=stage, pre_score=0.5, **kw)
        s.add(o)
        await s.commit()
        return o.id


async def _tasks(sm, oid: int) -> list[Task]:
    async with sm() as s:
        rows = (await s.execute(select(Task))).scalars().all()
    return [t for t in rows if (t.payload or {}).get("opportunity_id") == oid]


# ------------------------------------------------------------------------------- orphans
async def test_an_orphan_is_requeued_once_then_shown_stuck(sm):
    t0 = await _opp(sm, "t0", "t0_pass")
    t1 = await _opp(sm, "t1", "t1_pass")
    first = await requeue_orphans(sm)
    assert first == {"requeued": 2, "stuck": 0}
    assert [t.type for t in await _tasks(sm, t0)] == ["t1"]
    assert [t.type for t in await _tasks(sm, t1)] == ["t2"]
    assert (await _tasks(sm, t1))[0].payload.get("orphan_requeue")
    async with sm() as s:
        assert await stuck_ids(s) == {t0, t1}
    # A second startup while the tasks are still queued: nothing happens.
    assert await requeue_orphans(sm) == {"requeued": 0, "stuck": 0}
    # The task ran and the role is still unscored with nothing queued: stuck, never re-queued.
    async with sm() as s:
        for t in (await s.execute(select(Task))).scalars():
            t.status = "done"
        await s.commit()
    assert await requeue_orphans(sm) == {"requeued": 0, "stuck": 2}
    assert len(await _tasks(sm, t0)) == 1 and len(await _tasks(sm, t1)) == 1


async def test_a_scored_role_leaves_the_record_and_others_are_left_alone(sm):
    orphan = await _opp(sm, "o", "t1_pass")
    queued = await _opp(sm, "q", "t1_pass")
    parked = await _opp(sm, "p", "quarantined")
    hidden = await _opp(sm, "h", "t0_pass", archived_at=utcnow_iso())
    done = await _opp(sm, "d", "t2_done")
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": queued}, status="leased"))
        await s.commit()
    assert await requeue_orphans(sm) == {"requeued": 1, "stuck": 0}
    for oid in (queued, parked, hidden, done):
        assert len(await _tasks(sm, oid)) == (1 if oid == queued else 0), oid
    # The role is scored: its record goes, so a later orphaning gets its one retry again.
    async with sm() as s:
        s.add(Evaluation(opportunity_id=orphan, tier=2, track="employment", verdict="apply",
                         score=4.2))
        await s.commit()
    await requeue_orphans(sm)
    async with sm() as s:
        assert await stuck_ids(s) == set()
        row = await s.get(Setting, ORPHAN_KEY)
        assert row is not None and row.value == {}


async def test_a_quarantined_t1_pass_is_scoring_failed_not_an_orphan(sm):
    oid = await _opp(sm, "qt", "t1_pass")
    async with sm() as s:
        s.add(Quarantine(opportunity_id=oid, tier=2, error="validation error"))
        await s.commit()
    assert await requeue_orphans(sm) == {"requeued": 0, "stuck": 0}


async def test_worker_startup_runs_the_sweep_beside_the_reclaim(sm):
    from kestrel.pipeline.worker import Worker
    from tests.pipeline_fakes import FakeLLM

    oid = await _opp(sm, "w", "t0_pass")
    worker = Worker(sm, FakeLLM(get_settings()), get_settings())
    await worker.reclaim()
    assert [t.type for t in await _tasks(sm, oid)] == ["t1"]


# --------------------------------------------------------------------------- open failures
async def test_open_failed_counts_parked_roles_with_nothing_queued(sm):
    a = await _opp(sm, "a", "quarantined")
    await _opp(sm, "b", "error")
    again = await _opp(sm, "c", "quarantined")
    await _opp(sm, "h", "quarantined", archived_at=utcnow_iso())
    await _opp(sm, "s", "t2_done")
    async with sm() as s:
        s.add(Task(type="t2", payload={"opportunity_id": again, "rescore": True},
                   status="pending"))
        s.add(Task(type="t2", payload={"opportunity_id": a}, status="dead"))
        await s.commit()
    async with sm() as s:
        p = await scoring_progress(s, get_settings())
        assert await scoring_failed_count(s) == 2
    # No run at all, so last_run is None, and the failures are still counted.
    assert p["last_run"] is None and p["open_failed"] == 2


# ------------------------------------------------------------------- story lease renewal
class _FlakyQueue:
    """heartbeat raises "database is locked" `locked` times, then answers `then` forever."""

    def __init__(self, locked: int, then: list[bool]) -> None:
        self.locked = locked
        self.then = list(then)
        self.calls = 0

    async def heartbeat(self, task_id, worker_id, *, lease_seconds):
        self.calls += 1
        if self.locked:
            self.locked -= 1
            raise OperationalError("UPDATE task", {}, Exception("database is locked"))
        return self.then.pop(0) if self.then else True


async def test_story_lease_renewal_rides_out_database_is_locked(monkeypatch):
    monkeypatch.setattr(story_task, "_RENEW_LOCK_BACKOFF_S", 0.001)
    q = _FlakyQueue(locked=10, then=[True, True, False])
    # Ten locked renewals in a row never stop it; only the lost lease (False) does.
    await asyncio.wait_for(story_task._renew(q, 1, "w", every_s=0.001), timeout=5)
    assert q.calls == 13


async def test_story_lease_renewal_keeps_going_while_the_build_runs(monkeypatch):
    monkeypatch.setattr(story_task, "_RENEW_LOCK_BACKOFF_S", 0.001)
    q = _FlakyQueue(locked=1000, then=[])
    beat = asyncio.create_task(story_task._renew(q, 1, "w", every_s=0.001))
    await asyncio.sleep(0.2)
    assert not beat.done() and q.calls > 5  # still renewing, never gave up
    beat.cancel()
    with pytest.raises(asyncio.CancelledError):
        await beat


async def test_other_renewal_errors_wait_for_the_next_beat(monkeypatch):
    class _Broken(_FlakyQueue):
        async def heartbeat(self, task_id, worker_id, *, lease_seconds):
            self.calls += 1
            if self.calls < 3:
                raise OperationalError("UPDATE task", {}, Exception("disk I/O error"))
            return False

    q = _Broken(0, [])
    await asyncio.wait_for(story_task._renew(q, 1, "w", every_s=0.001), timeout=5)
    assert q.calls == 3
