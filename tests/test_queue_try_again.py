"""D-QUEUE-7: asking again for work that waits out a retry backoff runs it now.

A retryable failure sets `retry_at` (issue #153). Before this, only Score again cleared it: a
second press of Download, Draft or Build stories joined the waiting task and nothing happened for
up to ten minutes while the status rail stayed lit. Each path clears the wait of the task it
joins, through one shared helper (`queue.run_now`), and changes nothing else. Offline."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from kestrel.db.base import iso, utcnow
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Task
from kestrel.queue.queue import claimable, run_now


def _later() -> str:
    return iso(utcnow() + timedelta(minutes=10))


async def _one(sm, type_: str) -> Task:
    async with sm() as s:
        return (await s.execute(select(Task).where(Task.type == type_))).scalar_one()


async def _wait_out(sm, task_id: int) -> None:
    async with sm() as s:
        t = await s.get(Task, task_id)
        t.retry_at = _later()
        t.attempts = 1
        await s.commit()


async def _is_claimable(sm, task_id: int) -> bool:
    async with sm() as s:
        return (await s.execute(select(Task.id).where(
            Task.id == task_id, claimable(iso(utcnow()))))).first() is not None


async def test_run_now_clears_only_the_wait_of_matching_pending_tasks(sm):
    async with sm() as s:
        a = Task(type="kind_a", payload={}, retry_at=_later(), attempts=1)
        b = Task(type="kind_b", payload={}, retry_at=_later(), attempts=1)
        dead = Task(type="kind_a", payload={}, status="dead", retry_at=_later())
        s.add_all([a, b, dead])
        await s.commit()
        assert await run_now(s, Task.type == "kind_a") == 1
        await s.commit()
        for row in (a, b, dead):
            await s.refresh(row)
    assert a.retry_at is None and a.attempts == 1  # the attempt count is untouched
    assert b.retry_at is not None and dead.retry_at is not None


async def test_build_stories_again_runs_the_waiting_build_now(sm):
    from kestrel.interview import story_task

    async with sm() as s:
        assert await story_task.request_build(s)
    task = await _one(sm, story_task.TASK_TYPE)
    await _wait_out(sm, task.id)
    assert not await _is_claimable(sm, task.id)
    async with sm() as s:
        assert await story_task.request_build(s) is False  # joined, not a second build
    assert await _is_claimable(sm, task.id)
    async with sm() as s:
        n = len((await s.execute(select(Task).where(
            Task.type == story_task.TASK_TYPE))).scalars().all())
    assert n == 1


async def test_download_again_runs_the_waiting_pull_now(sm):
    from kestrel.services import model_tasks

    async with sm() as s:
        assert await model_tasks.queue_pulls(s, ["vendor-model:7b", "other-model:3b"]) == 2
        rows = (await s.execute(select(Task).where(
            Task.type == model_tasks.PULL_TYPE))).scalars().all()
    by_tag = {t.payload["tag"]: t.id for t in rows}
    for tid in by_tag.values():
        await _wait_out(sm, tid)
    async with sm() as s:
        assert await model_tasks.queue_pulls(s, ["vendor-model:7b"]) == 0
    assert await _is_claimable(sm, by_tag["vendor-model:7b"])
    assert not await _is_claimable(sm, by_tag["other-model:3b"])  # not asked again


@pytest.fixture
def ids():
    from tests import role_seed

    return asyncio.run(role_seed.seed_async())


def test_draft_again_runs_the_waiting_letter_now(ids):
    from tests.test_cover_letter_draft import GEN, _client

    with _client() as c:
        c.post(GEN.format(ids["strong"]), data={})  # on this computer: no consent step

        async def wait_out():
            sm = get_sessionmaker()
            task = await _one(sm, "letter_draft")
            await _wait_out(sm, task.id)
            return task.id

        tid = asyncio.run(wait_out())
        assert not asyncio.run(_is_claimable(get_sessionmaker(), tid))
        c.post(GEN.format(ids["strong"]), data={})  # Try again joins the waiting draft
    assert asyncio.run(_is_claimable(get_sessionmaker(), tid))

    async def count():
        async with get_sessionmaker()() as s:
            return len((await s.execute(select(Task).where(
                Task.type == "letter_draft"))).scalars().all())

    assert asyncio.run(count()) == 1
