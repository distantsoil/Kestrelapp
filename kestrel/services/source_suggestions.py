"""Suggested companies for the Sources page (D-UI-102): companies the operator has a pursuit at,
on the track being viewed, that no source covers yet. Nothing is added until the operator presses
Add; Dismiss is remembered per track and Undo takes it back. Local data only: no request leaves
this computer to build the list (a lookup happens only when Add is pressed)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Pursuit, Setting
from kestrel.ingest.orgs import normalized_name

KEY = "sources.dismissed_suggestions"
BOARD_CACHE_KEY = "sources.suggestion_boards"
MAX_SHOWN = 6
_CLOSED = {"rejected", "withdrawn", "ghosted", "declined", "closed", "lost"}


async def _dismissed(session: AsyncSession) -> dict[str, list[str]]:
    row = await session.get(Setting, KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {str(k): [str(x) for x in v] for k, v in value.items() if isinstance(v, list)}


async def _save(session: AsyncSession, value: dict[str, list[str]]) -> None:
    row = await session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


# --------------------------------------------------------------------------- board cache
async def board_cache(session: AsyncSession) -> dict:
    """{normalized name: {"found": bool, ...}}. A found entry carries the source spec; a not-found
    entry is {"found": False}. Kept so a repeated Add does not repeat the lookups (#123, finding 6)
    and the row can show the outcome up front (finding 7)."""
    row = await session.get(Setting, BOARD_CACHE_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return value


async def record_board_lookup(
    session: AsyncSession, name: str, found: bool, *, platform: str | None = None,
    jobs: int = 0, kind: str | None = None, config: dict | None = None,
    company: str | None = None,
) -> None:
    norm = normalized_name(name)
    if not norm:
        return
    value = await board_cache(session)
    entry: dict = {"found": found}
    if found:
        entry.update(platform=platform, jobs=int(jobs or 0), kind=kind, config=config or {},
                     company=company or name)
    value[norm] = entry
    row = await session.get(Setting, BOARD_CACHE_KEY)
    if row is None:
        session.add(Setting(key=BOARD_CACHE_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def dismiss(session: AsyncSession, track: str, name: str) -> bool:
    value = await _dismissed(session)
    norm = normalized_name(name)
    if not norm or norm in value.setdefault(track, []):
        return False
    value[track].append(norm)
    await _save(session, value)
    return True


async def undismiss(session: AsyncSession, track: str, name: str) -> None:
    value = await _dismissed(session)
    norm = normalized_name(name)
    value[track] = [x for x in value.get(track, []) if x != norm]
    await _save(session, value)


def _covered(norm: str, covered: list[str]) -> bool:
    return any(norm and (norm in c or c in norm) for c in covered if c)


async def suggestions(
    session: AsyncSession, track: str, source_names: list[str]
) -> list[dict]:
    """[{"name", "reason"}], at most MAX_SHOWN, newest pursuit first. A company already covered
    by a source (its name appears in a source's name) or dismissed is left out."""
    covered = [normalized_name(n) for n in source_names]
    gone = set((await _dismissed(session)).get(track, []))
    rows = (
        await session.execute(
            select(Org.name, Pursuit.status)
            .join(Opportunity, Opportunity.org_id == Org.id)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.track == track)
            .order_by(Pursuit.id.desc())
        )
    ).all()
    out: list[dict] = []
    seen: set[str] = set()
    for name, status in rows:
        norm = normalized_name(name)
        if (not norm or norm in seen or norm in gone or str(status) in _CLOSED
                or _covered(norm, covered)):
            continue
        seen.add(norm)
        what = "shortlisted a role" if status == "shortlisted" else "an application"
        out.append({
            "name": name,
            "reason": (f"You {what} here" if status == "shortlisted"
                       else f"You have {what} here"),
        })
        if len(out) >= MAX_SHOWN:
            break
    return out


async def suggestions_with_boards(
    session: AsyncSession, track: str, source_names: list[str]
) -> list[dict]:
    """suggestions() with the remembered board lookup on each row (None, a found board, or a
    not-found note), so the row says up front whether Add will work or the company must be added
    by hand (#123). Consulting rows carry no board (they watch filings, not a board)."""
    out = await suggestions(session, track, source_names)
    if track == "consulting":
        for sg in out:
            sg["board"] = None  # explicit: they watch filings, not a board
        return out
    boards = await board_cache(session)
    for sg in out:
        sg["board"] = boards.get(normalized_name(sg["name"]))
    return out
