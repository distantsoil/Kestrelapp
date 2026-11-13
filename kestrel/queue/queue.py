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
from datetime import datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import ColumnElement, CursorResult, and_, case, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import iso, utcnow, utcnow_iso
from kestrel.db.locks import is_lock_error
from kestrel.db.models import Opportunity, Source, Task

log = structlog.get_logger()

# Concurrent lanes commit lease, result and completion back to back; on a slow disk a waiting
# writer can lose the lock race for longer than busy_timeout and SQLite answers "database is
# locked". Every queue write is one short transaction, so it is retried whole with jittered
# backoff instead of letting the error crash the run (D-QUEUE-1). Anything else re-raises.
_LOCK_RETRIES = 6
_LOCK_BASE_DELAY_S = 0.05

# Task types whose expired lease is given back, not spent, when a stale-lease reclaim returns
# it (release round review, finding 6): a model download or build is long, and a crash or a
# restart mid-download must not use up its attempts. A clean quit gives the in-flight attempt
# back for EVERY type (issue #153, D-QUEUE-3); only a real failure (fail()) counts otherwise.
# Must equal services.model_tasks.TYPES (a test holds them together).
RESTART_REFUND_TYPES: tuple[str, ...] = ("ollama_pull", "ollama_create")

# issue #153 (D-QUEUE-3): a retryable failure backs off before it is claimable again, by the
# attempts already spent: 30 s after the first failed attempt, 2 min after the second, 10 min
# after the third and any later one. "Try again" clears the wait (rescore._upgrade_waiting).
_RETRY_BACKOFF_S = (30, 120, 600)


# Every claimable or in-flight arm matches only these two statuses; stating the set once as a
# plain prefilter lets the planner use the status index (D-PERF).
LIVE_STATUSES: tuple[str, ...] = ("pending", "leased")


def _backoff_seconds(attempts: int) -> int:
    attempts = max(1, attempts)
    return _RETRY_BACKOFF_S[min(attempts, len(_RETRY_BACKOFF_S)) - 1]


# When this process first claimed a task (D-QUEUE-9). A lease whose claim is older belongs to an
# earlier process that crashed, and a clean quit must not give its attempt back. Set on the first
# claim, never at import (D-TEST-3).
_CLAIMS_SINCE: list[str] = []


def _mark_claims() -> str:
    if not _CLAIMS_SINCE:
        _CLAIMS_SINCE.append(utcnow_iso())
    return _CLAIMS_SINCE[0]


def _refunded_attempts() -> ColumnElement[int]:
    """attempts minus the lease that a stale-lease reclaim gives back, for the refund types."""
    return case(
        (
            and_(Task.type.in_(RESTART_REFUND_TYPES), Task.attempts > 0),
            Task.attempts - 1,
        ),
        else_=Task.attempts,
    )


