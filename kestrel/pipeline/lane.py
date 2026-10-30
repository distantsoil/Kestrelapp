"""Drain the legacy background lane locally (D-SRC-13). No new roles enter this lane.
Before release and scoring, current country and title filters reject clear mismatches with
an explanation. Surviving tasks retain a local-only marker through Tier 2.

`release_due` runs at the end of a worker cycle and only when Kestrel is idle (no scrape, embed
or scoring task claimable and no role waiting for Tier 0). It frees up to the daily cap of lane
tasks (highest pre_score first) into the normal flow, where the usual tier draining applies:
all of Tier 1, then all of Tier 2. `release_all` is the operator's one-shot "Score everything
now" (D-SRC-9). All state is on the task rows (hard rule 5)."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Task
from kestrel.ingest import smart_filter
from kestrel.ingest.location import parse_location
from kestrel.pipeline.profile import load_profile
from kestrel.queue.queue import claimable, requested_by_operator
from kestrel.rules.helpers import value_in

log = structlog.get_logger()

LANE_PRIORITY = -1000  # below any normal priority (pre_score * 100 is never negative)
LANE_REASON = "lane:company_board"  # marks Opportunity.stage_reason for the UI
LOCAL_ONLY = "lane_local_only"
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


def _free(
    task: Task,
    *,
    on: str | None,
    online_approval: dict[str, list[str]] | None = None,
) -> None:
    payload = dict(task.payload or {})
    task.priority = int(payload.pop("lane_priority", 0) or 0)
    payload["lane"] = 0
    payload[LOCAL_ONLY] = True
    if on:
        payload["lane_released_on"] = on
    if online_approval is not None:
        from kestrel.services.online_consent import read_approval

        approval = read_approval(online_approval)
        if approval:
            payload["online_approval"] = approval
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


async def reject_mismatch(
    session: AsyncSession,
    opp: Opportunity,
    *,
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] | None = None,
) -> bool:
    """Recheck one legacy row using today's ingest rules, without a model or work-mode test."""
    cache = contexts if contexts is not None else {}
    if opp.track not in cache:
        profile = load_profile(opp.track)
        cache[opp.track] = (
            profile, await smart_filter.build_context(session, opp.track, profile)
        )
    profile, ctx = cache[opp.track]
    countries = getattr(getattr(profile, "geo_rules", None), "remote_country_allowlist", None)
    country = parse_location(opp.location_raw)[0] or opp.country
    reason = None
    if country and countries and not value_in(country, countries):
        reason = "outside_area: Outside your search countries."
    elif not smart_filter.keep(opp.title, ctx):
        reason = "unrelated_title: Title does not match your target titles."
    if reason is None:
        return False
    opp.stage = "t0_reject"
    opp.stage_reason = reason
    return True


async def _eligible_tasks(session: AsyncSession) -> list[Task]:
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] = {}
    eligible: list[Task] = []
    for task in await _pending_lane_tasks(session):
        opp = await session.get(Opportunity, task.payload["opportunity_id"])
        if await reject_mismatch(session, opp, contexts=contexts):
            task.status = "done"
            task.updated_at = utcnow_iso()
        else:
            eligible.append(task)
    return eligible


async def release_due(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    cap: int = DEFAULT_DAILY_CAP,
    hosted: bool = False,
    now: datetime | None = None,
) -> int:
    """Recheck the backlog, then free today's remaining share when idle and scoring is local.
    Hosted settings hold survivors; mismatches are rejected without model calls regardless."""
    today = _today(now)
    async with sessionmaker() as s:
        eligible = await _eligible_tasks(s)
        await s.commit()  # rejection cleanup also runs while hosted, busy or capped
        if hosted:
            return 0
        if cap <= 0 or not await is_idle(s):
            return 0
        room = cap - await released_today(s, today)
        if room <= 0:
            return 0
        tasks = eligible[:room]
        for t in tasks:
            _free(t, on=today)
        await s.commit()
    if tasks:
        log.info("lane.released", count=len(tasks))
    return len(tasks)


async def release_all(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    online_approval: dict[str, list[str]] | None = None,
    hosted: bool = False,
) -> int:
    """Recheck and release surviving legacy tasks for local scoring only, outside the cap."""
    async with sessionmaker() as s:
        eligible = await _eligible_tasks(s)
        tasks = [] if hosted else eligible
        for t in tasks:
            _free(t, on=None, online_approval=online_approval)
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
