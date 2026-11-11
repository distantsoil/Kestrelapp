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


TIER = "tier2"  # prompt story_consolidate/v2 runs on this tier
# How long one pass of a build may run before it gives the worker back (D-STORY-2). A pass
# always writes at least one group, then stops between groups once this has passed.
PASS_SECONDS = 120.0
_SKIP_KEY = "failed_sketch_ids"  # merges that failed earlier in this build: not sent again
_TALLY_KEY = "progress"  # counts so far across the build's passes, for the last-build line
# Everything a story build puts in its prompt (D-STORY-3): the notes and sketches, and the proof
# points from the profile. The resume text stays on this computer (it only checks names).
BUILD_SENDS_ALL = (
    "your interview notes, the interview sketches saved with each scored role, and the proof "
    "points in your profile (each claim with its evidence and metric)"
)


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
        # Asked again while a build waits out a retry backoff: it runs now (D-QUEUE-7).
        from kestrel.queue.queue import run_now

        if await run_now(session, Task.type == TASK_TYPE):
            await session.commit()
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


async def _record_failed_ids(sm: async_sessionmaker[AsyncSession], task_id: int,
                             failed_ids: list[int] | None) -> None:
    """Record the ids of the groups a pass that raised had already failed, in the task's payload
    (D-STORY-2). The pass hands them back on the exception (stories.FAILED_IDS_ATTR) because its
    `result` is lost to the raise; writing them before `queue.fail` means the retry of this build
    reads the same skip list the give_back path would have written, so a failed group is never
    resent. Merged with any skip list an earlier pass of this build already left behind."""
    if not failed_ids:
        return
    async with sm() as s:
        task = await s.get(Task, task_id)
        if task is None:
            return
        payload = dict(task.payload or {})
        skip = {int(i) for i in payload.get(_SKIP_KEY) or []}
        skip |= {int(i) for i in failed_ids}
        payload[_SKIP_KEY] = sorted(skip)
        task.payload = payload
        task.updated_at = utcnow_iso()
        await s.commit()


async def drain_story_builds(
    sm: async_sessionmaker[AsyncSession], client: Any, settings: Settings, *, stop: Any = None
) -> int:
    """Run every queued build, one at a time. A failure goes back to the queue while attempts
    remain (the page shows it as building) and is recorded once the task is dead (the page
    shows the reason and Try again); it never raises into the caller's worker cycle."""
    from kestrel.interview.stories import FAILED_IDS_ATTR, consolidate_stories
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
            await _notify_owed(sm)  # drafts earlier passes saved are still announced
            continue
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        payload = dict(task.payload or {})
        skip = frozenset(int(i) for i in payload.get(_SKIP_KEY) or [])
        try:
            async with sm() as s:
                # Passes only when Tier 2 is online: a local model shares the GPU with scoring,
                # and giving the worker back between groups would swap models every pass (the
                # drain-one-tier rule). A local build still saves each group as it finishes.
                res = await consolidate_stories(
                    s, client, settings, skip_ids=skip,
                    pass_seconds=PASS_SECONDS if _use_passes(settings) else None,
                )
        except asyncio.CancelledError as exc:
            # Quit cut the pass off (D-STORY-4). The groups this pass already failed are kept in
            # the payload before the cancel goes on, exactly as for a pass that raised, so the
            # build that resumes at the next start does not send them again. The lease itself is
            # handed back by the quit (attempt refunded). Shielded: the write finishes even if a
            # second cancel lands.
            beat.cancel()
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    _record_failed_ids(sm, task.id, getattr(exc, FAILED_IDS_ATTR, None)))
            raise
        except Exception as exc:  # a failed build is reported on the page, never fatal
            beat.cancel()
            log.warning("build_stories_failed", task=task.id,
                        error=f"{type(exc).__name__}: {exc}")
            # A pass that died may have already failed a group before the raise (D-STORY-2): it
            # hands those ids back on the exception (stories.FAILED_IDS_ATTR). Record them in the
            # task's payload before the task is failed, so the retry of this build skips them
            # exactly as the give_back path records them for a pass that stopped on purpose.
            # Best-effort: this is the moment a DB write just failed (a commit can be the one that
            # raised), so a lock here is plausible; if the recording itself fails we fall back to
            # the pre-fix behaviour (the retry may resend) rather than strand the task or break the
            # worker cycle, the same as the suppressed _record below.
            with contextlib.suppress(Exception):
                await _record_failed_ids(sm, task.id, getattr(exc, FAILED_IDS_ATTR, None))
            status = await queue.fail(task.id, failure_record(exc), worker_id=wid, retry=True)
            if status == "dead":
                with contextlib.suppress(Exception):
                    await _record(sm, {"at": utcnow_iso(), "ok": False,
                                       "error": task_failure_text(exc)[:300]})
                with contextlib.suppress(Exception):
                    await _notify_owed(sm)  # drafts earlier passes saved are still announced
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        tally = dict(payload.get(_TALLY_KEY) or {})
        first = not tally
        for key, n in (("created", len(res.created_story_ids)), ("attached", len(res.attached))):
            tally[key] = int(tally.get(key, 0)) + n
        # The first pass saw every sketch the build covers, and every blank or earlier-refused one
        # it skips; later passes see only what is left, so they add only what they refused.
        tally.setdefault("sketches", res.sketches)
        tally["skipped"] = res.skipped if first else int(tally.get("skipped", 0)) + res.refused
        if res.more:
            # Stopped between groups at the pass deadline (D-STORY-2): what it wrote is saved,
            # and the task goes back to the queue so the worker runs searches, letters and the
            # rest before the next pass carries on. Not a failure: the attempt is refunded.
            await queue.give_back(task.id, {
                **payload, _SKIP_KEY: sorted(skip | set(res.failed_ids)), _TALLY_KEY: tally,
            }, worker_id=wid)
            log.info("build_stories_paused", task=task.id, created=tally["created"],
                     attached=tally["attached"])
            break
        await _record(sm, {
            "at": utcnow_iso(), "ok": True, "sketches": tally["sketches"],
            "created": tally["created"], "attached": tally["attached"],
            "skipped": tally["skipped"],
        })
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done


def _use_passes(settings: Settings) -> bool:
    """Whether a build runs in passes that give the worker back (D-STORY-2): only when Tier 2 is
    online, so a local model is never swapped out between groups."""
    from kestrel.services.online_consent import tier_goes_online

    return isinstance(settings, Settings) and tier_goes_online(settings, TIER)


async def _notify_owed(sm: async_sessionmaker[AsyncSession]) -> None:
    """Send the drafts notice a build that ends without a last pass (held or dead) still owes
    for the drafts its earlier passes saved (D-STORY-2)."""
    from kestrel.interview.stories import _notify_unsent

    async with sm() as s:
        await _notify_unsent(s)
        await s.commit()


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