def _claim_state(now: str) -> ColumnElement[bool]:
    """A pending row whose backoff, if any, has passed (issue #153), or a leased one whose
    lease expired while attempts remain."""
    ready = or_(Task.retry_at.is_(None), Task.retry_at <= now)
    # The IN list is redundant with the arms below (D-PERF); it lets SQLite pick
    # ix_task_status_priority instead of scanning the whole task table per correlated probe.
    return and_(
        Task.status.in_(LIVE_STATUSES),
        or_(
            and_(Task.status == "pending", ready),
            and_(
                Task.status == "leased",
                Task.lease_until < now,
                Task.attempts < Task.max_attempts,
            ),
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


def _claimable_base(now: str) -> ColumnElement[bool]:
    """What a lease could take once the deferred mark is tested (D-QUEUE-3): a pending row, or a
    leased one whose lease has expired while attempts remain, out of the lane, and of a track that
    is on. `claimable` is this plus `~is_deferred()`, so the queue never leases a held task while
    its mark is set; `in_flight` keeps it, so a held role stays visible as stalled on the
    Pipeline (#143) while the queue sits still."""
    base = and_(_claim_state(now), ~in_lane())
    free = not_held()
    return base if free is None else and_(base, free)


def claimable(
    now: str, *, include_held: bool = False, include_deferred: bool = False
) -> ColumnElement[bool]:
    """What lease() may claim at `now`: a pending row, or a leased one whose lease has expired
    while attempts remain. A pending row still in a backoff (retry_at in the future, issue
    #153) is not claimable until it passes. An attempts-exhausted expired task (a worker that
    crashed without
    calling fail()) must NOT be re-run past max_attempts; reclaim_expired() dead-letters it.
    Pending rows never reach attempts>=max, so no guard there. Shared with the run log and Cancel
    scan, so "is there work" and "what does cancel stop" agree with what a lease would take.
    A task of a switched-off track is never claimable (D-UI-68): that track does not scan or
    score, and its tasks wait, unchanged, until it is switched back on. `include_held` drops
    that hold, for Cancel scan, which stops held scrapes too (review finding 1).
    A deferred task is never claimable while its mark is set (D-QUEUE-3): nothing leases it,
    rewrites it or spends its attempts for as long as the hold lasts. `include_deferred` is
    only for the operator's Cancel scan: a deliberate command is not a claim, so it may stop a
    held scrape (an approval hold, D-QUEUE-3); it still never reaches a live lease, and the
    mark is cleared with the cancellation so the deferred count stays honest."""
    base = (
        and_(_claim_state(now), ~in_lane())
        if include_held
        else _claimable_base(now)
    )
    if include_deferred:
        return base
    return and_(base, ~is_deferred())


def in_flight(now: str) -> ColumnElement[bool]:
    """The one "is something running" predicate (review finding 1): what a lease could take
    (once the deferred mark and the retry backoff are both tested), a pending task waiting out
    a retry backoff (issue #153: it runs on its own when the wait passes, so it is still live
    work), or a task under a live lease. A deferred task counts as live work, so a held role
    stays visible as stalled (#143) while `claimable` keeps the queue from leasing it. A task
    of a switched-off track, or held for the person's online approval, is neither: it waits for
    the track or the person, so it never keeps a page polling or a status rail lit (D-UI-239),
    and every count of live work (run summary, rail, scan button, run ring) agrees with the
    queue."""
    from kestrel.services.online_consent import APPROVAL_HOLD

    # A task held for the person's online approval waits for them, not for the worker: it is not
    # live work, so it never keeps a page polling or a status rail lit (D-UI-239 addition).
    awaiting_person = func.coalesce(Task.last_error, "") == APPROVAL_HOLD
    backoff_pending = and_(
        Task.status == "pending", Task.retry_at.is_not(None), ~in_lane())
    held = not_held()
    backoff_pending = backoff_pending if held is None else and_(backoff_pending, held)
    return and_(
        Task.status.in_(LIVE_STATUSES),  # redundant prefilter for the planner (D-PERF)
        or_(
            _claimable_base(now),
            backoff_pending,
            and_(Task.status == "leased", or_(Task.lease_until.is_(None), Task.lease_until >= now)),
        ),
        ~awaiting_person,
    )


def backing_off(now: str) -> ColumnElement[bool]:
    """A pending task waiting out a retry backoff (issue #153): it runs on its own once
    `retry_at` passes. Out of the lane and not held by the deferred mark."""
    return and_(
        Task.status == "pending", Task.retry_at.is_not(None), Task.retry_at > now,
        ~in_lane(), ~is_deferred(),
    )


async def run_now(session: AsyncSession, where: ColumnElement[bool]) -> int:
    """The person asked again for work that is already waiting (D-QUEUE-7): clear the retry
    backoff of the matching pending tasks, so the request they joined runs at the next pass
    instead of up to ten minutes later. Only `retry_at` changes: a held task keeps its mark and
    its hold, and nothing is leased or sent from here. The caller commits."""
    res = await session.execute(
        update(Task)
        .where(where, Task.status == "pending", Task.retry_at.is_not(None))
        .values(retry_at=None, updated_at=utcnow_iso())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, res).rowcount or 0


async def deferred_count(sm: async_sessionmaker[AsyncSession]) -> int:
    """Live deferred tasks, for the per-cycle measurement (#154)."""
    async with sm() as s:
        return (
            await s.execute(select(func.count())
                .select_from(Task)
                # The mark is only ever set on a pending row (D-PERF): the status test keeps the
                # per-cycle count off the finished rows and on ix_task_status_priority.
                .where(Task.status.in_(LIVE_STATUSES), is_deferred()))
        ).scalar_one()


# The task type the letter drafts use (letter_task.TASK_TYPE): its holds are the person's
# consent, and letter_task's own drain releases and re-checks them, so the queue-level release
# pass never touches them.
_LETTER_TASK_TYPE = "letter_draft"


def _release_settings(settings: Settings, task_type: str, payload: dict) -> Settings:
    """The effective settings for one task, resolved exactly as the worker's `_task_settings`
    does (D-UI-50): a Score again may name another model (`model_override`), and the worker's
    hold tests cover both the configured tier and that model, so the release tests must too, or
    a task whose override still holds is released and re-deferred, the #154 churn. An override
    that can no longer run falls back to the configured model, the worker's own fallback
    (hard rule 4). Tier 1 honors an override only on a Score again (D-UI-72 amended)."""
    tier = _TIER_OF_TYPE.get(task_type)
    if tier is None:
        return settings
    override = payload.get("model_override")
    if tier == "tier1" and not payload.get("rescore"):
        override = None
    if not override:
        return settings
    from kestrel.pipeline._shared import ModelOverrideError, settings_with_override

    try:
        return settings_with_override(settings, tier, override)
    except ModelOverrideError:
        return settings  # the worker falls back to the configured model and judges the hold there


async def release_holds(sm: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    """The owner of the deferred mark (D-QUEUE-3): test each deferred task's hold and clear the
    mark when the hold ended, so the next lease claims the task exactly once. Nothing is sent
    from here: a released task still passes its hold's check at run time, and a task whose hold
    is still live is left marked.

    Release by reason: approval ends when the confirmation now covers the actual destinations
    (the person re-confirmed); local-only ends when the tiers no longer score on hosted (mode or
    model switched); credit ends when the backend's hold is gone (cleared, or its window
    elapsed); a stop-defer ends the moment a live loop runs this pass.
    Safety sweep (hard rule 5): a deferred task at least SWEEP_SECONDS old is released for a
    trial lease no matter its reason, so a missed release event cannot strand work forever.
    Letter drafts are out (see _LETTER_TASK_TYPE). `last_error` is cleared with the mark: the
    hold text is a hold-state note, and a stale one would read like a failure. Returns the count
    released."""
    from kestrel.llm import credit
    from kestrel.llm.client import tier_credit_hold
    from kestrel.pipeline import lane
    from kestrel.services.online_consent import held_for_approval

    now = utcnow()

    async def _once() -> int:
        released = 0
        async with sm() as s:
            rows = (
                await s.execute(
                    select(Task).where(
                        Task.status == "pending",
                        is_deferred(),
                        Task.type != _LETTER_TASK_TYPE,
                    )
                )
            ).scalars().all()
            for task in rows:
                payload = task.payload or {}
                reason = payload.get(DEFER_REASON_KEY)
                tier = _TIER_OF_TYPE.get(task.type)
                # The worker's hold tests cover the task's model override as well as the
                # configured tier (D-UI-50); the release must too, or an override that still
                # holds is released and re-deferred, the #154 churn.
                task_settings = _release_settings(settings, task.type, payload)
                if reason == REASON_STOPPED:
                    hold_gone = True
                elif reason == REASON_LOCAL_ONLY:
                    hold_gone = not (
                        lane.scores_on_hosted(settings)
                        or lane.scores_on_hosted(task_settings)
                    )
                elif reason == REASON_CREDIT:
                    # t1/t2 credits are also blocked at lease time (_hold_exclusion covers the
                    # override); the rest run on a named tier or any backend, tested as deferred.
                    hold_gone = (
                        tier_credit_hold(settings, tier) is None
                        if tier is not None
                        else not credit.any_held()
                    )
                elif reason == REASON_APPROVAL:
                    hold_gone = (
                        tier is not None
                        and not held_for_approval(settings, (tier,), payload)
                        and not held_for_approval(task_settings, (tier,), payload)
                    )
                elif reason == REASON_CORRECTIONS:
                    from kestrel.learning.correction_lines import held_for_corrections

                    hold_gone = not await held_for_corrections(
                        s, (settings, task_settings), payload
                    )
                else:  # no reason recorded (a defer before the reason key): the sweep only
                    hold_gone = False
                if not hold_gone:
                    age = now - datetime.fromisoformat(task.updated_at or utcnow_iso())
                    hold_gone = age >= timedelta(seconds=SWEEP_SECONDS)
                if not hold_gone:
                    continue
                # Guarded: if the row was leased in the meantime, the update matches nothing
                # and nothing is written to a running task.
                new_payload = {
                    k: v for k, v in payload.items() if k not in (DEFERRED_KEY, DEFER_REASON_KEY)
                }
                res = await s.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status == "pending", is_deferred())
                    .values(
                        payload=new_payload or None,
                        last_error=None,
                        updated_at=utcnow_iso(),
                    )
                )
                released += cast(CursorResult, res).rowcount or 0
            await s.commit()
        return released

    # Like every TaskQueue write (D-QUEUE-1): a transient lock is retried here, and the
    # caller's guard (worker cycle, online lane) is the backstop for anything else.
    return await _with_lock_retry("release_holds", _once)


DEFERRED_KEY = "deferred"

# Why a deferred task is held (D-QUEUE-3), in `payload`: the machine-readable reason that
# release_holds() tests to know when the hold has ended. The operator-facing text stays in
# `last_error`. A task deferred before this key existed carries no reason; its release is the
# safety sweep, which is exactly the old behavior (the worker re-checked and re-deferred).
DEFER_REASON_KEY = "defer_reason"
REASON_LOCAL_ONLY = "local_only"
REASON_APPROVAL = "approval"
# Held because the correction lines changed since the operator confirmed the online send
# (D-SCORE-3). It ends when the lines match the confirmation again or Tier 2 no longer goes
# online; a new confirmation replaces the hold (queue.rescore._reconfirm_held).
REASON_CORRECTIONS = "corrections"
REASON_CREDIT = "credit"
REASON_STOPPED = "stopped"

# The safety sweep interval (D-QUEUE-3): a deferred task older than this is released for a trial
# lease no matter its reason, so a missed release event cannot strand work forever (hard rule 5).
# The sweep never sends anything: the run re-checks its hold before it sends and defers again if
# the hold is still live, so the worst case is one lease and one defer per interval, not one per
# cycle as before.
SWEEP_SECONDS = 60

# The credit tier a task type resolves to, for the per-tier credit-hold test (the same pairing
# the worker uses in held_types, D-LLM-8). Types not listed fall back to "any backend on hold".
_TIER_OF_TYPE = {"t1": "tier1", "t2": "tier2"}


def is_deferred() -> ColumnElement[bool]:
    """A pending task given back unstarted by `defer` (a key out of credit, a local-only hold):
    it cannot run now, whatever its attempts. A task that merely failed and was requeued is not
    deferred; it is next in line again. `lease` and `fail` clear the mark."""
    return func.coalesce(func.json_extract(Task.payload, f"$.{DEFERRED_KEY}"), 0) == 1


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


_is_lock_error = is_lock_error  # the shared check (kestrel/db/locks.py); name kept for callers


async def _with_lock_retry[T](op: str, fn: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            return await fn()
        except OperationalError as exc:
            if not _is_lock_error(exc) or attempt == _LOCK_RETRIES:
                raise
            delay = _LOCK_BASE_DELAY_S * (2**attempt) * (0.5 + random.random())  # noqa: S311  retry jitter, not security
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
        _mark_claims()
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
                        payload=func.json_remove(Task.payload, f"$.{DEFERRED_KEY}"),
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
            if task.payload and DEFERRED_KEY in task.payload:
                task.payload = {k: v for k, v in task.payload.items() if k != DEFERRED_KEY}
            if retry and task.attempts < task.max_attempts:
                task.status = "pending"
                task.worker_id = None
                task.lease_until = None
                # Back off before it is claimable again (issue #153); "Try again" clears
                # retry_at and runs it now (rescore._upgrade_waiting).
                task.retry_at = iso(utcnow() + timedelta(seconds=_backoff_seconds(task.attempts)))
            else:
                task.status = "dead"
                task.retry_at = None
            await s.commit()
            return task.status

    async def _defer(
        self, task_id: int, note: str, *, worker_id: str | None = None, reason: str | None = None
    ) -> str:
        """Give a leased task back unstarted: pending again with the lease's attempt refunded and
        the reason kept in `last_error`. For a backend that cannot serve it right now (an online
        key out of credit, D-LLM-8): the work is neither failed nor lost, and nothing is retried
        until the operator, or the hold timing out, lets sends resume. `reason` names which hold
        set the mark (D-QUEUE-3), in `payload`, so release_holds() knows what ended it; without
        it only the safety sweep releases (legacy behavior, unchanged)."""
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            if task is None:
                return "missing"
            if worker_id is not None and (task.worker_id != worker_id or task.status != "leased"):
                return "not_owned"
            task.last_error = (note or "")[:4000]
            task.updated_at = utcnow_iso()
            payload = {**(task.payload or {}), DEFERRED_KEY: True}
            if reason is not None:
                payload[DEFER_REASON_KEY] = reason
            task.payload = payload
            task.status = "pending"
            task.worker_id = None
            task.lease_until = None
            task.attempts = max(0, (task.attempts or 0) - 1)
            await s.commit()
            return task.status

    async def _give_back(
        self, task_id: int, payload: dict, *, worker_id: str | None = None
    ) -> str:
        """Return a leased task that made progress and stopped on purpose to let other work run
        (a story build's pass, D-STORY-2): pending again with the lease's attempt refunded, its
        payload replaced by `payload` (where the work got to), and no error recorded. Unlike
        `defer` it is not a hold: the next lease takes it as soon as the worker gets back to it."""
        async with self._sm() as s:
            task = await s.get(Task, task_id)
            if task is None:
                return "missing"
            if worker_id is not None and (task.worker_id != worker_id or task.status != "leased"):
                return "not_owned"
            task.payload = {k: v for k, v in payload.items() if k != DEFERRED_KEY}
            task.updated_at = utcnow_iso()
            task.status = "pending"
            task.worker_id = None
            task.lease_until = None
            task.attempts = max(0, (task.attempts or 0) - 1)
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

    async def _release_all_leased(self, keep_worker_ids: tuple[str, ...] = ()) -> int:
        """Return EVERY currently-leased task to pending, clearing its lease and giving the
        in-flight attempt back for every task type (issue #153, D-QUEUE-3: a clean quit is
        not a failure). Called on a graceful quit after the worker has been signalled and
        given time to finish (SPEC.md 1.3, F8): it lets a restart resume immediately instead
        of waiting for each lease to expire. Handlers are idempotent on stage, so re-running
        a released in-flight task is safe.

        Only leases this process claimed are given back that way (D-QUEUE-9). A lease an earlier
        process left when it crashed is still `leased` after a relaunch (its lease has usually
        not expired, so the start-up reclaim kept it): its attempt was spent by the crash, so it
        is reclaimed as an expired lease would be, out of attempts going to dead-letter, never
        revived with an attempt refunded. So is a lease of this process that already expired
        (its holder stopped renewing it).

        `keep_worker_ids` (D-QUEUE-10): leases held under these worker ids are left exactly as
        they are. A restore or a reset stops the worker and the online lane only, while the
        process (and the setup loop's model download or build) keeps running; handing that
        lease back made its completion fail the ownership check and the pull or build run
        again. A quit passes nothing: every lease is handed back."""
        now = utcnow_iso()
        # Live in this process: claimed (or renewed) since its first claim, lease not expired.
        mine = (
            and_(Task.updated_at >= _CLAIMS_SINCE[0], Task.lease_until >= now)
            if _CLAIMS_SINCE else None
        )
        leased = Task.status == "leased"
        if keep_worker_ids:
            leased = and_(leased, or_(Task.worker_id.is_(None),
                                      Task.worker_id.not_in(keep_worker_ids)))
        async with self._sm() as s:
            n = 0
            if mine is not None:
                res = await s.execute(
                    update(Task)
                    .where(leased, mine)
                    .values(status="pending", worker_id=None, lease_until=None, retry_at=None,
                            attempts=case((Task.attempts > 0, Task.attempts - 1), else_=0),
                            updated_at=now)
                )
                n += cast(CursorResult, res).rowcount or 0
            refund = Task.type.in_(RESTART_REFUND_TYPES)
            dead = await s.execute(
                update(Task)
                .where(leased, ~refund, Task.attempts >= Task.max_attempts)
                .values(status="dead", worker_id=None, lease_until=None, updated_at=now)
            )
            pend = await s.execute(
                update(Task)
                .where(leased, or_(refund, Task.attempts < Task.max_attempts))
                .values(status="pending", worker_id=None, lease_until=None, retry_at=None,
                        attempts=_refunded_attempts(), updated_at=now)
            )
            await s.commit()
            return (n + (cast(CursorResult, dead).rowcount or 0)
                    + (cast(CursorResult, pend).rowcount or 0))

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

    async def _guarded[T](self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Run a queue write to completion even when the caller is cancelled mid-write: a
        cancel landing inside a database call abandons its connection unclosed, to be
        finalized later by the garbage collector (on Windows, after the loop that owned it
        is gone). The write runs to the end, the cancel is raised after, and a failed write
        is retrieved so it is not logged as unhandled (D-QUEUE-4)."""
        write = asyncio.ensure_future(_with_lock_retry(op, fn))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            if not write.done():
                await asyncio.wait([write])
            if not write.cancelled():
                write.exception()  # retrieved, so a failed write is not logged as unhandled
            raise

    async def heartbeat(self, task_id: int, worker_id: str, *, lease_seconds: int = 300) -> bool:
        """Renew a lease. Every renewal loop is cancelled when its task ends, often mid-write,
        so the write is shielded: it runs to the end even when the caller is cancelled, and
        the cancel is raised after (D-QUEUE-4)."""
        return await self._guarded(
            "heartbeat",
            lambda: self._heartbeat(task_id, worker_id, lease_seconds=lease_seconds),
        )

    async def complete(self, task_id: int, worker_id: str | None = None) -> bool:
        return await self._guarded("complete", lambda: self._complete(task_id, worker_id))

    async def fail(
        self, task_id: int, error: str, *, worker_id: str | None = None, retry: bool = True
    ) -> str:
        return await self._guarded(
            "fail", lambda: self._fail(task_id, error, worker_id=worker_id, retry=retry)
        )

    async def defer(
        self, task_id: int, note: str, *, worker_id: str | None = None, reason: str | None = None
    ) -> str:
        return await self._guarded(
            "defer", lambda: self._defer(task_id, note, worker_id=worker_id, reason=reason)
        )

    async def give_back(self, task_id: int, payload: dict, *, worker_id: str | None = None) -> str:
        return await self._guarded(
            "give_back", lambda: self._give_back(task_id, payload, worker_id=worker_id)
        )

    async def reclaim_expired(self) -> int:
        return await _with_lock_retry("reclaim_expired", self._reclaim_expired)

    async def release_all_leased(self, *, keep_worker_ids: tuple[str, ...] = ()) -> int:
        return await self._guarded(
            "release_all_leased", lambda: self._release_all_leased(tuple(keep_worker_ids))
        )

    async def get(self, task_id: int) -> Task | None:
        async with self._sm() as s:
            return await s.get(Task, task_id)
