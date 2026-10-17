"""The operator's own notes for an interview (D-UI-52), one text per opportunity.

Stored in the `setting` table under `prep_notes.<opportunity_id>`, so they survive every rebuild of
the prep pack (a rebuild writes a new pack row) and exist whether or not the role has a pursuit.
The story builder reads them next to the interview sketches in each scored role; they are the
operator's words and are never rewritten."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

NOTES_PREFIX = "prep_notes."
NOTES_MAX = 20000


def notes_key(opportunity_id: int) -> str:
    return f"{NOTES_PREFIX}{opportunity_id}"


async def get_notes(session: AsyncSession, opportunity_id: int) -> str:
    row = await session.get(Setting, notes_key(opportunity_id))
    return row.value if row is not None and isinstance(row.value, str) else ""


async def save_notes(session: AsyncSession, opportunity_id: int, text: str) -> str:
    """Save the text exactly as typed (trailing whitespace trimmed, length capped). Empty text
    removes the row. Commits. Returns what was stored."""
    text = (text or "").strip()[:NOTES_MAX]
    row = await session.get(Setting, notes_key(opportunity_id))
    if not text:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(Setting(key=notes_key(opportunity_id), value=text))
    else:
        row.value = text
        row.updated_at = utcnow_iso()
    await session.commit()
    return text


async def all_notes(session: AsyncSession) -> list[tuple[int, str, str]]:
    """(opportunity_id, text, saved_at) for every non-empty note, oldest first."""
    rows = (
        await session.execute(
            select(Setting)
            .where(Setting.key.like(f"{NOTES_PREFIX}%"))
            .order_by(Setting.updated_at, Setting.key)
        )
    ).scalars().all()
    out: list[tuple[int, str, str]] = []
    for row in rows:
        suffix = row.key[len(NOTES_PREFIX):]
        if suffix.isdigit() and isinstance(row.value, str) and row.value.strip():
            out.append((int(suffix), row.value, row.updated_at))
    return out
