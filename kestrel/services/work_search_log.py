"""The work search log (D-EXPORT-1): every application in a date range, whatever happened after.

A state work search log asks for the role, the employer and the date of each application. A role
that was later rejected, withdrawn, ghosted or whose posting closed still counts, so the current
status never filters a row; only "was it applied for, and on what date" does. The title and company
are the stored role's, never a live page. Local data only; nothing is sent (hard rule 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.enums import APPLIED_OR_LATER
from kestrel.db.models import Opportunity, Pursuit, StatusEvent
from kestrel.services.export_terms import export_copies
from kestrel.services.pursuit_board import CLOSED_STATUSES, parse_day

QUICK_PICKS = (("this_week", "This week"), ("last_week", "Last week"), ("this_month", "This month"))


@dataclass(frozen=True)
class LogRow:
    pursuit_id: int
    role: str
    company: str
    applied: str  # YYYY-MM-DD, or "" when the date is unknown


@dataclass
class WorkLog:
    start: date
    end: date
    rows: list[LogRow] = field(default_factory=list)  # applied in range, oldest first
    unknown: list[LogRow] = field(default_factory=list)  # applied or later, no applied date
    # Applications that belong in this log but whose posting's source forbids passing it on
    # (D-SRC-19): kept out of the rows, counted so the sheet can say so.
    withheld: int = 0


def pick_range(pick: str, today: date) -> tuple[date, date]:
    """A quick pick as (start, end), weeks Monday to Sunday. Anything else is this week."""
    monday = today - timedelta(days=today.weekday())
    if pick == "last_week":
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if pick == "this_month":
        return today.replace(day=1), today
    return monday, monday + timedelta(days=6)


async def collect(session: AsyncSession, start: date, end: date,
                  track: str = "employment") -> WorkLog:
    """Every pursuit of `track` in an applied-or-later status, split by its applied date.

    A pursuit with no applied date is an application when it is still open (it got past the
    shortlist) or its history shows it reached an open applied-or-later stage; one closed straight
    from the shortlist never was one and is left out."""
    closed = set(CLOSED_STATUSES.get(track, ()))
    open_stages = APPLIED_OR_LATER - closed
    found = (await session.execute(
        select(Pursuit, Opportunity)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .where(Pursuit.track == track, Pursuit.status.in_(APPLIED_OR_LATER))
        .order_by(Pursuit.applied_at, Pursuit.id)
    )).all()
    undated = [p.id for p, _o in found if parse_day(p.applied_at) is None]
    reached: set[int] = set()
    if undated:
        reached = set((await session.execute(
            select(StatusEvent.pursuit_id).where(
                StatusEvent.pursuit_id.in_(undated), StatusEvent.to_status.in_(open_stages))
        )).scalars())
    copies = await export_copies(session, [o.id for _p, o in found])  # D-SRC-19
    log = WorkLog(start=start, end=end)
    for p, o in found:
        copy = copies.get(o.id)
        day = parse_day(p.applied_at)
        if day is None:
            belongs = p.status not in closed or p.id in reached
        else:
            belongs = start <= day <= end
        if not belongs:
            continue
        if copy is None:  # the source forbids export: left out, and counted (D-EXPORT-3)
            log.withheld += 1
            continue
        row = LogRow(p.id, copy.title or "", copy.org_name or "", day.isoformat() if day else "")
        (log.unknown if day is None else log.rows).append(row)
    log.rows.sort(key=lambda r: (r.applied, r.pursuit_id))
    return log
