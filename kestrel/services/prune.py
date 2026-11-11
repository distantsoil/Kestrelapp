"""Retention (D-PERF-3): old finished work leaves the database so a long-lived install stays fast.

What goes, and when (SPEC.md, Data retention):
- background tasks `done` or `cancelled` for 30 days (by `updated_at`);
- `dead` tasks for 30 days, except those of a role that is still parked in Scoring failed or still
  has an open failure on Needs another try: that history is what explains the failure;
- finished runs and scrape runs, and every notification, for 365 days.

What never goes: a task that is pending, leased or `failed`; the newest follow_up task of each
application (its payload holds the drafted text the Follow up dialog shows); the newest sorting
model training task of each track and every one that records a retrain of a refused model (they
are the only record of "50 more ratings" and "once per refused model ever", D-LEARN-10); a run
that has not finished; a role,
an evaluation, a pursuit or anything the operator wrote. A scrape run a role still points at is
let go by clearing that role's `scrape_run_id` first (the column is nullable and only the live
run summary reads it); the role stays. The run's raw payload file (`raw_path`) is deleted with
it, only when it resolves inside the data folder.

Hard rule 5. The work is done in short transactions of BATCH rows, each one complete in itself,
and the day's stamp (`housekeeping.last_prune`) is written only after the last batch. A kill at
any point leaves a consistent database, and the next pass picks up what is left: every statement
selects by age and condition, never from remembered state. The pass runs on the worker's loop when
a cycle is idle and no run is open, and yields between batches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import structlog
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel.db.base import iso, utcnow
from kestrel.db.models import (
    Notification,
    Opportunity,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Task,
)

log = structlog.get_logger()
_now = utcnow  # this module's own clock; tests patch it

TASK_KEEP_DAYS = 30
HISTORY_KEEP_DAYS = 365
BATCH = 500
STAMP_KEY = "housekeeping.last_prune"
# One pass a day at most; the loop asks no more than once an hour whether it is due.
INTERVAL = timedelta(days=1)


@dataclass
class PruneResult:
    tasks_done: int = 0
    tasks_dead: int = 0
    runs: int = 0
    scrape_runs: int = 0
    notifications: int = 0
    # Undo snapshots older than their day (a removed correction's note among them): swept here
    # too, so they go even when no new Undo is ever made (fable review, 0.7.6).
    undo_snapshots: int = 0
    stopped: bool = False
    detail: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.tasks_done + self.tasks_dead + self.runs + self.scrape_runs + self.notifications


def _still_needs_you():
    """A dead task of a role that is still parked (quarantined or errored) or still has an open
    failure on Needs another try (queries.open_quarantine_clause, the page's own predicate)."""
    from kestrel.web.queries import _PARKED_STAGES, open_quarantine_clause

    return and_(
        Task.opportunity_id.is_not(None),
        or_(
            Task.opportunity_id.in_(
                select(Opportunity.id).where(Opportunity.stage.in_(_PARKED_STAGES))
            ),
            Task.opportunity_id.in_(
                select(Quarantine.opportunity_id).where(
                    Quarantine.opportunity_id.is_not(None), open_quarantine_clause()
                )
            ),
        ),
    )


def _shown_follow_up():
    """The newest follow_up task of each application (D-PERF-5): the Follow up dialog reads only
    that one, and its payload is the only copy of the drafted text (and the record that stops the
    same paid request being sent twice). It stays whatever its age; older ones may go."""
    from kestrel.services.follow_up import TASK_TYPE

    return Task.id.in_(
        select(func.max(Task.id))
        .where(Task.type == TASK_TYPE)
        .group_by(func.json_extract(Task.payload, "$.pursuit_id"))
    )


def _training_markers():
    """The training tasks `learning.training.request_training` reads as its markers (D-LEARN-10):
    the newest of each track (its `ratings` is the "50 more" baseline when no model row exists)
    and every one carrying `replaces_refused` ("once per refused model row ever"). They stay
    whatever their age; there is at most one per track plus one per refused model."""
    from kestrel.learning.training import TASK_TYPE

    newest = select(func.max(Task.id)).where(Task.type == TASK_TYPE).group_by(
        func.json_extract(Task.payload, "$.track"))
    marked = select(Task.id).where(
        Task.type == TASK_TYPE,
        func.json_extract(Task.payload, "$.replaces_refused").is_not(None),
    )
    return or_(Task.id.in_(newest), Task.id.in_(marked))


async def _delete_in_batches(sm: async_sessionmaker, model, where, stop) -> int:
    """Delete rows matching `where`, BATCH at a time, one short transaction each."""
    gone = 0
    while stop is None or not stop.is_set():
        async with sm() as s:
            ids = list(
                (
                    await s.execute(select(model.id).where(where).order_by(model.id).limit(BATCH))
                ).scalars()
            )
            if not ids:
                return gone
            await s.execute(delete(model).where(model.id.in_(ids)))
            await s.commit()
        gone += len(ids)
        await asyncio.sleep(0)  # the loop serves the page between batches
    return gone


UNDO_KEEP = timedelta(days=1)
UNDO_PREFIXES = ("undo.correction.", "undo.pursuit.", "undo.applied.")


async def _prune_undo_snapshots(sm: async_sessionmaker, cutoff: str, stop) -> int:
    """Delete Undo snapshot rows (setting rows keyed by prefix) older than CUTOFF."""
    gone = 0
    where = and_(
        or_(*(Setting.key.like(p + "%") for p in UNDO_PREFIXES)), Setting.updated_at < cutoff
    )
    while stop is None or not stop.is_set():
        async with sm() as s:
            keys = list((await s.execute(select(Setting.key).where(where).limit(BATCH))).scalars())
            if not keys:
                return gone
            await s.execute(delete(Setting).where(Setting.key.in_(keys)))
            await s.commit()
        gone += len(keys)
        await asyncio.sleep(0)
    return gone


async def _prune_scrape_runs(sm: async_sessionmaker, cutoff: str, stop) -> int:
    gone = 0
    old = and_(ScrapeRun.finished_at.is_not(None), ScrapeRun.finished_at < cutoff)
    while stop is None or not stop.is_set():
        async with sm() as s:
            found = (
                await s.execute(
                    select(ScrapeRun.id, ScrapeRun.raw_path).where(old)
                    .order_by(ScrapeRun.id).limit(BATCH)
                )
            ).all()
            ids = [int(i) for i, _p in found]
            if not ids:
                return gone
            # The role stays; only its pointer to a run that is leaving is cleared, in the same
            # transaction as the delete (foreign keys are enforced).
            await s.execute(
                update(Opportunity)
                .where(Opportunity.scrape_run_id.in_(ids))
                .values(scrape_run_id=None)
                .execution_options(synchronize_session=False)
            )
            await s.execute(delete(ScrapeRun).where(ScrapeRun.id.in_(ids)))
            await s.commit()
        # The raw payload file goes after its row, and only when it is inside the data folder:
        # a path read from the database (a restored backup can write any) is never followed out.
        for _id, raw in found:
            _remove_raw(raw)
        gone += len(ids)
        await asyncio.sleep(0)
    return gone


def _remove_raw(stored: object) -> None:
    from kestrel import paths

    target = paths.confined_file(stored, paths.data_home())
    if target is None:
        return
    try:
        if target.is_file():
            target.unlink()
    except OSError as exc:  # the row is gone; a file left behind is only disk
        log.warning("prune_raw_file_failed", error=str(exc))


async def prune(sm: async_sessionmaker, *, stop: asyncio.Event | None = None) -> PruneResult:
    """One pass. Safe to run at any time, to stop at any time, and to run twice."""
    now = _now()
    tasks_cut = iso(now - timedelta(days=TASK_KEEP_DAYS))
    hist_cut = iso(now - timedelta(days=HISTORY_KEEP_DAYS))
    out = PruneResult()
    out.tasks_done = await _delete_in_batches(
        sm, Task,
        and_(Task.status.in_(("done", "cancelled")), Task.updated_at < tasks_cut,
             ~_shown_follow_up(), ~_training_markers()),
        stop,
    )
    out.tasks_dead = await _delete_in_batches(
        sm, Task,
        and_(Task.status == "dead", Task.updated_at < tasks_cut, ~_still_needs_you(),
             ~_shown_follow_up(), ~_training_markers()),
        stop,
    )
    out.notifications = await _delete_in_batches(
        sm, Notification, Notification.created_at < hist_cut, stop
    )
    # An open run has no finished_at: it is never in this set, whatever its age.
    out.runs = await _delete_in_batches(
        sm, Run, and_(Run.finished_at.is_not(None), Run.finished_at < hist_cut), stop
    )
    out.scrape_runs = await _prune_scrape_runs(sm, hist_cut, stop)
    out.undo_snapshots = await _prune_undo_snapshots(sm, iso(now - UNDO_KEEP), stop)
    out.stopped = bool(stop is not None and stop.is_set())
    return out


async def prune_if_due(
    sm: async_sessionmaker, *, stop: asyncio.Event | None = None
) -> PruneResult | None:
    """prune() when the last complete pass was a day or more ago; None when not due. The stamp is
    written only after a pass that ran to the end, so an interrupted one is retried."""
    now = _now()
    async with sm() as s:
        row = await s.get(Setting, STAMP_KEY)
        last = row.value if row is not None and isinstance(row.value, str) else None
    if last and last > iso(now - INTERVAL):
        return None
    out = await prune(sm, stop=stop)
    if out.stopped:
        return out
    async with sm() as s:
        row = await s.get(Setting, STAMP_KEY)
        if row is None:
            s.add(Setting(key=STAMP_KEY, value=iso(now)))
        else:
            row.value = iso(now)
        await s.commit()
    log.info(
        "prune_done", tasks=out.tasks_done + out.tasks_dead, runs=out.runs,
        scrape_runs=out.scrape_runs, notifications=out.notifications,
    )
    return out
