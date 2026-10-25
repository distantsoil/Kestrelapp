"""Save for later (D-UI-96): a role bookmarked without a decision, retrievable under Pipeline's
Saved chip. It is a per-role flag, not a pursuit: it never creates a shortlist row, a label or a
task.

One setting row per role, `saved_role.<opportunity id>`, holding when it was saved. SPEC.md names
`opportunity.saved_at` for this; the column has no migration yet, so the flag lives here, behind
this one module and `queries.saved_clause`, and moving it to the column later changes only those
two places. State, not configuration (setting_overrides keeps the prefix out of the config
layer). Nothing is deleted when a role is hidden or scored."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

PREFIX = "saved_role."


def key_for(opportunity_id: int) -> str:
    return f"{PREFIX}{int(opportunity_id)}"


async def save(session: AsyncSession, opportunity_id: int) -> bool:
    """Save the role for later in the caller's session; the caller commits. False when it was
    already saved."""
    row = await session.get(Setting, key_for(opportunity_id))
    if row is not None:
        return False
    session.add(Setting(key=key_for(opportunity_id), value={"at": utcnow_iso()}))
    return True


async def unsave(session: AsyncSession, opportunity_id: int) -> bool:
    """Undo Save for later. True when there was one; the caller commits."""
    row = await session.get(Setting, key_for(opportunity_id))
    if row is None:
        return False
    await session.delete(row)
    return True


async def saved_ids(session: AsyncSession) -> set[int]:
    keys = (
        await session.execute(select(Setting.key).where(Setting.key.like(f"{PREFIX}%")))
    ).scalars().all()
    return {int(k[len(PREFIX):]) for k in keys if k[len(PREFIX):].isdigit()}
