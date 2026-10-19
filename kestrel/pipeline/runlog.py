"""Run rows for pipeline work (SPEC.md 4.7 `run`, SPEC-INTERFACES 2.1 /runs). Shared by the CLI's
`Worker.run()` and the desktop app's worker loop, so Today, the Runs page and the status rail read
one record whichever path did the work.

The desktop loop opens a `CycleRun` per cycle and writes the row only when the cycle has work: an
idle 5 s cycle never leaves an empty row behind. A row the app opened and never closed (the process
was killed mid-cycle) is closed as `interrupted` at the next start (hard rule 5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Run, ScrapeRun, Task
from kestrel.queue.queue import claimable

if TYPE_CHECKING:
    from kestrel.pipeline.worker import RunStats, TierStats

log = structlog.get_logger()

# The name the desktop app's loop records its runs under. The CLI records "pipeline". Only rows
# with this name are closed as interrupted at app start: a CLI run in another process may be live.
APP_RUN_NAME = "desktop"
INTERRUPTED = "interrupted"
_WORK_TYPES = ("scrape", "embed", "t1", "t2", "t0_recheck")


def tier_counts(stats: TierStats) -> dict[str, Any]:
    return {
        "completed": stats.completed,
        "errors": stats.errors,
        "per_minute": round(stats.per_minute, 2),
    }


def stats_counts(stats: RunStats) -> dict[str, Any]:
    """The Run.counts shape Today and the Runs page read: tier0 sweep counts, then per tier."""
    return {
        "tier0": stats.tier0,
        "embed": tier_counts(stats.embed),
        "tier1": tier_counts(stats.tier1),
        "tier2": tier_counts(stats.tier2),
    }


async def open_run(
    sm: async_sessionmaker[AsyncSession],
    *,
    name: str,
    mode: str | None,
    track: str | None = None,
    started_at: str | None = None,
) -> int:
    async with sm() as s:
        run = Run(
            name=name,
            track=track,
            status="running",
            started_at=started_at or utcnow_iso(),
            mode=mode,
            counts={},
        )
        s.add(run)
        await s.commit()
        return run.id


async def write_counts(
    sm: async_sessionmaker[AsyncSession], run_id: int, counts: dict[str, Any]
) -> None:
    """Replace the live counts of a run still running (the Runs page shows them as they land)."""
    async with sm() as s:
        await s.execute(
            update(Run).where(Run.id == run_id, Run.status == "running").values(counts=counts)
        )
        await s.commit()


async def finish_run(
    sm: async_sessionmaker[AsyncSession],
    run_id: int,
    *,
    status: str,
    counts: dict[str, Any] | None = None,
    notes: str | None = None,
) -> None:
    """Close a run that is still `running`. A row already closed (by a restart sweep, say) is
    left as it is."""
    async with sm() as s:
        run = await s.get(Run, run_id)
        if run is None or run.status != "running":
            return
        run.status = status
        run.finished_at = utcnow_iso()
        if counts is not None:
            run.counts = counts
        if notes:
            run.notes = notes[:500]
        await s.commit()


async def close_orphaned_runs(
    sm: async_sessionmaker[AsyncSession], *, name: str = APP_RUN_NAME
) -> int:
    """Close every `running` row this app owns as `interrupted`. Called once at worker setup,
    before any cycle opens a row, so whatever is still running belonged to a dead process."""
    async with sm() as s:
        result = await s.execute(
            update(Run)
            .where(Run.name == name, Run.status == "running")
            .values(
                status=INTERRUPTED,
                finished_at=utcnow_iso(),
                notes="Closed at startup: the app stopped before this run finished.",
            )
        )
        await s.commit()
    n = result.rowcount or 0
    if n:
        log.info("runs_marked_interrupted", count=n)
    return n


async def has_pipeline_work(sm: async_sessionmaker[AsyncSession]) -> bool:
    """Whether a cycle starting now has anything to do: a scrape, embed or tier task this loop
    could actually lease (pending, or an expired lease with attempts left; not a live lease held
    by a concurrent CLI run, nor an expired one out of attempts), or a new posting waiting for
    Tier 0."""
    async with sm() as s:
        tasks = (
            await s.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.type.in_(_WORK_TYPES), claimable(utcnow_iso()))
            )
        ).scalar_one()
        if tasks:
            return True
        new = (
            await s.execute(
                select(func.count())
                .select_from(Opportunity)
                .where(Opportunity.stage == "new", Opportunity.archived_at.is_(None))
            )
        ).scalar_one()
    return new > 0


async def discard_run(sm: async_sessionmaker[AsyncSession], run_id: int) -> None:
    """Delete a row this loop opened that turned out to have nothing to do, so the Runs page
    never fills with empty `done` rows. Only a row still `running` is removed."""
    async with sm() as s:
        await s.execute(delete(Run).where(Run.id == run_id, Run.status == "running"))
        await s.commit()


async def new_postings_since(sm: async_sessionmaker[AsyncSession], since: str) -> int:
    """New postings the scrape runs started at or after `since` found."""
    async with sm() as s:
        rows = (
            await s.execute(select(ScrapeRun.counts).where(ScrapeRun.started_at >= since))
        ).scalars().all()
    return sum(int((c or {}).get("new", 0) or 0) for c in rows)


class CycleRun:
    """One desktop worker cycle's Run row, opened lazily: `ensure_open()` before work starts when
    the queue says there is some, and `record()` after each stage opens it if that stage did work
    (an embed sweep can find work the pre-check could not see). Counts are written after every
    stage so the Runs page shows a live run, not an empty one."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], *, mode: str | None) -> None:
        self._sm = sm
        self.mode = mode
        self.started_at = utcnow_iso()
        self.run_id: int | None = None
        self.counts: dict[str, Any] = {}
        self.did_work = False

    async def ensure_open(self) -> int:
        if self.run_id is None:
            self.run_id = await open_run(
                self._sm, name=APP_RUN_NAME, mode=self.mode, started_at=self.started_at
            )
        return self.run_id

    async def record(self, key: str, value: dict[str, Any], *, did_work: bool) -> None:
        self.counts[key] = value
        if did_work:
            self.did_work = True
            await self.ensure_open()
        if self.run_id is not None:
            await write_counts(self._sm, self.run_id, dict(self.counts))

    async def record_scrapes(self, stats: TierStats) -> None:
        """The scan half of the cycle. `finished_at`, `new` and `errors` are what the
        scan-finished toast and Today's idle line read. A scan in which every search crashed
        (completed 0, errors N) is still a finished scan, recorded with its errors."""
        if stats.completed == 0 and stats.errors == 0:
            return
        value = {
            "completed": stats.completed,
            "errors": stats.errors,
            "new": await new_postings_since(self._sm, self.started_at),
            "finished_at": utcnow_iso(),
        }
        await self.record("scrape", value, did_work=True)

    async def finish(self, status: str, *, notes: str | None = None) -> None:
        if self.run_id is None:
            return
        if not self.did_work and status in ("done", "stopping"):
            # Opened on the queue's say-so, then nothing was claimable after all: no row.
            await discard_run(self._sm, self.run_id)
            self.run_id = None
            return
        scrape = self.counts.get("scrape") or {}
        errors = int(scrape.get("errors", 0) or 0)
        if errors and status == "done":
            n = f"{errors} search{'es' if errors != 1 else ''}"
            if not int(scrape.get("completed", 0) or 0):
                status = "failed"  # every search crashed: not a clean run
            notes = notes or f"Search done; {n} had a problem. See Sources."
        await finish_run(self._sm, self.run_id, status=status, counts=dict(self.counts),
                         notes=notes)
