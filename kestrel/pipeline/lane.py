"""The background lane (D-SRC-8, D-SRC-9). A role from a found company board that the smart
filter did not keep is stored and goes through Tier 0 like any other, but its first scoring
task (t1) is created in the lane: payload `lane` = 1 and a very low priority. The queue never
claims a lane task (queue.in_lane), so it cannot cause model swap thrash or delay normal work;
nothing is rejected (hard rule 4).

`release_due` runs at the end of a worker cycle and only when Kestrel is idle (no scrape, embed
or scoring task claimable and no role waiting for Tier 0). It frees up to the daily cap of lane
tasks (highest pre_score first) into the normal flow, where the usual tier draining applies:
all of Tier 1, then all of Tier 2. `release_all` is the operator's one-shot "Score everything
now" (D-SRC-9). All state is on the task rows (hard rule 5)."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Task
from kestrel.queue.queue import claimable, requested_by_operator

log = structlog.get_logger()

LANE_PRIORITY = -1000  # below any normal priority (pre_score * 100 is never negative)
LANE_REASON = "lane:company_board"  # marks Opportunity.stage_reason for the UI
DEFAULT_DAILY_CAP = 200
_BUSY_TYPES = ("scrape", "embed", "t1", "t2", "t3", "rescore")
# Seconds per role when no evaluation has been timed yet: a local model reads a posting in
# about this long. Stated to the operator as a rough figure.
ROUGH_SECONDS_PER_ROLE = 60.0


def lane_extra(pre_score: float) -> dict:
    """Payload keys that put a t1 task in the lane, remembering the priority it will have once
    released."""
    return {"lane": 1, "lane_priority": int(pre_score * 100)}


def _today(now: datetime | None = None) -> str:
    """Today on the operator's own clock (D-UI-9), not UTC: the daily cap rolls over at their
    midnight."""
    return (now or datetime.now(UTC)).astimezone().date().isoformat()


def scores_on_hosted(settings: Settings) -> bool:
    """True when Tier 1 or Tier 2 would run on an online model: the run mode is hosted, or a
    tier is assigned to an external backend. The lane never spends an online model on its own."""
    if settings.llm.mode == "hosted":
        return True
    for name in ("tier1", "tier2"):
        tier = settings.llm.tiers.get(name)
        backend = settings.llm.backends.get(tier.backend) if tier is not None else None
        if backend is not None and backend.external:
            return True
    return False


def _waiting_filter():
    """The one definition of a waiting lane task, shared by the count, release_due and
    release_all: pending, in the lane, the role still active, and not one the operator asked
    for (Score anyway or Score again), which runs on its own."""
    return (
        Task.status == "pending",
        func.coalesce(func.json_extract(Task.payload, "$.lane"), 0) == 1,
        ~requested_by_operator(),
        Opportunity.archived_at.is_(None),
    )


def _join_opportunity(stmt):
    return stmt.join(
        Opportunity, Opportunity.id == func.json_extract(Task.payload, "$.opportunity_id")
    )


async def waiting_count(session: AsyncSession, track: str | None = None) -> int:
    """Roles waiting in the lane (on one track when `track` is given)."""
    stmt = _join_opportunity(select(func.count()).select_from(Task)).where(*_waiting_filter())
    if track in ("employment", "consulting"):
        stmt = stmt.where(Opportunity.track == track)
    return (await session.execute(stmt)).scalar_one()


async def released_today(session: AsyncSession, today: str | None = None) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(func.json_extract(Task.payload, "$.lane_released_on") == (today or _today()))
        )
    ).scalar_one()


async def is_idle(session: AsyncSession) -> bool:
    """Nothing the worker could claim, and no new role waiting for Tier 0."""
    busy = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.type.in_(_BUSY_TYPES), claimable(utcnow_iso()))
        )
    ).scalar_one()
    if busy:
        return False
    new = (
        await session.execute(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.stage == "new", Opportunity.archived_at.is_(None))
        )
    ).scalar_one()
    return new == 0


def _free(task: Task, *, on: str | None) -> None:
    payload = dict(task.payload or {})
    task.priority = int(payload.pop("lane_priority", 0) or 0)
    payload["lane"] = 0
    if on:
        payload["lane_released_on"] = on
    task.payload = payload
    task.updated_at = utcnow_iso()


async def _pending_lane_tasks(session: AsyncSession) -> list[Task]:
    rows = (
        await session.execute(
            _join_opportunity(select(Task)).where(*_waiting_filter()).order_by(Task.id.asc())
        )
    ).scalars().all()
    return sorted(
        rows, key=lambda t: (-int((t.payload or {}).get("lane_priority", 0) or 0), t.id)
    )


async def release_due(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    cap: int = DEFAULT_DAILY_CAP,
    hosted: bool = False,
    now: datetime | None = None,
) -> int:
    """Free up to what is left of today's cap, only when idle. Returns how many were freed.
    When scoring runs on an online model (`hosted`), nothing is freed here: those roles wait
    for the operator's "Score everything now", which states the cost first."""
    if hosted:
        return 0
    today = _today(now)
    async with sessionmaker() as s:
        if cap <= 0 or not await is_idle(s):
            return 0
        room = cap - await released_today(s, today)
        if room <= 0:
            return 0
        tasks = (await _pending_lane_tasks(s))[:room]
        for t in tasks:
            _free(t, on=today)
        await s.commit()
    if tasks:
        log.info("lane.released", count=len(tasks))
    return len(tasks)


async def release_all(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """The operator's one-shot "Score everything now": every waiting lane task joins the normal
    flow. Does not count against the daily cap and does not stay on."""
    async with sessionmaker() as s:
        tasks = await _pending_lane_tasks(s)
        for t in tasks:
            _free(t, on=None)
        await s.commit()
    log.info("lane.released_all", count=len(tasks))
    return len(tasks)


async def seconds_per_role(session: AsyncSession) -> tuple[float, bool]:
    """(median seconds a recent scoring took, whether it is measured). Tier 1 and Tier 2
    latencies of the last 100 evaluations; the rough default when fewer than five exist."""
    ms = (
        await session.execute(
            select(Evaluation.latency_ms)
            .where(Evaluation.latency_ms > 0)
            .order_by(Evaluation.id.desc())
            .limit(100)
        )
    ).scalars().all()
    if len(ms) < 5:
        return ROUGH_SECONDS_PER_ROLE, False
    return float(median(ms)) / 1000.0, True
