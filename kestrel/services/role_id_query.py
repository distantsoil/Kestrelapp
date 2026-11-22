"""Find a role by its ID (D-UI-770): one parser and one clause for the command palette and the
Roles list filter, so the two can never disagree about what an ID query means.

Three spellings, all exact, case-insensitive, with spaces trimmed and runs of spaces collapsed:
`:jobid-434`, `#434` and `role id 434` (the wording on the role page). Anything else is not an
ID query and the caller falls back to its ordinary title search, unchanged. Read only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity

_ID_QUERY = re.compile(r"^(?::jobid-|#|role id )([0-9]{1,9})$")


def parse_role_id(q: str | None) -> int | None:
    """The role ID a query names, or None when the query is not exactly an ID query."""
    if not q:
        return None
    m = _ID_QUERY.match(" ".join(q.split()).casefold())
    return int(m.group(1)) if m else None


def role_id_clause(role_id: int):
    """Rows to show for an ID query: the role itself when it is listed on its own (the main copy
    of its duplicate group, or a copy the operator shortlisted or saved), otherwise the main copy
    of its group (a duplicate opens its canonical copy). A hidden role, a role of a hidden company
    and a role that is not in the active list are all still found: the caller skips those
    exclusions for an ID query and labels the row instead."""
    from kestrel.web.queries import listed_clause

    own_listed = (
        select(Opportunity.id).where(Opportunity.id == role_id, listed_clause()).correlate(None)
    )
    group = (
        select(Opportunity.dedup_group_id).where(Opportunity.id == role_id).correlate(None)
        .scalar_subquery()
    )
    return or_(
        and_(Opportunity.id == role_id, listed_clause()),
        and_(
            Opportunity.is_canonical.is_(True),
            Opportunity.dedup_group_id.is_not(None),
            Opportunity.dedup_group_id == group,
            ~exists(own_listed),
        ),
        # A copy with no main copy to open is shown as itself rather than lost.
        and_(
            Opportunity.id == role_id,
            ~exists(own_listed),
            ~exists(
                select(Opportunity.id)
                .where(Opportunity.is_canonical.is_(True), Opportunity.dedup_group_id == group)
                .correlate(None)
            ),
        ),
    )


def role_id_found(role_id: int):
    """True when the ID query finds a role (an EXISTS over `role_id_clause`). This one test is
    the decision both surfaces share: an ID that finds a role shows that role, otherwise the
    query is searched as ordinary title text, exactly as it was before (D-UI-772). It looks at
    every track: a valid ID is never searched as text because of the tab the operator is on
    (D-UI-773)."""
    return exists(select(Opportunity.id).where(role_id_clause(role_id)).correlate(None))


@dataclass(frozen=True)
class RoleIdHit:
    """One role an ID query finds, with the words both surfaces print beside it."""

    opp_id: int
    track: str  # "employment" or "consulting"
    label: str  # "Hidden", "Hidden company" or ""
    note: str  # "Shown as role N, the main copy." for a duplicate, else ""


async def role_id_hits(session: AsyncSession, role_id: int) -> list[RoleIdHit]:
    """The roles an ID query finds, labeled; empty when the ID finds nothing. The palette and
    the Roles list both take their labels from here, so they can never disagree."""
    from kestrel.services.hidden_orgs import opp_visible_clause

    rows = (
        await session.execute(
            select(Opportunity.id, Opportunity.track, Opportunity.archived_at, opp_visible_clause())
            .where(role_id_clause(role_id))
            .order_by(Opportunity.id)
        )
    ).all()
    return [
        RoleIdHit(
            oid,
            track,
            "Hidden" if archived_at else ("Hidden company" if not visible else ""),
            f"Shown as role {oid}, the main copy." if oid != role_id else "",
        )
        for oid, track, archived_at, visible in rows
    ]
