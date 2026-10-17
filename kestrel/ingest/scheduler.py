"""Recurring-scan scheduling (SPEC.md 6.5). Every five minutes the worker enqueues `scrape` tasks
for due, enabled sources, with guards: never enqueue for a source that already has a pending or
leased scrape, and (kill switch) do nothing unless settings.scheduler.enabled is true (default
false, so a fresh install never scrapes unattended). The AsyncIOScheduler wiring lives in the app
lifespan (Phase 13/14); the pure functions here are what the pipeline and tests use."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import iso, utcnow
from kestrel.db.models import Setting, Source, Task
from kestrel.queue import TaskQueue
from kestrel.queue.queue import claimable


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None

# Cancel scan (D-UI-35) holds the AUTOMATIC scheduler (cadence and run schedule) for this long,
# so the due searches the operator just cancelled are not queued straight back on the next tick.
# Stored as a setting row (state, not configuration: the Scheduler config model ignores the key),
# so it survives a restart. Any explicit scan by the operator clears it.
SCAN_HOLD_KEY = "scheduler.scan_hold_until"
SCAN_HOLD = timedelta(hours=1)


async def _set_hold(session: AsyncSession, until: str | None) -> None:
    row = await session.get(Setting, SCAN_HOLD_KEY)
    if until is None:
        if row is not None:
            await session.delete(row)
        return
    if row is None:
        session.add(Setting(key=SCAN_HOLD_KEY, value=until))
    else:
        row.value = until


async def scan_hold_until(session: AsyncSession, *, now: datetime | None = None) -> datetime | None:
    """When the cancel hold ends, or None when no hold is in force."""
    row = await session.get(Setting, SCAN_HOLD_KEY)
    until = _parse(row.value if row is not None and isinstance(row.value, str) else None)
    if until is None or until <= (now or utcnow()):
        return None
    return until


async def clear_scan_hold(session: AsyncSession) -> None:
    """The operator asked for a scan: the cancel hold no longer applies. Does not commit."""
    await _set_hold(session, None)


async def due_sources(session: AsyncSession, *, now: datetime | None = None) -> list[Source]:
    from kestrel.services.tracks import load_enabled

    now_dt = now or utcnow()
    tracks = await load_enabled(session)
    # A track the operator turned off never scrapes, whatever its rows say (review finding 4):
    # its sources are hidden from Sources, so they could not be switched off by hand.
    enabled = (
        (
            await session.execute(
                select(Source).where(Source.enabled.is_(True), Source.track.in_(tracks))
            )
        ).scalars().all()
    )
    due = []
    for s in enabled:
        last = _parse(s.last_run_at)
        if last is None or (now_dt - last) >= timedelta(hours=s.cadence_hours):
            due.append(s)
    return due


async def _has_pending_scrape(session: AsyncSession, source_id: int) -> bool:
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.type == "scrape",
            Task.status.in_(("pending", "leased")),
            Task.payload["source_id"].as_integer() == source_id,
        )
    )
    return ((await session.execute(stmt)).scalar() or 0) > 0


async def enqueue_due_scrapes(
    session: AsyncSession, queue: TaskQueue, *, now: datetime | None = None, operator: bool = False
) -> int:
    """Enqueue a `scrape` task per due, enabled source that has no pending/leased scrape already.
    Returns the number enqueued. Enqueues nothing while a Cancel scan hold is in force, unless
    `operator` (an explicit scan request: the tray, onboarding), which clears the hold."""
    if operator:
        await clear_scan_hold(session)
        await session.commit()
    elif await scan_hold_until(session, now=now) is not None:
        return 0
    enqueued = 0
    for source in await due_sources(session, now=now):
        if await _has_pending_scrape(session, source.id):
            continue
        await queue.enqueue("scrape", {"source_id": source.id})
        enqueued += 1
    return enqueued


async def enqueue_all_scrapes(
    session: AsyncSession,
    queue: TaskQueue,
    *,
    model_override: dict | None = None,
    model_label: str | None = None,
) -> int:
    """Enqueue a scrape for every enabled source on an enabled track, due or not: the operator
    pressed Scan now. Sources with a pending or leased scrape are skipped, as everywhere else.
    Clears any Cancel scan hold. `model_override` is the one-scan model choice (D-UI-72): it rides
    on each scrape task's payload (so a restart keeps it) to the runner, which records it on the
    scrape_run. Signal searches (SEC, SAM.gov and the like) never carry it: their roles do not
    pass through the posting path, so they run on the usual models.
    """
    from kestrel.ingest.signal_sources import SIGNAL_SOURCE_KINDS
    from kestrel.services.tracks import load_enabled

    await clear_scan_hold(session)
    await session.commit()
    tracks = await load_enabled(session)
    sources = (
        await session.execute(
            select(Source).where(Source.enabled.is_(True), Source.track.in_(tracks))
        )
    ).scalars().all()
    enqueued = 0
    for src in sources:
        if await _has_pending_scrape(session, src.id):
            continue
        payload: dict = {"source_id": src.id}
        if model_override and src.kind not in SIGNAL_SOURCE_KINDS:
            payload["model_override"] = dict(model_override)
            payload["model_label"] = model_label or str(model_override.get("model") or "")
        await queue.enqueue("scrape", payload)
        enqueued += 1
    return enqueued


async def cancel_pending_scrapes(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Cancel the scan in progress: every claimable scrape task (pending, or leased with an
    expired lease and attempts left, which a worker would pick up next), including one held by
    a switched-off track, becomes `cancelled`. A
    scrape under a LIVE lease is left to finish its current source; killing it mid-write would
    leave a half-recorded scrape run. No source's last_run_at is touched (a cancelled search did
    not run, and Sources must not say it did); instead a short scan hold stops the scheduler
    queuing the due ones straight back (SCAN_HOLD). Returns the number of rows actually
    cancelled. Commits."""
    now_dt = now or utcnow()
    now_s = iso(now_dt)
    res = await session.execute(
        update(Task)
        # A held scrape (switched-off track, D-UI-68) is cancelled too (review finding 1).
        .where(Task.type == "scrape", claimable(now_s, include_held=True))
        .values(
            status="cancelled", last_error="cancelled by the operator", worker_id=None,
            lease_until=None, updated_at=now_s,
        )
    )
    cancelled = int(res.rowcount or 0)
    if cancelled:
        await _set_hold(session, iso(now_dt + SCAN_HOLD))
    await session.commit()
    return cancelled
