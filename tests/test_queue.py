"""Task queue: lease/expiry/reclaim, retry increments, dead-lettering, and no double-lease under
concurrency."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

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
    # the 30 s backoff after attempt 1 has passed (issue #153)
    async with sm() as s:
        t = await s.get(Task, tid)
        t.retry_at = iso(utcnow().replace(year=2000))
        await s.commit()
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


@pytest.mark.parametrize(
    "method,call,private,ret",
    [
        ("complete", lambda q: q.complete(1, "w1"), "_complete", True),
        ("fail", lambda q: q.fail(1, "boom", worker_id="w1"), "_fail", "pending"),
        ("defer", lambda q: q.defer(1, "out of credit", worker_id="w1"), "_defer", "deferred"),
        ("give_back", lambda q: q.give_back(1, {"n": 1}, worker_id="w1"), "_give_back", "pending"),
        ("release_all_leased", lambda q: q.release_all_leased(), "_release_all_leased", 1),
    ],
)
async def test_a_cancelled_queue_write_still_finishes_its_write(sm, method, call, private, ret):
    # D-QUEUE-4: every queue write is shielded the way heartbeat is (issue #153 point 4): a
    # cancel landing inside the database call must not abandon the connection; the write runs
    # to the end (closing its session) and only then reports the cancel.
    import asyncio

    import pytest

    q = TaskQueue(sm)
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[bool] = []

    async def slow_write(*_args, **_kwargs):
        started.set()
        await release.wait()
        finished.append(True)
        return ret

    setattr(q, private, slow_write)
    job = asyncio.create_task(call(q))
    await started.wait()
    job.cancel()
    await asyncio.sleep(0)
    assert finished == []  # still writing: the cancel waits for it
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert finished == [True]


async def test_a_clean_quit_never_spends_a_scoring_tasks_attempts(sm):
    # issue #153: an intentional quit is not a failure. Three quit/restart cycles while a
    # scoring task is running leave it pending with no change in attempts.
    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 1})
    for _ in range(3):  # running, quit, restart
        assert await q.lease("w", types=["t1"]) is not None
        await q.release_all_leased()
    async with sm() as s:
        (t,) = (await s.execute(select(Task))).scalars().all()
    assert t.status == "pending" and t.attempts == 0
    # Only a real failure counts.
    task = await q.lease("w", types=["t1"])
    assert await q.fail(task.id, "boom", worker_id="w") == "pending"
    async with sm() as s:
        assert (await s.get(Task, t.id)).attempts == 1


async def test_a_retryable_failure_backs_off_until_its_wait_passes(sm):
    # issue #153: a retryable failure is requeued with a backoff (30 s out after the first
    # failed attempt); not claimable before the wait, claimable after it.
    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 1})
    task = await q.lease("w", types=["t1"])
    assert await q.fail(task.id, "boom", worker_id="w") == "pending"
    async with sm() as s:
        t = await s.get(Task, task.id)
    assert t.attempts == 1 and t.retry_at is not None and t.retry_at > iso(utcnow())
    assert await q.lease("w2", types=["t1"]) is None  # not claimable before the wait
    async with sm() as s:
        t = await s.get(Task, task.id)
        t.retry_at = iso(utcnow().replace(year=2000))  # the wait has passed
        await s.commit()
    again = await q.lease("w3", types=["t1"])
    assert again is not None and again.id == t.id  # claimable after the wait


async def test_the_backoff_ladder_is_30_seconds_2_minutes_then_10(sm):
    # issue #153: 30 s after the first failed attempt, 2 min after the second, 10 min from the
    # third onward (the clamp); a rung makes the task unclaimable until the wait passes.
    from datetime import timedelta

    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 44})
    async with sm() as s:
        t = (await s.execute(select(Task))).scalar_one()
        t.max_attempts = 4  # so the third failure still requeues (the 10 min rung)
        await s.commit()
    for wait in (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10)):
        task = await q.lease("w", types=["t1"])
        assert await q.fail(task.id, "boom", worker_id="w") == "pending"
        async with sm() as s:
            t = await s.get(Task, task.id)
        # the wait is the rung, not any other value
        assert iso(utcnow() + wait - timedelta(seconds=1)) <= t.retry_at <= \
            iso(utcnow() + wait + timedelta(seconds=1))
        assert await q.lease("w2", types=["t1"]) is None  # not claimable during the wait
        async with sm() as s:
            t = await s.get(Task, task.id)
            t.retry_at = iso(utcnow().replace(year=2000))  # the wait has passed
            await s.commit()
    task = await q.lease("w", types=["t1"])
    assert await q.fail(task.id, "boom", worker_id="w") == "dead"  # no attempts left
    async with sm() as s:
        assert (await s.get(Task, task.id)).retry_at is None


async def test_a_permanent_failure_dead_letters_without_a_backoff(sm):
    # issue #153: permanent errors dead-letter at once, as before, with no backoff wait.
    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 2})
    task = await q.lease("w", types=["t1"])
    assert await q.fail(task.id, "no such model", worker_id="w", retry=False) == "dead"
    async with sm() as s:
        t = await s.get(Task, task.id)
    assert t.status == "dead" and t.retry_at is None


async def test_a_try_again_clears_the_backoff_wait(sm):
    # issue #153: the operator's fresh request (Score again on the waiting row) clears the
    # backoff, so the task is claimable at once.
    from kestrel.queue import rescore

    q = TaskQueue(sm)
    await q.enqueue("t1", {"opportunity_id": 3})
    task = await q.lease("w", types=["t1"])
    assert await q.fail(task.id, "boom", worker_id="w") == "pending"
    async with sm() as s:
        assert (await s.get(Task, task.id)).retry_at is not None
        assert await rescore._upgrade_waiting(s, "t1", 3, {}) == "upgraded"
        await s.commit()
        assert (await s.get(Task, task.id)).retry_at is None
    assert await q.lease("w2", types=["t1"]) is not None  # the wait is gone



async def test_a_clean_quit_never_revives_a_lease_a_crash_left_out_of_attempts(sm):
    """D-QUEUE-9: a task whose attempts a crash spent is reclaimed as an expired lease would be,
    never given its attempt back by the next clean quit, so a poison task still dead-letters."""
    from datetime import timedelta

    from sqlalchemy import update

    q = TaskQueue(sm)
    tid = await q.enqueue("t2", {"opportunity_id": 1}, max_attempts=3)
    for _ in range(3):  # three crashes: each lease spends an attempt and is left to expire
        assert await q.lease("w", types=["t2"], lease_seconds=900) is not None
        async with sm() as s:
            await s.execute(update(Task).where(Task.id == tid).values(
                lease_until=iso(utcnow() - timedelta(seconds=5))))
            await s.commit()
    await q.release_all_leased()
    async with sm() as s:
        assert (await s.get(Task, tid)).status == "dead"
    assert await q.lease("w", types=["t2"], lease_seconds=900) is None


async def test_a_lease_an_earlier_process_left_is_not_refunded_by_this_ones_quit(sm, monkeypatch):
    """The relaunch came inside the crashed lease's window, so the lease has not expired; it was
    claimed before this process claimed anything, so the quit reclaims it without a refund."""
    from datetime import timedelta

    from sqlalchemy import update

    from kestrel.queue import queue as qmod

    q = TaskQueue(sm)
    crashed = await q.enqueue("t2", {"opportunity_id": 1}, max_attempts=3)
    live = await q.enqueue("t2", {"opportunity_id": 2}, max_attempts=3)
    assert await q.lease("w", types=["t2"], lease_seconds=900) is not None  # crashed's
    async with sm() as s:
        await s.execute(update(Task).where(Task.id == crashed).values(
            updated_at=iso(utcnow() - timedelta(hours=1))))
        await s.commit()
    monkeypatch.setattr(qmod, "_CLAIMS_SINCE", [qmod.utcnow_iso()])  # this process starts now
    assert await q.lease("w", types=["t2"], lease_seconds=900) is not None  # live's
    await q.release_all_leased()
    async with sm() as s:
        a, b = await s.get(Task, crashed), await s.get(Task, live)
    assert (a.status, a.attempts) == ("pending", 1)  # the crash spent its attempt
    assert (b.status, b.attempts) == ("pending", 0)  # a clean quit gives it back
