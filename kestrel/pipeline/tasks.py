"""Enqueue the next tier's work as a durable Task row (hard rule 5: every unit of work is a task,
so a kill-and-restart resumes cleanly). Enqueue is idempotent per (type, opportunity): re-running
Tier 0 or re-leasing a handler after a crash never creates a second pending task for the same
opportunity, so resumption neither duplicates nor loses work.

Two processes (the desktop app and the CLI) can both pass the "is one live?" check at once. For
the scoring types the database settles it: migration 0014's partial unique index allows one live
t1, t2, t3 or rescore task per (type, opportunity), and the insert is INSERT OR IGNORE, so the
loser inserts nothing and reads as "already queued" (release round review, finding 4). It runs
in the caller's transaction, atomic with the stage change as before."""

from __future__ import annotations

import structlog
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Task

# Non-terminal task states: an existing one of these means the work is already queued/underway.
_LIVE = ("pending", "leased")
# The task types migration 0014's unique index covers (one live row per type and opportunity).
UNIQUE_LIVE_TYPES = ("t1", "t2", "t3", "rescore")

log = structlog.get_logger()


async def enqueue_once(
    session: AsyncSession,
    task_type: str,
    opportunity_id: int,
    *,
    priority: int = 0,
    extra: dict | None = None,
) -> bool:
    """Add a task of `task_type` for this opportunity unless a live one already exists. Adds to the
    CALLER's session (committed by the caller, atomic with the stage change). Returns True if added.
    Matches on the JSON payload's opportunity_id via SQLite's json_extract."""
    existing = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.type == task_type,
                Task.status.in_(_LIVE),
                func.json_extract(Task.payload, "$.opportunity_id") == opportunity_id,
            )
        )
    ).scalar_one()
    if existing:
        if extra and extra.get("requested"):
            # The operator asked for work already queued in the background: mark it requested,
            # so a switched-off track's hold never keeps it back silently (review finding 10).
            await session.execute(
                update(Task)
                .where(
                    Task.type == task_type,
                    Task.status.in_(_LIVE),
                    func.json_extract(Task.payload, "$.opportunity_id") == opportunity_id,
                )
                .values(payload=func.json_set(Task.payload, "$.requested", func.json("true")))
            )
        return False
    payload = {"opportunity_id": opportunity_id}
    if extra:
        payload.update(extra)
    if task_type not in UNIQUE_LIVE_TYPES:
        session.add(Task(type=task_type, payload=payload, priority=priority))
        return True
    res = await session.execute(
        insert(Task)
        .prefix_with("OR IGNORE")
        .values(type=task_type, payload=payload, priority=priority, status="pending")
    )
    if (res.rowcount or 0) != 1:
        # Another process inserted the same live task between the check and this insert.
        log.info("enqueue_once_lost_race", type=task_type, opportunity_id=opportunity_id)
        return False
    return True
