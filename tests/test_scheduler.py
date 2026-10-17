"""Scheduler: enqueue a scrape per due enabled source, and never double-enqueue."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Source, Task
from kestrel.ingest.scheduler import enqueue_due_scrapes
from kestrel.queue import TaskQueue


async def test_enqueue_due_and_no_double_enqueue(sm):
    async with sm() as s:
        s.add_all(
            [
                Source(
                    track="employment",
                    kind="greenhouse",
                    name="due",
                    config={"token": "x"},
                    enabled=True,
                    cadence_hours=12,
                    last_run_at=None,
                ),  # never run -> due
                Source(
                    track="employment",
                    kind="greenhouse",
                    name="recent",
                    config={"token": "y"},
                    enabled=True,
                    cadence_hours=12,
                    last_run_at=utcnow_iso(),
                ),  # just ran -> not due
                Source(
                    track="employment",
                    kind="greenhouse",
                    name="off",
                    config={"token": "z"},
                    enabled=False,
                    last_run_at=None,
                ),  # disabled -> skipped
            ]
        )
        await s.commit()

    queue = TaskQueue(sm)
    async with sm() as s:
        first = await enqueue_due_scrapes(s, queue)
    assert first == 1  # only "due"

    async with sm() as s:
        scrapes = (
            await s.execute(select(func.count()).select_from(Task).where(Task.type == "scrape"))
        ).scalar()
    assert scrapes == 1

    # a second pass must NOT enqueue again (a pending scrape already exists for that source)
    async with sm() as s:
        second = await enqueue_due_scrapes(s, queue)
    assert second == 0
