"""Remove, on the Needs another try page (D-UI-104): take a failed role off that list only.

One setting row per role, `retry_removed.<opportunity id>`, holding when the operator pressed
Remove. It is state, not configuration (setting_overrides keeps the prefix out of the config
layer), like `tier0_override.<id>`. Nothing is deleted: the quarantine rows, their raw replies
and the role itself stay exactly as they were (hard rules 3 and 4), and Undo deletes the
marker. A failure newer than the marker brings the role back to the list, so a role that fails
again is never hidden by an old Remove (kestrel.web.queries._retry_removed)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

PREFIX = "retry_removed."


def key_for(opportunity_id: int) -> str:
    return f"{PREFIX}{int(opportunity_id)}"


async def mark(session: AsyncSession, opportunity_id: int) -> str:
    """Record (or refresh) the operator's Remove in the caller's session; the caller commits."""
    value = {"at": utcnow_iso()}
    row = await session.get(Setting, key_for(opportunity_id))
    if row is None:
        session.add(Setting(key=key_for(opportunity_id), value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()

    return value["at"]


async def clear(session: AsyncSession, opportunity_id: int, *, at: str | None = None) -> bool:
    """Undo a Remove. True when there was one; the caller commits."""
    row = await session.get(Setting, key_for(opportunity_id))
    if row is None or (at is not None and row.value.get("at") != at):
        return False
    await session.delete(row)
    return True


async def removed_ids(session: AsyncSession) -> set[int]:
    """Every role the operator removed from the list (markers only, not the newer-failure rule)."""
    keys = (
        await session.execute(select(Setting.key).where(Setting.key.like(f"{PREFIX}%")))
    ).scalars().all()
    return {int(k[len(PREFIX):]) for k in keys if k[len(PREFIX):].isdigit()}
