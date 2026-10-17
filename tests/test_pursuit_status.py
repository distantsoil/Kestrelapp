"""A pursuit status never changes without an append-only status_event (SPEC.md 4.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from kestrel.db.models import Opportunity, Pursuit, StatusEvent
from kestrel.services.pursuits import InvalidStatusError, set_pursuit_status


async def _make_pursuit(sm, *, track="employment", status="shortlisted") -> int:
    async with sm() as s:
        opp = Opportunity(track=track, opp_type="job_posting", title="VP IT")
        s.add(opp)
        await s.flush()
        pursuit = Pursuit(opportunity_id=opp.id, track=track, status=status)
        s.add(pursuit)
        await s.commit()
        return pursuit.id


async def test_status_change_writes_a_status_event(sm):
    pid = await _make_pursuit(sm)
    async with sm() as s:
        await set_pursuit_status(s, pid, "applied", note="submitted")
        await s.commit()
    async with sm() as s:
        pursuit = await s.get(Pursuit, pid)
        events = (
            (await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid)))
            .scalars()
            .all()
        )
    assert pursuit.status == "applied"
    assert pursuit.last_activity_at is not None
    assert len(events) == 1
    assert events[0].from_status == "shortlisted"
    assert events[0].to_status == "applied"
    assert events[0].note == "submitted"


async def test_every_change_appends_one_event(sm):
    pid = await _make_pursuit(sm)
    async with sm() as s:
        for status in ("preparing", "applied", "screening"):
            await set_pursuit_status(s, pid, status)
        await s.commit()
    async with sm() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(StatusEvent).where(StatusEvent.pursuit_id == pid)
            )
        ).scalar()
    assert count == 3


async def test_invalid_status_for_track_is_rejected(sm):
    pid = await _make_pursuit(sm, track="employment")
    async with sm() as s:
        with pytest.raises(InvalidStatusError):
            await set_pursuit_status(s, pid, "won")  # consulting-only status
        # and no stray event was written
        count = (
            await s.execute(
                select(func.count()).select_from(StatusEvent).where(StatusEvent.pursuit_id == pid)
            )
        ).scalar()
    assert count == 0
