"""The task queue: enqueue, atomic lease with expiry, heartbeat, complete, fail-with-retry,
dead-letter after max_attempts, and reclaim of expired leases.

Atomicity: lease claims a row with a single guarded UPDATE and checks rowcount == 1, so two
concurrent workers can never lease the same task (WAL serializes writers; the loser's guard fails
and it tries the next candidate). attempts increments on LEASE, so a worker that crashes without
calling fail() still consumes an attempt and the task eventually dead-letters."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog
from sqlalchemy import ColumnElement, and_, case, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import iso, utcnow, utcnow_iso
from kestrel.db.models import Opportunity, Source, Task

log = structlog.get_logger()

# Concurrent lanes commit lease, result and completion back to back; on a slow disk a waiting
# writer can lose the lock race for longer than busy_timeout and SQLite answers "database is
# locked". Every queue write is one short transaction, so it is retried whole with jittered
# backoff instead of letting the error crash the run (D-QUEUE-1). Anything else re-raises.
_LOCK_RETRIES = 6
_LOCK_BASE_DELAY_S = 0.05

# Task types whose lease is given back, not spent, when a clean restart or a stale-lease reclaim
# returns it (release round review, finding 6): a model download or build is long, and quitting
# or restarting the app mid-download must not use up its attempts. Only a real failure (fail())
# counts. Must equal services.model_tasks.TYPES (a test holds them together).
RESTART_REFUND_TYPES: tuple[str, ...] = ("ollama_pull", "ollama_create")


def _refunded_attempts() -> ColumnElement[int]:
    """attempts minus the lease that a clean release gives back, for the refund types."""
    return case(
        (
            and_(Task.type.in_(RESTART_REFUND_TYPES), Task.attempts > 0),
            Task.attempts - 1,
        ),
        else_=Task.attempts,
    )


def _claim_state(now: str) -> ColumnElement[bool]:
    """A pending row, or a leased one whose lease expired while attempts remain."""
    return or_(
        Task.status == "pending",
        and_(
            Task.status == "leased",
            Task.lease_until < now,
            Task.attempts < Task.max_attempts,
        ),
    )


def in_lane() -> ColumnElement[bool]:
    """A scoring task waiting in the background lane (D-SRC-8): a role from a found company
    board the smart filter did not keep. Never claimable until the lane releases it, unless the
    operator asked for the role (Score anyway, Score again)."""
    return and_(
        func.coalesce(func.json_extract(Task.payload, "$.lane"), 0) == 1,
        ~requested_by_operator(),
    )


def claimable(now: str, *, include_held: bool = False) -> ColumnElement[bool]:
    """What lease() may claim at `now`: a pending row, or a leased one whose lease has expired
    while attempts remain. An attempts-exhausted expired task (a worker that crashed without
    calling fail()) must NOT be re-run past max_attempts; reclaim_expired() dead-letters it.
    Pending rows never reach attempts>=max, so no guard there. Shared with the run log and Cancel
    scan, so "is there work" and "what does cancel stop" agree with what a lease would take.
    A task of a switched-off track is never claimable (D-UI-68): that track does not scan or
    score, and its tasks wait, unchanged, until it is switched back on. `include_held` drops
    that hold, for Cancel scan, which stops held scrapes too (review finding 1)."""
    can = and_(_claim_state(now), ~in_lane())
    free = None if include_held else not_held()
    return can if free is None else and_(can, free)


def in_flight(now: str) -> ColumnElement[bool]:
    """The one "is something running" predicate (review finding 1): what lease() could claim
    now, or a task under a live lease. A held task (switched-off track) is neither, so every
    count of live work (run summary, rail, scan button, run ring) agrees with the queue."""
    return or_(
        claimable(now),
        and_(Task.status == "leased", or_(Task.lease_until.is_(None), Task.lease_until >= now)),
    )


def requested_by_operator() -> ColumnElement[bool]:
    """A task the operator explicitly asked for (Score again, a quarantine retry, an MCP scan):
    payload `requested` or `rescore` is true. The track switch never holds these (finding 10)."""
    return or_(
        func.coalesce(func.json_extract(Task.payload, "$.requested"), 0) == 1,
        func.coalesce(func.json_extract(Task.payload, "$.rescore"), 0) == 1,
    )


def held_by_track(off: tuple[str, ...] | list[str]) -> ColumnElement[bool]:
    """A task that belongs to a switched-off track (D-UI-68): a scrape of one of its sources, or
    scoring of one of its roles. Such a task is never leased and is not counted as live work; it
    stays pending, so switching the track back on resumes it with nothing lost (hard rule 5).
    Background work only: a task the operator requested is never held (finding 10)."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    sid = func.json_extract(Task.payload, "$.source_id")
    belongs = or_(
        select(Opportunity.id)
        .where(Opportunity.id == oid, Opportunity.track.in_(list(off)))
        .correlate(Task)
        .exists(),
        select(Source.id)
        .where(Source.id == sid, Source.track.in_(list(off)))
        .correlate(Task)
        .exists(),
    )
    return and_(belongs, ~requested_by_operator())


