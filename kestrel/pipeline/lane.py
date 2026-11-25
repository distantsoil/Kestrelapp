"""Drain the legacy background lane locally (D-SRC-13). No new roles enter this lane.
Before release and scoring, current country and title filters reject clear mismatches with
an explanation. Surviving tasks retain a local-only marker through Tier 2.

`release_due` runs at the end of a worker cycle and only when Kestrel is idle (no scrape, embed
or scoring task claimable and no role waiting for Tier 0). It frees up to the daily cap of lane
tasks (highest pre_score first) into the normal flow, where the usual tier draining applies:
all of Tier 1, then all of Tier 2. `release_all` is the operator's one-shot "Score everything
now" (D-SRC-9). All state is on the task rows (hard rule 5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from statistics import median
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Task
from kestrel.ingest import smart_filter
from kestrel.ingest.location import parse_location
from kestrel.pipeline.profile import load_profile
from kestrel.queue.queue import backing_off, claimable, requested_by_operator
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


async def prior_lane(session: AsyncSession, opportunity_id: int) -> bool:
    """Whether this role ever went through the background lane, which means a local model only
    (D-SRC-13). Read from the role's own mark, which outlives the tasks (retention deletes
    finished ones after 30 days, D-PERF-3); the task rows are kept as a second source."""
    from sqlalchemy import exists, or_

    marked = await session.scalar(
        select(Opportunity.lane_local_only).where(Opportunity.id == opportunity_id)
    )
    if marked:
        return True
    return bool(await session.scalar(
        select(exists().where(
            Task.type.in_(("t1", "t2")),
            Task.opportunity_id == opportunity_id,  # indexed copy (0030)
            or_(
                func.json_type(Task.payload, "$.lane").is_not(None),
                func.json_extract(Task.payload, f"$.{LOCAL_ONLY}") == 1,
            ),
        ))
    ))


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
    """Nothing the worker could claim or is only waiting to retry, and no new role waiting for
    Tier 0. Normal-tier work waiting out a retry backoff is still the worker's (D-QUEUE-8): the
    lane does not run ahead of it."""
    now = utcnow_iso()
    busy = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.type.in_(_BUSY_TYPES), or_(claimable(now), backing_off(now)))
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


def _waiting_now():
    """A task that is a waiting lane task as its row stands at the write: the guard of every
    update the sweep makes (D-SCORE-919 as amended). Pending, in the lane, and not one the
    person asked for since it was listed."""
    return (
        Task.status == "pending",
        func.coalesce(func.json_extract(Task.payload, "$.lane"), 0) == 1,
        ~requested_by_operator(),
    )


async def _free(
    session: AsyncSession,
    task_id: int,
    *,
    on: str | None,
    online_approval: dict[str, list[str]] | None = None,
) -> bool:
    """Release one waiting lane task into the normal flow. One guarded UPDATE that edits the
    stored payload in place (json_set): the row is released only if it is still a waiting lane
    task at the write, and nothing a person's request wrote to it since the sweep listed it is
    ever overwritten. False when it no longer was (a Score again took it out of the lane)."""
    sets: list[Any] = ["$.lane", 0, f"$.{LOCAL_ONLY}", func.json("true")]
    if on:
        sets += ["$.lane_released_on", on]
    if online_approval is not None:
        from kestrel.services.online_consent import read_approval

        approval = read_approval(online_approval)
        if approval:
            sets += ["$.online_approval", func.json(json.dumps(approval))]
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, *_waiting_now())
        .values(
            priority=func.coalesce(func.json_extract(Task.payload, "$.lane_priority"), 0),
            payload=func.json_set(func.json_remove(Task.payload, "$.lane_priority"), *sets),
            updated_at=utcnow_iso(),
        )
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, done).rowcount == 1


async def _pending_lane_tasks(session: AsyncSession) -> list[Task]:
    rows = (
        await session.execute(
            _join_opportunity(select(Task)).where(*_waiting_filter()).order_by(Task.id.asc())
        )
    ).scalars().all()
    return sorted(
        rows, key=lambda t: (-int((t.payload or {}).get("lane_priority", 0) or 0), t.id)
    )


def asked_by_person(payload: dict | None) -> bool:
    """A task the person explicitly asked for: payload `requested` or `rescore`, the same two
    keys as kestrel.queue.queue.requested_by_operator (the SQL form the lane sweep uses)."""
    return bool(payload) and bool(payload.get("requested") or payload.get("rescore"))


async def _not_ours_to_reject(
    session: AsyncSession, opp: Opportunity, payload: dict | None
) -> bool:
    """Whether the recheck must leave this role alone (issue #250, D-SCORE-919). Three questions,
    each answered in one place:
      - the task being run is one the person asked for (Score this role anyway, Score again, a
        retry): `asked_by_person`, read from the task's own payload, because a first Score this
        role anyway is recorded nowhere else until Quick sort has run;
      - the role is past Quick sort, kept by it or scored: `rescore.past_tier1`;
      - the person did something with the role (a pursuit, a label, a Save, notes, a hide and
        every other mark): `kestrel.services.person_touched.person_touched`.
    The recheck is for a role still waiting on its first model read; a filter that changed
    later never restages a role the person acted on, never finishes their request unrun, and
    never takes a kept or scored role back."""
    from kestrel.queue.rescore import past_tier1
    from kestrel.services.person_touched import person_touched

    if asked_by_person(payload) or past_tier1(opp.stage):
        return True
    return await person_touched(session, opp)


async def reject_mismatch(
    session: AsyncSession,
    opp: Opportunity,
    *,
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] | None = None,
    payload: dict | None = None,
) -> bool:
    """Recheck one legacy row using today's ingest rules, without a model or work-mode test.
    `payload` is the payload of the task about to run for this role, when there is one. Never a
    task the person asked for, a role that is the person's own, or one past Quick sort
    (`_not_ours_to_reject`, the one place that decides). It judges what it is given: a caller
    that also finishes a task goes through `settle_recheck`, which gives it fresh rows."""
    reason = await _mismatch(session, opp, contexts, payload)
    if reason is None:
        return False
    opp.stage = "t0_reject"
    opp.stage_reason = reason
    return True


async def _mismatch(
    session: AsyncSession,
    opp: Opportunity,
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] | None,
    payload: dict | None,
) -> str | None:
    """The stage reason a recheck would set for this role, or None to leave it. Changes
    nothing."""
    if await _not_ours_to_reject(session, opp, payload):
        return None
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
    return reason


REJECTED, KEPT, CHANGED = "rejected", "kept", "changed"
# A lost compare still takes the write lock, so the read after it is current and the second
# compare wins. Two tries are all a real row needs.
_COMPARE_TRIES = 2


async def _fresh_task(session: AsyncSession, task_id: int) -> Task | None:
    """The task row as it is stored now, never the copy this session read earlier."""
    return await session.get(Task, task_id, populate_existing=True)


async def settle_recheck(
    session: AsyncSession,
    task_id: int,
    *,
    worker_id: str | None = None,
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] | None = None,
) -> tuple[str, dict | None]:
    """Recheck the role of one task and write the result, deciding from the rows as they are at
    the write (issue #250, D-SCORE-919 as amended). The one place a recheck finishes a task: the
    lane sweep (a pending lane task) and the worker (`worker_id`: the task it holds leased).

    A person's Score again, Try again or Score this role anyway can commit at any moment, and
    it always writes to the task row. So nothing is decided from a copy read earlier:
      1. the task row is read fresh;
      2. a guarded UPDATE that changes nothing takes the database's write lock, and applies
         only if the row's status, owner and payload are still exactly as just read. If it does
         not apply, someone changed the row in between: read it again and compare again;
      3. with the lock held nobody else can commit, so the role and every mark of the person's
         are read fresh and `reject_mismatch` decides from them and from that payload;
      4. a rejection finishes the task with a second guarded UPDATE in the same transaction.
    The caller commits. Returns (REJECTED | KEPT | CHANGED, the payload it decided from).
    CHANGED: the row is no longer this caller's to judge (it left the lane, was taken by a
    request, or its lease moved), and nothing was written to it."""
    for _ in range(_COMPARE_TRIES):
        task = await _fresh_task(session, task_id)
        if task is None:
            return CHANGED, None
        payload = dict(task.payload or {})
        if worker_id is None:
            ours = (task.status == "pending" and (payload.get("lane") or 0) == 1
                    and not asked_by_person(payload))
        else:
            ours = task.status == "leased" and task.worker_id == worker_id
        if not ours:
            return CHANGED, payload
        as_read = (
            Task.id == task_id,
            Task.status == task.status,
            Task.worker_id.is_(None) if task.worker_id is None else (
                Task.worker_id == task.worker_id),
            # Minified on both sides: the stored text may come from json_set (compact).
            func.json(Task.payload) == func.json(json.dumps(task.payload)),
        )
        locked = await session.execute(
            update(Task).where(*as_read).values(updated_at=Task.updated_at)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, locked).rowcount == 1:
            break
    else:
        return CHANGED, None  # the read never matched the stored row: decide nothing
    opp = await session.get(Opportunity, payload.get("opportunity_id"), populate_existing=True)
    if opp is None or not await reject_mismatch(
            session, opp, contexts=contexts, payload=payload):
        return KEPT, payload
    await session.execute(
        update(Task).where(*as_read)
        .values(status="done", worker_id=None, lease_until=None, updated_at=utcnow_iso())
        .execution_options(synchronize_session=False)
    )
    return REJECTED, payload


async def stamp_leased(
    session: AsyncSession, task_id: int, worker_id: str, key: str, value: Any
) -> dict | None:
    """Set one key on the payload of a task this worker holds, in place (json_set), and return
    the payload as stored afterwards; None when the worker no longer holds it. A payload read
    at lease time is never written back: a request that stamped the row since stays on it."""
    done = await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "leased", Task.worker_id == worker_id)
        .values(payload=func.json_set(Task.payload, f"$.{key}", func.json(json.dumps(value))))
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult, done).rowcount != 1:
        return None
    task = await _fresh_task(session, task_id)
    return dict(task.payload or {}) if task is not None else None


async def _eligible_tasks(session: AsyncSession) -> list[Task]:
    """The waiting lane tasks that survive the recheck. The list is read once, so it is only a
    list of candidates: a role that looks like a mismatch is settled by `settle_recheck`, from
    the rows as they are then, and each result is committed before the next is looked at."""
    contexts: dict[str, tuple[Any, smart_filter.FilterContext]] = {}
    eligible: list[Task] = []
    for task in await _pending_lane_tasks(session):
        # The waiting filter joined the role through this payload, so both exist.
        payload = task.payload or {}
        opp = cast(Opportunity, await session.get(Opportunity, payload["opportunity_id"]))
        if await _mismatch(session, opp, contexts, payload) is None:
            eligible.append(task)
            continue
        outcome, _fresh = await settle_recheck(session, task.id, contexts=contexts)
        await session.commit()
        if outcome == KEPT:
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
        freed = 0
        for t in eligible:
            if freed >= room:
                break
            freed += await _free(s, t.id, on=today)
        await s.commit()
    if freed:
        log.info("lane.released", count=freed)
    return freed


async def release_all(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    online_approval: dict[str, list[str]] | None = None,
    hosted: bool = False,
) -> int:
    """Recheck and release surviving legacy tasks for local scoring only, outside the cap."""
    async with sessionmaker() as s:
        eligible = await _eligible_tasks(s)
        freed = 0
        for t in ([] if hosted else eligible):
            freed += await _free(s, t.id, on=None, online_approval=online_approval)
        await s.commit()
    log.info("lane.released_all", count=freed)
    return freed


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
