"""The cover letter / engagement outline draft as a queued task (D-UI-239 addition, hard rule 5).

A draft is a `task` row of type `letter_draft`, never an in-memory asyncio task: quitting
mid-draft leaves the row, and the next start leases it again. Only the serve worker drains this
type (after the tiers, never while paused); a request queues the task and answers with the
drafting card, which asks the status route until the draft is saved. An online draft
carries the model and destination the person confirmed on its payload
(`online_consent.consent_payload`); `check` re-reads the settings in effect when the task runs and
a task whose confirmation no longer matches is HELD (`APPROVAL_HOLD`, left pending and never
leased again) instead of sending. The person re-confirms in the ask-first panel, which replaces
the hold with the new confirmation.

INVARIANT: nothing is sent to an online model without a ticked confirmation that matches the
model and destination configured at the moment of the send, on the first run, on a retry, on a
join and on resume after a restart.

This module is the queue side only. What a draft is (prompt, validation, the saved row) lives with
the Role page (`kestrel.web.pages.role`), which hands `run` and `check` in."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import JSON, and_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Task
from kestrel.errors import shown_failure
from kestrel.queue.queue import TaskQueue
from kestrel.services.online_consent import APPROVAL_HOLD, CONSENT_PAYLOAD_KEYS

log = structlog.get_logger()

TASK_TYPE = "letter_draft"
TIER = "tier3"
LIVE = ("pending", "leased")
MAX_ATTEMPTS = 3
STOPPED = "Drafting stopped before it finished. Try again."
# An online draft that may already have been sent is never run again by itself (a second run
# would send the same material twice). It is marked failed with this sentence on the Documents
# row, and the person drafts again through the ask-first panel.
STOPPED_ONLINE = "The draft stopped partway. Draft again to try once more."
SENDING_KEY = "send_started"
_OPP = func.json_extract(Task.payload, "$.opportunity_id")


def is_online(task: Task | None) -> bool:
    """A draft the person confirmed for an online model (its payload carries the confirmation)."""
    from kestrel.services.online_consent import CONSENT_KEY

    return bool(task is not None and (task.payload or {}).get(CONSENT_KEY))


def is_held(task: Task | None) -> bool:
    return bool(task is not None and task.status == "pending" and task.last_error == APPROVAL_HOLD)


async def live_task(s: AsyncSession, opp_id: int) -> Task | None:
    """The role's draft that is queued, running or held, or None. There is at most one."""
    return (await s.execute(
        select(Task).where(Task.type == TASK_TYPE, Task.status.in_(LIVE), _OPP == opp_id)
        .order_by(Task.id.desc()).limit(1)
    )).scalars().first()


async def latest_task(s: AsyncSession, opp_id: int) -> Task | None:
    return (await s.execute(
        select(Task).where(Task.type == TASK_TYPE, _OPP == opp_id)
        .order_by(Task.id.desc()).limit(1)
    )).scalars().first()


def summary(task: Task | None) -> dict[str, Any]:
    """What the page says about the newest draft task: {"state": "drafting" | "held" | "failed" |
    None, "reason": the sentence to show, "quarantined": bool, "at": when it last changed}."""
    if task is None:
        return {"state": None}
    payload = task.payload or {}
    if is_held(task):
        return {"state": "held", "at": task.updated_at}
    if task.status in LIVE:
        return {"state": "drafting", "at": task.updated_at}
    if task.status == "dead":
        return {"state": "failed", "at": task.updated_at, "quarantined": False,
                "reason": shown_failure(task.last_error) or STOPPED}
    if payload.get("error"):
        return {"state": "failed", "at": task.updated_at, "reason": str(payload["error"]),
                "quarantined": bool(payload.get("quarantined"))}
    return {"state": None}