def not_held() -> ColumnElement[bool] | None:
    """~held_by_track for the tracks switched off now, or None when every track is on."""
    from kestrel.services.tracks import switched_off_tracks

    off = switched_off_tracks()
    return ~held_by_track(off) if off else None


def _is_lock_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower() or "database is busy" in str(exc).lower()


async def _with_lock_retry[T](op: str, fn: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            return await fn()
        except OperationalError as exc:
            if not _is_lock_error(exc) or attempt == _LOCK_RETRIES:
                raise
            delay = _LOCK_BASE_DELAY_S * (2**attempt) * (0.5 + random.random())
            log.warning("queue_lock_retry", op=op, attempt=attempt + 1, delay_s=round(delay, 3))
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class TaskQueue:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def _enqueue(
        self, type: str, payload: dict | None = None, *, priority: int = 0, max_attempts: int = 3
    ) -> int:
        async with self._sm() as s:
            task = Task(
                type=type, payload=payload or {}, priority=priority, max_attempts=max_attempts
            )
            s.add(task)
            await s.commit()
            return task.id

    async def _lease(
        self,
        worker_id: str,
        *,
        types: list[str] | None = None,
        lease_seconds: int = 300,
        where: ColumnElement[bool] | None = None,
    ) -> int | None:
        """Claim one task and return its id. Nothing runs after the commit, so a retry of this
        whole call can never claim twice; the caller reads the row separately. `where` narrows
        the candidates further (the worker's per-model Tier 2 drain)."""
        now = utcnow_iso()
        until = iso(utcnow() + timedelta(seconds=lease_seconds))
        can_claim = claimable(now)
        async with self._sm() as s:
            for _ in range(8):  # a few candidates, in case of a lost race
                sel = select(Task.id).where(can_claim)
                if types:
                    sel = sel.where(Task.type.in_(types))
                if where is not None:
                    sel = sel.where(where)
                sel = sel.order_by(Task.priority.desc(), Task.id.asc()).limit(1)
                task_id = (await s.execute(sel)).scalar_one_or_none()
                if task_id is None:
                    return None
                res = await s.execute(
                    update(Task)
                    .where(Task.id == task_id, can_claim)
                    .values(
                        status="leased",
                        worker_id=worker_id,
                        lease_until=until,
                        # Re-claiming an expired lease of a refund type gives the dead lease's
                        # attempt back first (finding 6); SET reads the row's old values.
                        attempts=case(
                            (
                                and_(
                                    Task.status == "leased",
                                    Task.type.in_(RESTART_REFUND_TYPES),
                                    Task.attempts > 0,
                                ),
                                Task.attempts,
                            ),
                            else_=Task.attempts + 1,
                        ),
                        updated_at=now,
                    )
                )
                await s.commit()
                if res.rowcount == 1:
                    return task_id
            return None

    async def _heartbeat(self, task_id: int, worker_id: str, *, lease_seconds: int = 300) -> bool:
        async with self._sm() as s:
            res = await s.execute(
                update(Task)
                .where(Task.id == task_id, Task.worker_id == worker_id, Task.status == "leased")
                .values(
                    lease_until=iso(utcnow() + timedelta(seconds=lease_seconds)),
                    updated_at=utcnow_iso(),
                )
            )
            await s.commit()
            return res.rowcount == 1

    async def _complete(self, task_id: int, worker_id: str | None = None) -> bool:
        """Mark a leased task done. If worker_id is given, only the owning worker succeeds, so a
        stale worker whose lease expired cannot clobber a task re-leased to someone else."""
        async with self._sm() as s:
            guard = [Task.id == task_id, Task.status == "leased"]
            if worker_id is not None:
                guard.append(Task.worker_id == worker_id)
            res = await s.execute(
                update(Task).where(*guard).values(status="done", updated_at=utcnow_iso())
            )
            await s.commit()
            return res.rowcount == 1

    async def _fail(
        self, task_id: int, error: str, *, worker_id: str | None = None, retry: bool = True
    ) -> str:
        """Record the error and either requeue (retry, if attempts remain) or dead-letter. If
        worker_id is given and no longer owns the leased task, do nothing (returns 'not_owned')."""
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            if task is None:
                return "missing"
            if worker_id is not None and (task.worker_id != worker_id or task.status != "leased"):
                return "not_owned"
            task.last_error = (error or "")[:4000]
            task.updated_at = utcnow_iso()
            if retry and task.attempts < task.max_attempts:
                task.status = "pending"
                task.worker_id = None
                task.lease_until = None
            else:
                task.status = "dead"
            await s.commit()
            return task.status

    async def _reclaim_expired(self) -> int:
        """Return expired leases to the queue; dead-letter those already out of attempts."""
        now = utcnow_iso()
        expired = and_(Task.status == "leased", Task.lease_until < now)
        async with self._sm() as s:
            refund = Task.type.in_(RESTART_REFUND_TYPES)
            dead = await s.execute(
                update(Task)
                .where(expired, ~refund, Task.attempts >= Task.max_attempts)
                .values(status="dead", updated_at=now)
            )
            pend = await s.execute(
                update(Task)
                .where(expired, or_(refund, Task.attempts < Task.max_attempts))
                .values(status="pending", worker_id=None, lease_until=None,
                        attempts=_refunded_attempts(), updated_at=now)
            )
            await s.commit()
            return (dead.rowcount or 0) + (pend.rowcount or 0)

    async def _release_all_leased(self) -> int:
        """Return EVERY currently-leased task to pending, clearing its lease. Called on a graceful
        quit after the worker has been signalled and given time to finish (SPEC.md 1.3, F8): it lets
        a restart resume immediately instead of waiting for each lease to expire. Handlers are
        idempotent on stage, so re-running a released in-flight task is safe."""
        now = utcnow_iso()
        async with self._sm() as s:
            res = await s.execute(
                update(Task)
                .where(Task.status == "leased")
                .values(status="pending", worker_id=None, lease_until=None,
                        attempts=_refunded_attempts(), updated_at=now)
            )
            await s.commit()
            return res.rowcount or 0

    async def enqueue(
        self, type: str, payload: dict | None = None, *, priority: int = 0, max_attempts: int = 3
    ) -> int:
        return await _with_lock_retry(
            "enqueue",
            lambda: self._enqueue(type, payload, priority=priority, max_attempts=max_attempts),
        )

    async def lease(
        self,
        worker_id: str,
        *,
        types: list[str] | None = None,
        lease_seconds: int = 300,
        where: ColumnElement[bool] | None = None,
    ) -> Task | None:
        task_id = await _with_lock_retry(
            "lease",
            lambda: self._lease(
                worker_id, types=types, lease_seconds=lease_seconds, where=where
            ),
        )
        if task_id is None:
            return None
        # The claim is committed; re-reading the row is idempotent, so it retries on its own.
        return await _with_lock_retry("lease_read", lambda: self.get(task_id))

    async def heartbeat(self, task_id: int, worker_id: str, *, lease_seconds: int = 300) -> bool:
        return await _with_lock_retry(
            "heartbeat", lambda: self._heartbeat(task_id, worker_id, lease_seconds=lease_seconds)
        )

    async def complete(self, task_id: int, worker_id: str | None = None) -> bool:
        return await _with_lock_retry("complete", lambda: self._complete(task_id, worker_id))

    async def fail(
        self, task_id: int, error: str, *, worker_id: str | None = None, retry: bool = True
    ) -> str:
        return await _with_lock_retry(
            "fail", lambda: self._fail(task_id, error, worker_id=worker_id, retry=retry)
        )

    async def reclaim_expired(self) -> int:
        return await _with_lock_retry("reclaim_expired", self._reclaim_expired)

    async def release_all_leased(self) -> int:
        return await _with_lock_retry("release_all_leased", self._release_all_leased)

    async def get(self, task_id: int) -> Task | None:
        async with self._sm() as s:
            return await s.get(Task, task_id)
