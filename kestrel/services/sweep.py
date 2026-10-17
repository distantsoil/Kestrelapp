"""pursuit_sweep (SPEC.md 4.6): runs daily from the scheduler and is what makes next-action and
ghosting notifications possible. It finds pursuits with a next_action_at due today (or earlier) and
pursuits whose last_activity_at has passed the ghosting threshold, writing notification rows."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.models import Pursuit
from kestrel.services.notifications import NotificationService

# Terminal statuses across both tracks; a closed pursuit is never swept.
_TERMINAL = {
    "accepted",
    "rejected",
    "withdrawn",
    "ghosted",  # employment
    "won",
    "lost",
    "no_response",  # consulting
}


def _as_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def pursuit_sweep(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: NotificationService,
    *,
    now: str | date | None = None,
    ghosting_days: int = 21,
) -> dict:
    today = _as_date(now) or date.today()
    counts = {"due": 0, "overdue": 0}
    async with sessionmaker() as session:
        pursuits = (await session.execute(select(Pursuit))).scalars().all()
    for p in pursuits:
        if p.status in _TERMINAL:
            continue
        due = _as_date(p.next_action_at)
        if due is not None and due <= today:
            await notifier.notify(
                "next_action_due",
                f"Next action due: {p.next_action or 'review pursuit'}",
                severity="info",
                pursuit_id=p.id,
            )
            counts["due"] += 1
        last = _as_date(p.last_activity_at)
        if last is not None and (today - last).days >= ghosting_days:
            await notifier.notify(
                "ghosting",
                f"No response for {ghosting_days}+ days on pursuit {p.id}",
                severity="warning",
                pursuit_id=p.id,
            )
            counts["overdue"] += 1
    return counts
