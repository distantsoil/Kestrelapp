"""Building stories as a queued task (D-UI-52, hard rule 5). The My stories page only ENQUEUES a
`build_stories` task; the serve worker runs `consolidate_stories` over the operator's interview
notes and the Block F sketches in each scored role, off the request thread. The outcome is kept in
one setting row so the page can say what happened, including "nothing new to build from".

Resumable: a build is retried (MAX_ATTEMPTS) and its lease is renewed while it runs, so an app
killed mid-build picks the build up again after the lease expires instead of dropping it.
Re-running is safe: consolidate_stories commits once at the end (a killed build left nothing
behind) and never rewrites a canonical story, an operator-saved one least of all; a re-run only
attaches to it. The page reads the task row itself (build_state), so "building" and
"failed: <reason>" are true even when the process died before it could write a word."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting, Task
from kestrel.errors import failure_record, shown_failure, task_failure_text

log = structlog.get_logger()

TASK_TYPE = "build_stories"
LAST_BUILD_KEY = "stories.last_build"
_LIVE = ("pending", "leased")
MAX_ATTEMPTS = 3
# The lease covers a slow stretch of model calls; a heartbeat renews it every _RENEW_S while the
# build runs, so a long build never looks crashed, and a killed one is reclaimed within LEASE_S.
LEASE_S = 600
_RENEW_S = 120
# After "database is locked" a renewal is tried again this soon, doubling up to _RENEW_S.
_RENEW_LOCK_BACKOFF_S = 5.0
_STOPPED = "it stopped before it finished (Kestrel was closed or crashed)"


async def build_pending(session: AsyncSession) -> bool:
    """Whether a build is queued or running."""
    n = (
        await session.execute(
            select(func.count()).select_from(Task).where(
                Task.type == TASK_TYPE, Task.status.in_(_LIVE)
            )
        )
    ).scalar_one()
    return bool(n)


TIER = "tier2"  # prompt story_consolidate/v1 runs on this tier
BUILD_SENDS = "your interview notes and the interview sketches saved with each scored role"


async def request_build(session: AsyncSession, *, consent: dict | None = None) -> bool:
    """Queue one build unless one is already waiting or running. Commits. True when queued.
    `consent` is the operator's confirmation of an online send
    (kestrel.services.online_consent.consent_payload). With Tier 2 online and no confirmation for
    the model configured now, nothing is queued: a build never goes online without a click."""
    from kestrel.services.online_consent import held_for_consent
    from kestrel.services.setting_overrides import load_settings_with_db

    if held_for_consent(await load_settings_with_db(session), TIER, consent):
        return False
    if await build_pending(session):
        return False
    session.add(Task(type=TASK_TYPE, payload=dict(consent or {}), max_attempts=MAX_ATTEMPTS))
    await session.commit()
    return True


async def build_state(session: AsyncSession) -> dict:
    """What the page says about the newest build, from its task row: {"state": "building"} while
    one is queued or running, {"state": "failed", "reason": ...} when the newest ended dead (its
    last error, or that it stopped before it finished), else {"state": None} and the page reads
    last_build for how the newest finished build went."""
    task = (
        await session.execute(
            select(Task).where(Task.type == TASK_TYPE).order_by(Task.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if task is None:
        return {"state": None}
    if task.status in _LIVE:
        return {"state": "building", "attempt": task.attempts or 0}
    if task.status == "dead":
        return {"state": "failed", "reason": (shown_failure(task.last_error) or _STOPPED)[:300]}
    return {"state": None}


async def last_build(session: AsyncSession) -> dict | None:
    row = await session.get(Setting, LAST_BUILD_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None


async def _record(sm: async_sessionmaker[AsyncSession], value: dict) -> None:
    async with sm() as s:
        row = await s.get(Setting, LAST_BUILD_KEY)
        if row is None:
            s.add(Setting(key=LAST_BUILD_KEY, value=value))
        else:
            row.value = value
            row.updated_at = utcnow_iso()
        await s.commit()


async def drain_story_builds(
    sm: async_sessionmaker[AsyncSession], client: Any, settings: Settings, *, stop: Any = None
) -> int:
    """Run every queued build, one at a time. A failure goes back to the queue while attempts
    remain (the page shows it as building) and is recorded once the task is dead (the page
    shows the reason and Try again); it never raises into the caller's worker cycle."""
    from kestrel.interview.stories import consolidate_stories
    from kestrel.queue.queue import TaskQueue
    from kestrel.services.online_consent import held_at_run_time

    queue = TaskQueue(sm)
    wid = "pipeline-stories"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        if await held_at_run_time(sm, settings, TIER, task.payload):
            # Tier 2 went online after this was queued: it needs a fresh click, never a send.
            await _record(sm, {
                "at": utcnow_iso(), "ok": False,
                "error": "held because Tier 2 now uses an online model. Press Build stories "
                         "and confirm the send.",
            })
            await queue.complete(task.id, worker_id=wid)
            continue
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            async with sm() as s:
                res = await consolidate_stories(s, client, settings)
        except Exception as exc:  # a failed build is reported on the page, never fatal
            beat.cancel()
            log.warning("build_stories_failed", task=task.id,
                        error=f"{type(exc).__name__}: {exc}")
            status = await queue.fail(task.id, failure_record(exc), worker_id=wid, retry=True)
            if status == "dead":
                with contextlib.suppress(Exception):
                    await _record(sm, {"at": utcnow_iso(), "ok": False,
                                       "error": task_failure_text(exc)[:300]})
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        await _record(sm, {
            "at": utcnow_iso(), "ok": True, "sketches": res.sketches,
            "created": len(res.created_story_ids), "attached": len(res.attached),
            "skipped": res.skipped,
        })
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done


async def _renew(queue: Any, task_id: int, worker_id: str, *, every_s: float = _RENEW_S) -> None:
    """Keep the build's lease alive while it runs, for the life of the build (D-UI-56).

    Renewal is best effort: SQLite answering "database is locked" (the queue's own retries
    exhausted while another lane writes) backs off and tries again, a little later each time but
    always well inside the lease, and any other error waits for the next beat. A missed renewal
    never aborts the build. Only a lease actually lost (heartbeat False: reclaimed, or another
    worker took it) stops renewing. Cancelled by drain_story_builds when the build ends."""
    from sqlalchemy.exc import OperationalError

    from kestrel.queue.queue import _is_lock_error

    delay = every_s
    backoff = _RENEW_LOCK_BACKOFF_S
    while True:
        await asyncio.sleep(delay)
        try:
            alive = await queue.heartbeat(task_id, worker_id, lease_seconds=LEASE_S)
        except OperationalError as exc:
            if not _is_lock_error(exc):
                log.warning("build_stories_renew_error", task=task_id, error=str(exc))
                delay = every_s
                continue
            log.info("build_stories_renew_locked", task=task_id, retry_in_s=backoff)
            delay = backoff
            backoff = min(backoff * 2, every_s)
            continue
        except Exception as exc:  # noqa: BLE001 - a missed renewal must not kill the build
            log.warning("build_stories_renew_error", task=task_id, error=str(exc))
            delay = every_s
            continue
        if not alive:
            log.warning("build_stories_lease_lost", task=task_id)
            return
        delay, backoff = every_s, _RENEW_LOCK_BACKOFF_S
