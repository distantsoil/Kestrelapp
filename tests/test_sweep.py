"""pursuit_sweep raises notification rows for a due next-action and an overdue (ghosting) pursuit,
and skips terminal pursuits."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from kestrel.db.models import Notification, Opportunity, Pursuit
from kestrel.services.notifications import NotificationService
from kestrel.services.sweep import pursuit_sweep


async def _opp(session) -> int:
    o = Opportunity(track="employment", opp_type="job_posting", title="Role")
    session.add(o)
    await session.flush()
    return o.id


async def test_pursuit_sweep_notifies_due_and_overdue(sm):
    today = date.today()
    async with sm() as s:
        s.add(
            Pursuit(
                opportunity_id=await _opp(s),
                track="employment",
                status="applied",
                next_action_at=today.isoformat(),
                next_action="follow up with recruiter",
            )
        )
        s.add(
            Pursuit(
                opportunity_id=await _opp(s),
                track="employment",
                status="applied",
                last_activity_at=(today - timedelta(days=30)).isoformat(),
            )
        )
        # terminal pursuit: due next action but must be SKIPPED
        s.add(
            Pursuit(
                opportunity_id=await _opp(s),
                track="employment",
                status="rejected",
                next_action_at=today.isoformat(),
            )
        )
        await s.commit()

    counts = await pursuit_sweep(sm, NotificationService(sm), now=today, ghosting_days=21)
    assert counts["due"] == 1 and counts["overdue"] == 1  # terminal one not counted

    async with sm() as s:
        kinds = {n.kind for n in (await s.execute(select(Notification))).scalars().all()}
    assert "next_action_due" in kinds and "ghosting" in kinds
