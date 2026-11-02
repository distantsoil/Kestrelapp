"""Database writes the Being scored again tests share (issue #74): queue a Score again task the
way enqueue_rescore does, start it (a lease), and land a new evaluation, straight in the
database. Each is a plain function that runs its own event loop, so a browser test calls it from
a thread. Nothing here touches a model."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db.base import iso
from kestrel.db.engine import make_async_engine
from kestrel.db.models import Evaluation, Opportunity, Task
from tests import clock_seed


def run(coro_fn, *args, **kw):
    """Run an async helper to the end from any thread (Playwright's sync API owns its own)."""

    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await coro_fn(s, *args, **kw)
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, go()).result()


async def _queue(s, opp_id, *, type="t2", priority=0, held=None):
    task = Task(type=type, payload={"opportunity_id": opp_id, "rescore": True},
                priority=priority, last_error=held)
    s.add(task)
    await s.commit()
    return task.id


async def _lease(s, task_id):
    until = iso(datetime.now(UTC) + timedelta(minutes=5))
    await s.execute(update(Task).where(Task.id == task_id)
                    .values(status="leased", worker_id="test", lease_until=until))
    await s.commit()


async def _finish(s, task_id):
    await s.execute(update(Task).where(Task.id == task_id).values(status="done"))
    await s.commit()


async def _land(s, opp_id, *, score, verdict="apply"):
    old = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == opp_id)
                           .order_by(Evaluation.id.desc()).limit(1))).scalar_one()
    s.add(Evaluation(opportunity_id=opp_id, tier=2, track=old.track, model=old.model,
                     verdict=verdict, verdict_model=verdict, score=score, weighted_score=score,
                     needs_review=False, blocks=old.blocks, dimensions=old.dimensions,
                     legitimacy=old.legitimacy, created_at=iso(datetime.now(UTC))))
    await s.commit()


async def _clear(s):
    await s.execute(delete(Task))
    await s.commit()


async def _fresh_today(s, opp_id):
    opp = await s.get(Opportunity, opp_id)
    # Found five minutes ago, but never before local midnight (D-TEST-2): CI at 00:03 UTC put
    # `now - 5 min` on the previous day and the Today card the test reads was empty.
    now = datetime.now(UTC)
    opp.first_seen_at = iso(clock_seed.ago_within(
        timedelta(minutes=5), clock_seed.local_day_start(now), horizon=timedelta(minutes=10),
        now=now))
    await s.commit()


async def _lease_then(s, task_id, how, note):
    """Lease the task through the real TaskQueue and give it back the way the worker does:
    `defer` (cannot run now) or `fail` (retry), so a test sees the rows production leaves."""
    from kestrel.queue.queue import TaskQueue

    q = TaskQueue(async_sessionmaker(s.bind, expire_on_commit=False))
    await q.lease("test-worker", types=["t1", "t2"])
    if how == "defer":
        await q.defer(task_id, note, worker_id="test-worker")
    else:
        await q.fail(task_id, note, worker_id="test-worker", retry=True)


def give_back(task_id, how, note="x") -> None:
    run(_lease_then, task_id, how, note)


def queue(opp_id, **kw) -> int:
    return run(_queue, opp_id, **kw)


def lease(task_id) -> None:
    run(_lease, task_id)


def land(opp_id, task_id, *, score) -> None:
    """The worker's end of a Score again: the new evaluation, then the task done."""
    run(_land, opp_id, score=score)
    run(_finish, task_id)


def clear_tasks() -> None:
    run(_clear)


def fresh_today(opp_id) -> None:
    run(_fresh_today, opp_id)
