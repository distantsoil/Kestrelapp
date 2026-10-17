"""The run schedule (SPEC.md 6.5 "Run schedule"): one daily or weekly time, in the operator's
local clock, at which the scheduler enqueues a scrape for every enabled source, cadence or not.
Stored as two setting rows; fires only while Kestrel is running; fires once per local day or
ISO week, so a restart inside the period does not repeat it and a time missed while the app was
closed fires at the next start within the same period, never for an earlier one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting, Source
from kestrel.queue.queue import TaskQueue

SCHEDULE_KEY = "scheduler.run_schedule"
LAST_KEY = "scheduler.last_scheduled_run"
# When the schedule last fired, as a UTC ISO instant (D-UI-60): Today's Schedule card reads it and
# the run that started at or after it, for "Last scheduled run: Thu 6:00 AM, finished".
LAST_AT_KEY = "scheduler.last_scheduled_at"
KINDS = ("off", "daily", "weekly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class RunSchedule:
    kind: str = "off"
    time: str = "07:00"  # HH:MM, local
    weekday: int = 0  # Monday = 0, used by weekly

    @property
    def hour_minute(self) -> tuple[int, int]:
        h, m = self.time.split(":")
        return int(h), int(m)

    def describe(self) -> str:
        if self.kind == "daily":
            return f"daily at {self.time}"
        if self.kind == "weekly":
            return f"weekly on {WEEKDAYS[self.weekday]} at {self.time}"
        return "off"


def parse(value: object) -> RunSchedule:
    """A stored row (or form) to a schedule; anything malformed reads as off, never raises."""
    if not isinstance(value, dict):
        return RunSchedule()
    kind = str(value.get("kind") or "off")
    time_s = str(value.get("time") or "07:00")
    m = _TIME.match(time_s)
    if kind not in KINDS or not m:
        return RunSchedule()
    try:
        weekday = int(value.get("weekday", 0))
    except (TypeError, ValueError):
        weekday = 0
    if not 0 <= weekday <= 6:
        weekday = 0
    return RunSchedule(kind=kind, time=f"{int(m.group(1)):02d}:{m.group(2)}", weekday=weekday)


def local_now(now: datetime | None = None) -> datetime:
    """The operator's clock. An explicit `now` is taken as already being on that clock (tests
    pass a fixed offset); only the default reads the system zone."""
    return now if now is not None else datetime.now().astimezone()


def period_key(sched: RunSchedule, now_local: datetime) -> str:
    if sched.kind == "weekly":
        year, week, _ = now_local.isocalendar()
        return f"{year}-W{week:02d}"
    return now_local.date().isoformat()


def is_due(sched: RunSchedule, now_local: datetime, last: str | None) -> bool:
    """True when the scheduled moment of the current period has passed and this period has not
    fired yet."""
    if sched.kind == "off":
        return False
    if last == period_key(sched, now_local):
        return False
    h, m = sched.hour_minute
    at = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if sched.kind == "daily":
        return now_local >= at
    # weekly: the scheduled weekday of THIS ISO week, at the time
    monday = now_local - timedelta(days=now_local.weekday())
    at = monday.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=sched.weekday)
    return now_local >= at


def next_fire(sched: RunSchedule, now_local: datetime, last: str | None) -> datetime | None:
    """When the schedule fires next, for the page. None when off."""
    if sched.kind == "off":
        return None
    h, m = sched.hour_minute
    if sched.kind == "daily":
        at = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if last == period_key(sched, now_local) or at <= now_local:
            at += timedelta(days=1)
        return at
    monday = now_local - timedelta(days=now_local.weekday())
    at = monday.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=sched.weekday)
    if last == period_key(sched, now_local) or at <= now_local:
        at += timedelta(days=7)
    return at


async def load(session: AsyncSession) -> tuple[RunSchedule, str | None]:
    rows = (
        await session.execute(
            select(Setting.key, Setting.value).where(Setting.key.in_((SCHEDULE_KEY, LAST_KEY)))
        )
    ).all()
    data = dict(rows)
    last = data.get(LAST_KEY)
    return parse(data.get(SCHEDULE_KEY)), (str(last) if isinstance(last, str) else None)


async def _set(session: AsyncSession, key: str, value) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def save(session: AsyncSession, sched: RunSchedule) -> None:
    """Write the schedule row. Nothing else: the schedule is honored by the scheduler loop on
    its own (SPEC.md 6.5), independent of any source cadence."""
    await _set(
        session,
        SCHEDULE_KEY,
        {"kind": sched.kind, "time": sched.time, "weekday": sched.weekday},
    )


async def fire_if_due(
    session: AsyncSession, queue: TaskQueue, *, now: datetime | None = None
) -> int | None:
    """Enqueue a scrape for every enabled source when the schedule is due; None when it is not.
    Records the period so it fires once. Sources with a pending or leased scrape are skipped,
    as the cadence scheduler does. Deferred (None) while a Cancel scan hold is in force."""
    from kestrel.ingest.scheduler import _has_pending_scrape, scan_hold_until

    sched, last = await load(session)
    now_local = local_now(now)
    if not is_due(sched, now_local, last):
        return None
    # A Cancel scan hold defers the scheduled run without recording the period, so it fires
    # once the hold lapses, still inside the same day or week.
    if await scan_hold_until(session, now=now) is not None:
        return None
    from kestrel.services.tracks import load_enabled

    tracks = await load_enabled(session)
    sources = (
        (
            await session.execute(
                select(Source).where(Source.enabled.is_(True), Source.track.in_(tracks))
            )
        ).scalars().all()
    )
    enqueued = 0
    for src in sources:
        if await _has_pending_scrape(session, src.id):
            continue
        await queue.enqueue("scrape", {"source_id": src.id})
        enqueued += 1
    await _set(session, LAST_KEY, period_key(sched, now_local))
    await _set(session, LAST_AT_KEY, utcnow_iso())
    await session.commit()
    return enqueued


async def last_fired(session: AsyncSession) -> dict | None:
    """The schedule's latest firing and how its run went: {"at", "outcome"}, where outcome is
    "queued" (no run has started since), "running", or how the run ended in one word. None
    when it never fired (or fired before this was recorded)."""
    from kestrel.analytics.today import run_ended_word
    from kestrel.db.models import Run

    row = await session.get(Setting, LAST_AT_KEY)
    at = row.value if row is not None and isinstance(row.value, str) else None
    if not at:
        return None
    run = (
        await session.execute(
            select(Run).where(Run.started_at >= at).order_by(Run.started_at, Run.id).limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        outcome = "queued"
    elif run.status == "running":
        outcome = "running"
    else:
        outcome = run_ended_word(run.status)
    return {"at": at, "outcome": outcome}
