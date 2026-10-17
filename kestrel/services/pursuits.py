"""Pursuit status transitions. The ONLY supported way to change a pursuit's status: it validates
the target against the track's status set and writes an append-only status_event in the same
transaction. A status change without a status_event is a bug (SPEC.md 4.5)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import valid_pursuit_status
from kestrel.db.models import Pursuit, StatusEvent


class InvalidStatusError(ValueError):
    pass


async def set_pursuit_status(
    session: AsyncSession,
    pursuit_id: int,
    to_status: str,
    *,
    note: str | None = None,
    occurred_at: str | None = None,
) -> StatusEvent:
    """Transition a pursuit and record the change. Flushes but does not commit; the caller owns the
    transaction. Raises InvalidStatusError if to_status is not valid for the pursuit's track."""
    pursuit = await session.get(Pursuit, pursuit_id)
    if pursuit is None:
        raise InvalidStatusError(f"pursuit {pursuit_id} not found")
    if not valid_pursuit_status(pursuit.track, to_status):
        raise InvalidStatusError(f"'{to_status}' is not a valid {pursuit.track} status")

    from_status = pursuit.status
    pursuit.status = to_status
    pursuit.last_activity_at = utcnow_iso()
    event = StatusEvent(
        pursuit_id=pursuit.id,
        from_status=from_status,
        to_status=to_status,
        note=note,
        occurred_at=occurred_at or utcnow_iso(),
    )
    session.add(event)
    await session.flush()
    return event
