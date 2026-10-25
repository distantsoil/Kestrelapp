"""Hidden companies (D-UI-123 amended): Hide this company on the Company page, and a Hidden
companies list in Settings with Unhide per company.

Hiding sets `org.hidden_at`. A hidden company's roles are left out of Pipeline (and Today); nothing
is deleted, scoring still runs, and Unhide puts everything back. The caller commits."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Pursuit

# Pursuit statuses that end a pursuit, across both tracks (mirrors analytics.today._TERMINAL).
_ENDED = ("accepted", "rejected", "withdrawn", "ghosted", "won", "lost", "no_response")


@dataclass(frozen=True)
class HiddenOrg:
    org_id: int
    name: str
    hidden_at: str
    role_count: int  # roles of this company kept out of the lists


def _active_pursuit():
    """EXISTS: the (outer) opportunity has a pursuit the operator is still working. A role they
    applied to, or are interviewing for, never disappears because its company was hidden."""
    pa = aliased(Pursuit)
    return exists().where(
        pa.opportunity_id == Opportunity.id,
        pa.closed_at.is_(None),
        pa.status.not_in(_ENDED),
    )


def not_hidden_org_clause():
    """SQL condition: the role's company is not hidden, or the role has an active pursuit. A role
    with no company row passes (an outer-joined `org` is NULL). Use on a statement that already
    joins `Org` to `Opportunity`."""
    return or_(Org.hidden_at.is_(None), _active_pursuit())


def _hidden_org_ids():
    return select(Org.id).where(Org.hidden_at.is_not(None))


def opp_visible_clause():
    """The same condition on `Opportunity` alone (no `Org` join needed). Use it on any count or
    list that reads opportunities directly, so a hidden company never shows or counts on Today."""
    return or_(
        Opportunity.org_id.is_(None),
        Opportunity.org_id.not_in(_hidden_org_ids()),
        _active_pursuit(),
    )


def hidden_opportunity_ids():
    """A SELECT of the ids of every role kept out of the lists by a hidden company."""
    return select(Opportunity.id).where(~opp_visible_clause())


async def any_hidden(session: AsyncSession) -> bool:
    return bool(
        (await session.execute(select(func.count()).select_from(Org).where(
            Org.hidden_at.is_not(None)))).scalar_one()
    )


async def hide_org(session: AsyncSession, org_id: int) -> bool:
    """Hide a company. False when it does not exist. Hiding twice keeps the first timestamp."""
    org = await session.get(Org, org_id)
    if org is None:
        return False
    if org.hidden_at is None:
        org.hidden_at = utcnow_iso()
        await session.flush()
    return True


async def unhide_org(session: AsyncSession, org_id: int) -> bool:
    """Put a company back. False when it does not exist."""
    org = await session.get(Org, org_id)
    if org is None:
        return False
    if org.hidden_at is not None:
        org.hidden_at = None
        await session.flush()
    return True


async def is_hidden(session: AsyncSession, org_id: int | None) -> bool:
    if org_id is None:
        return False
    org = await session.get(Org, org_id)
    return org is not None and org.hidden_at is not None


async def list_hidden(session: AsyncSession) -> list[HiddenOrg]:
    """Hidden companies for Settings > Which roles you see, most recently hidden first."""
    counts = (
        select(Opportunity.org_id, func.count().label("n"))
        # Only roles the hide really keeps out: one with a pursuit still being worked stays
        # visible (opp_visible_clause), so it is not counted as kept out.
        .where(Opportunity.archived_at.is_(None), ~_active_pursuit())
        .group_by(Opportunity.org_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Org.id, Org.name, Org.hidden_at, counts.c.n)
            .join(counts, counts.c.org_id == Org.id, isouter=True)
            .where(Org.hidden_at.is_not(None))
            .order_by(Org.hidden_at.desc(), Org.id.desc())
        )
    ).all()
    return [HiddenOrg(int(i), n, h, int(c or 0)) for i, n, h, c in rows]
