"""Read-only view of the person's applications for scripts (D-IMPORT-10, issue 147 M3): one row
per application with its Kestrel ID, role, company, source, applied date, status and last change,
so a script can bring an outside tracker up to date by ID. Reads only; nothing here writes except
creating the install's ID salt the first time an ID is handed out (D-IMPORT-9). Confidential mode
and a source's no-export terms apply exactly as they do to the opportunities API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Pursuit, Source, StatusEvent
from kestrel.services import kestrel_ids
from kestrel.services.applied_import import imported_board
from kestrel.services.export_terms import export_copies
from kestrel.web.redact import confidential, redact_name, redact_text

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class BadQuery(ValueError):
    """A parameter the caller sent cannot be read; the message says which and how."""


# The whole list of BadQuery messages. The API answers with one of these, picked by
# kestrel.web.errors.authored_error, never with the exception's own text (D-SEC-40).
SINCE_INVALID = "since must be an ISO date or time, such as 2026-10-01"
CURSOR_INVALID = "cursor is the next_cursor of an earlier page"
QUERY_REFUSALS = (SINCE_INVALID, CURSOR_INVALID)
QUERY_FALLBACK = "a query parameter could not be read"


def parse_when(raw: str | None, name: str) -> datetime | None:
    """An ISO date or timestamp (UTC when it has no offset), or None when absent."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadQuery(f"{name} must be an ISO date or time, such as 2026-10-01") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Page:
    rows: list[dict]
    next_cursor: str | None


def _cursor(raw: str | None) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    if not re.fullmatch(r"\d{1,12}", s):
        raise BadQuery(CURSOR_INVALID)
    return int(s)


async def list_applications(
    session: AsyncSession,
    *,
    track: str | None = None,
    status: str | None = None,
    since: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page:
    """Applications in id order, `limit` at a time. `since` keeps those whose last change is at
    or after it. `next_cursor` is None on the last page."""
    since_dt = parse_when(since, "since")
    after = _cursor(cursor)
    limit = max(1, min(int(limit), MAX_LIMIT))
    last_event = (
        select(StatusEvent.pursuit_id.label("pid"), func.max(StatusEvent.occurred_at).label("at"))
        .group_by(StatusEvent.pursuit_id)
        .subquery()
    )
    stmt = (
        select(Pursuit, Opportunity, Source.name, last_event.c.at)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .outerjoin(Source, Source.id == Opportunity.source_id)
        .outerjoin(last_event, last_event.c.pid == Pursuit.id)
        .where(Pursuit.id > after)
        .order_by(Pursuit.id)
    )
    if track:
        stmt = stmt.where(Pursuit.track == track)
    if status:
        stmt = stmt.where(Pursuit.status == status)
    found = (await session.execute(stmt)).all()
    entries = []
    for p, o, source_name, event_at in found:
        change = max(
            (t for t in (_ts(p.last_activity_at), _ts(p.applied_at), _ts(event_at)) if t),
            default=None,
        )
        if since_dt is not None and (change is None or change < since_dt):
            continue
        entries.append((p, o, source_name, change))
    # A source whose terms forbid passing a posting on is read from the same role's copy on
    # another board, or left out when there is none (D-SRC-19), as in the applications export.
    copies = await export_copies(session, [o.id for _p, o, _s, _c in entries])
    entries = [(p, c, s, ch) for p, o, s, ch in entries if (c := copies.get(o.id)) is not None]
    page, more = entries[:limit], len(entries) > limit
    conf, mapping = await confidential(session)
    ids = await kestrel_ids.ids_by_pursuit(session, create=True)
    await session.commit()  # only the install's ID salt, the first time
    rows = [
        {
            "id": ids.get(p.id, ""),
            "track": p.track,
            "role": redact_text(o.title, mapping, enabled=conf),
            "company": redact_name(o.org_name, mapping, enabled=conf),
            # a company board is named after the company, an import records the person's own words
            "source": redact_text(
                imported_board(o.description_md) or source_name, mapping, enabled=conf),
            "applied_at": (p.applied_at or "")[:10] or None,
            "status": p.status,
            "last_change": change.isoformat(timespec="seconds") if change else None,
        }
        for p, o, source_name, change in page
    ]
    return Page(rows, str(page[-1][0].id) if more and page else None)
