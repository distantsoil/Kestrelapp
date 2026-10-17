"""D-QUEUE-1: a queue write that meets "database is locked" is retried whole with backoff; any
other database error, or a lock that outlasts the retries, still raises."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from kestrel.queue import queue as q


def _locked() -> OperationalError:
    return OperationalError("UPDATE task ...", {}, Exception("database is locked"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_s):
        return None

    monkeypatch.setattr(q.asyncio, "sleep", _instant)


async def test_lock_error_is_retried_until_it_clears():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _locked()
        return "ok"

    assert await q._with_lock_retry("lease", op) == "ok"
    assert calls["n"] == 3


async def test_other_operational_errors_are_not_retried():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise OperationalError("SELECT", {}, Exception("no such table: task"))

    with pytest.raises(OperationalError, match="no such table"):
        await q._with_lock_retry("lease", op)
    assert calls["n"] == 1


async def test_a_lock_that_outlasts_the_retries_still_raises():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _locked()

    with pytest.raises(OperationalError, match="locked"):
        await q._with_lock_retry("complete", op)
    assert calls["n"] == q._LOCK_RETRIES + 1


async def test_queue_lease_goes_through_the_retry(sm, monkeypatch):
    queue = q.TaskQueue(sm)
    task_id = await queue.enqueue("t1", {"opportunity_id": 1})
    real = queue._lease
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _locked()
        return await real(*a, **k)

    monkeypatch.setattr(queue, "_lease", flaky)
    task = await queue.lease("w-0", types=["t1"])
    assert task is not None and task.id == task_id and task.attempts == 1  # one lease, not two


async def test_a_lock_on_the_read_after_a_claim_never_claims_a_second_task(sm, monkeypatch):
    queue = q.TaskQueue(sm)
    first = await queue.enqueue("t1", {"opportunity_id": 1})
    await queue.enqueue("t1", {"opportunity_id": 2})
    real_get = queue.get
    calls = {"n": 0}

    async def flaky_get(task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _locked()
        return await real_get(task_id)

    monkeypatch.setattr(queue, "get", flaky_get)
    task = await queue.lease("w-0", types=["t1"])
    assert task.id == first and task.attempts == 1
    monkeypatch.setattr(queue, "get", real_get)
    second = await real_get(first + 1)
    assert second.status == "pending" and second.attempts == 0  # untouched