async def enqueue_or_join(
    s: AsyncSession, opp_id: int, *, kind: str, consent: dict[str, Any], replace_id: int | None,
) -> tuple[Task, bool]:
    """Queue the role's draft unless one is already live. Returns (task, created). One statement
    inserts only when no live row exists, so two requests at once cannot both queue. Commits."""
    payload: dict[str, Any] = {
        "opportunity_id": opp_id, "requested": True, "kind": kind, "replace_id": replace_id,
        **consent,
    }
    now = utcnow_iso()
    live = and_(Task.type == TASK_TYPE, Task.status.in_(LIVE), _OPP == opp_id)
    res = await s.execute(insert(Task).from_select(
        ["type", "payload", "status", "priority", "attempts", "max_attempts", "created_at",
         "updated_at"],
        select(
            literal(TASK_TYPE), literal(payload, type_=JSON), literal("pending"), literal(0),
            literal(0), literal(MAX_ATTEMPTS), literal(now), literal(now),
        ).where(~select(Task.id).where(live).exists()),
    ))
    created = res.rowcount == 1
    await s.commit()
    task = await live_task(s, opp_id)
    if task is None:  # it finished between the insert and the read; the newest row is the answer
        task = await latest_task(s, opp_id)
    assert task is not None
    return task, created


async def run_now(s: AsyncSession, task: Task) -> None:
    """The person asked again for the draft already waiting (same confirmation): if it waits out
    a retry backoff it runs at the next pass (D-QUEUE-7). Nothing is sent from here. Commits."""
    from kestrel.queue.queue import run_now as clear_wait

    if await clear_wait(s, Task.id == task.id):
        await s.commit()
        await s.refresh(task)


async def reconfirm(s: AsyncSession, task: Task, consent: dict[str, Any]) -> bool:
    """Replace a held task's confirmation with the person's new one and let it run. Only an
    unstarted hold is touched (never a lease). True when it was released. Commits."""
    payload = {k: v for k, v in (task.payload or {}).items()
               if k not in (*CONSENT_PAYLOAD_KEYS, "deferred")}
    payload.update(consent)
    res = await s.execute(
        update(Task).where(
            Task.id == task.id, Task.status == "pending", Task.last_error == APPROVAL_HOLD,
        ).values(payload=payload, last_error=None, updated_at=utcnow_iso())
        .execution_options(synchronize_session=False)
    )
    await s.commit()
    return res.rowcount == 1


async def release_holds(
    sm: async_sessionmaker[AsyncSession], check: Callable[[Task], Awaitable[str | None]],
) -> int:
    """Let held drafts run again when the settings now match what the person confirmed (the model
    was switched back, or it is local now). Nothing is sent from here: the run re-checks."""
    async with sm() as s:
        held = (await s.execute(select(Task).where(
            Task.type == TASK_TYPE, Task.status == "pending", Task.last_error == APPROVAL_HOLD,
        ))).scalars().all()
        freed = 0
        for task in held:
            if await check(task) is None:
                res = await s.execute(update(Task).where(
                    Task.id == task.id, Task.status == "pending",
                    Task.last_error == APPROVAL_HOLD,
                ).values(last_error=None,
                         payload=func.json_remove(Task.payload, "$.deferred"),
                         updated_at=utcnow_iso()))
                freed += res.rowcount or 0
        await s.commit()
    return freed


