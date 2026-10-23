"""Task queue: lease/expiry/reclaim, retry increments, dead-lettering, and no double-lease under
concurrency."""

from __future__ import annotations

import asyncio

from kestrel.db.base import iso, utcnow
from kestrel.db.models import Task
from kestrel.queue import TaskQueue


async def test_enqueue_and_lease(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t1", {"x": 1}, priority=5)
    task = await q.lease("w1")
    assert task is not None and task.id == tid
    assert task.status == "leased" and task.worker_id == "w1" and task.attempts == 1


async def test_priority_order(sm):
    q = TaskQueue(sm)
    low = await q.enqueue("t", priority=1)
    high = await q.enqueue("t", priority=9)
    first = await q.lease("w1")
    assert first.id == high
    second = await q.lease("w1")
    assert second.id == low


async def test_lease_expiry_reclaim(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t")
    task = await q.lease("w1", lease_seconds=60)
    assert task.id == tid
    # a fresh lease finds nothing (still leased and not expired)
    assert await q.lease("w2") is None
    # force the lease to have expired
    async with sm() as s:
        t = await s.get(Task, tid)
        t.lease_until = iso(utcnow().replace(year=2000))
        await s.commit()
    reclaimed = await q.reclaim_expired()
    assert reclaimed == 1
    # now it is leasable again
    again = await q.lease("w2")
    assert again is not None and again.id == tid and again.attempts == 2


async def test_retry_increments_then_dead_letters(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t", max_attempts=2)
    # attempt 1
    await q.lease("w1")
    status = await q.fail(tid, "boom")
    assert status == "pending"
    t = await q.get(tid)
    assert t.attempts == 1 and t.last_error == "boom"
    # attempt 2 -> exhausts max_attempts -> dead
    await q.lease("w1")
    status = await q.fail(tid, "boom again")
    assert status == "dead"
    t = await q.get(tid)
    assert t.attempts == 2 and t.status == "dead"
    # a dead task is never leased
    assert await q.lease("w1") is None


async def test_complete(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t")
    await q.lease("w1")
    await q.complete(tid)
    assert (await q.get(tid)).status == "done"
    assert await q.lease("w1") is None


async def test_heartbeat_extends_only_for_owner(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t")
    await q.lease("w1", lease_seconds=1)
    assert await q.heartbeat(tid, "w1", lease_seconds=300) is True
    assert await q.heartbeat(tid, "someone_else") is False


async def _expire_lease(sm, task_id: int) -> None:
    async with sm() as s:
        task = await s.get(Task, task_id)
        task.lease_until = iso(utcnow().replace(year=2000))
        await s.commit()


async def test_expired_but_attempts_exhausted_is_not_re_run(sm):
    # crash-without-fail on a single-attempt task: after the lease expires it must NOT be re-leased
    # (that would run it a 2nd time past max_attempts); reclaim dead-letters it instead.
    q = TaskQueue(sm)
    tid = await q.enqueue("t", max_attempts=1)
    task = await q.lease("w1", lease_seconds=60)
    assert task.attempts == 1
    await _expire_lease(sm, tid)
    assert await q.lease("w2") is None  # not re-leased
    assert await q.reclaim_expired() == 1
    assert (await q.get(tid)).status == "dead"  # dead-lettered, not re-run


async def test_stale_worker_cannot_clobber_a_reassigned_task(sm):
    q = TaskQueue(sm)
    tid = await q.enqueue("t", max_attempts=3)
    await q.lease("wA", lease_seconds=60)  # attempts=1, owned by wA
    await _expire_lease(sm, tid)
    await q.reclaim_expired()  # back to pending
    b = await q.lease("wB", lease_seconds=60)  # attempts=2, owned by wB
    assert b.worker_id == "wB"
    # the stale worker A must not be able to finish or fail wB's task
    assert await q.complete(tid, "wA") is False
    assert await q.fail(tid, "late", worker_id="wA") == "not_owned"
    t = await q.get(tid)
    assert t.status == "leased" and t.worker_id == "wB"
    # the true owner can
    assert await q.complete(tid, "wB") is True
    assert (await q.get(tid)).status == "done"


async def test_two_workers_never_lease_the_same_task(sm):
    q = TaskQueue(sm)
    ids = [await q.enqueue("t") for _ in range(20)]

    async def worker(wid: str) -> list[int]:
        got: list[int] = []
        while True:
            task = await q.lease(wid, lease_seconds=120)
            if task is None:
                break
            got.append(task.id)
        return got

    results = await asyncio.gather(*(worker(f"w{i}") for i in range(4)))
    leased = [tid for r in results for tid in r]
    assert sorted(leased) == sorted(ids)  # every task leased exactly once
    assert len(leased) == len(set(leased))  # no task leased twice


async def test_a_cancelled_heartbeat_still_finishes_its_write(sm):
    # Renewal loops are cancelled when their task ends, often mid-write. The write must run to the
    # end (closing its session) and only then report the cancel; an interrupted write left an
    # unclosed connection that failed later on Windows ("Event loop is closed").
    import asyncio

    import pytest

    q = TaskQueue(sm)
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[bool] = []

    async def slow_write(task_id, worker_id, *, lease_seconds):
        started.set()
        await release.wait()
        finished.append(True)
        return True

    q._heartbeat = slow_write  # type: ignore[method-assign]
    beat = asyncio.create_task(q.heartbeat(1, "w1"))
    await started.wait()
    beat.cancel()
    await asyncio.sleep(0)
    assert finished == []  # still writing: the cancel waits for it
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await beat
    assert finished == [True]