async def drain(
    sm: async_sessionmaker[AsyncSession],
    run: Callable[[Task], Awaitable[dict[str, Any]]],
    check: Callable[[Task], Awaitable[str | None]],
    *, stop: Any = None, task_id: int | None = None, worker_id: str = "pipeline-letters",
    paused: Callable[[], bool] | None = None,
) -> int:
    """Run queued drafts one at a time (all of them, or just `task_id`). `check` returns a reason
    when the task may not send now: it is held and left alone. `run` returns the outcome
    ({"ok": True, "render_id"} or {"ok": False, "reason", "quarantined"}); a refused or failed
    draft is finished, not retried (a retry could send again). A crash of a draft on this computer
    is retried while attempts remain, but not again in this pass (the next worker cycle takes it
    up), so a failing draft never spins. A crash of an online draft is not retried at all, and an
    online draft found leased again with its send marked started (a quit or crash mid-send) is
    failed the same way: it may already have been sent, so it is never run again by itself.
    `paused` stops the pass before the next lease; the lease is renewed while a draft runs."""
    from kestrel.pipeline.worker import _TASK_LEASE_S, _renew_lease

    queue = TaskQueue(sm)
    await release_holds(sm, check)
    done = 0
    crashed: list[int] = []  # failed this pass: the next pass retries a local one
    while (stop is None or not stop.is_set()) and not (paused is not None and paused()):
        where = func.coalesce(Task.last_error, "") != APPROVAL_HOLD
        if task_id is not None:
            where = and_(where, Task.id == task_id)
        if crashed:
            where = and_(where, Task.id.not_in(crashed))
        task = await queue.lease(
            worker_id, types=[TASK_TYPE], lease_seconds=_TASK_LEASE_S, where=where
        )
        if task is None:
            break
        if is_online(task) and (task.payload or {}).get(SENDING_KEY):
            await queue.fail(task.id, STOPPED_ONLINE, worker_id=worker_id, retry=False)
            log.warning("letter_draft_not_resent", task_id=task.id)
            continue
        if await check(task) is not None:
            await queue.defer(task.id, APPROVAL_HOLD, worker_id=worker_id)
            log.info("letter_draft_held", task_id=task.id)
            continue
        if is_online(task) and not await _mark_sending(sm, task.id, worker_id, True):
            # The lease is no longer ours (worker or status changed): fail closed. The row's
            # owner decides what happens to it; this pass sends nothing.
            log.warning("letter_draft_lease_lost", task_id=task.id)
            continue
        beat = asyncio.create_task(_renew_lease(queue, task.id, worker_id))
        try:
            outcome = await run(task)
        except Exception as exc:  # noqa: BLE001 - a bad draft never takes the worker down
            log.warning("letter_draft_task_failed", task_id=task.id, error_type=type(exc).__name__)
            if is_online(task):  # the send may have happened: no automatic second one
                await queue.fail(task.id, STOPPED_ONLINE, worker_id=worker_id, retry=False)
            else:
                await queue.fail(task.id, STOPPED, worker_id=worker_id, retry=True)
                crashed.append(task.id)
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        if outcome.get("held"):  # the settings moved between the check and the send: no send
            if is_online(task):
                await _mark_sending(sm, task.id, worker_id, False)  # nothing went out
            await queue.defer(task.id, APPROVAL_HOLD, worker_id=worker_id)
            log.info("letter_draft_held", task_id=task.id)
            continue
        result = ({"render_id": outcome.get("render_id")} if outcome.get("ok") else {
            "error": str(outcome.get("reason") or "No draft was made."),
            "quarantined": bool(outcome.get("quarantined")),
        })
        async with sm() as s:
            res = await s.execute(update(Task).where(
                Task.id == task.id, Task.status == "leased", Task.worker_id == worker_id,
            ).values(payload={**_without(task.payload, SENDING_KEY), **result}, status="done",
                     last_error=None, updated_at=utcnow_iso()))
            await s.commit()
        done += res.rowcount or 0
    return done


def _without(payload: dict[str, Any] | None, key: str) -> dict[str, Any]:
    return {k: v for k, v in (payload or {}).items() if k != key}


async def _mark_sending(
    sm: async_sessionmaker[AsyncSession], task_id: int, worker_id: str, on: bool
) -> bool:
    """Record on the task row, before an online draft can send, that it may have been sent. If
    the process dies mid-send the row says so, and the next start fails it instead of sending
    again. Cleared when the draft is held unsent. False when the lease is lost (another worker or
    status): nothing is written and the caller must not send."""
    async with sm() as s:
        task = await s.get(Task, task_id)
        if task is None or task.worker_id != worker_id or task.status != "leased":
            return False
        base = _without(task.payload, SENDING_KEY)
        task.payload = {**base, SENDING_KEY: True} if on else base
        await s.commit()
        return True
