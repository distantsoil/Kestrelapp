"""Ghost-job aggregate signals and presentation (SPEC-MODULES 6.2). The per-opportunity ghost score
is computed at ingestion (kestrel.ingest.ghost.compute_ghost_score); this module adds the signals
that need CROSS-opportunity context the normalizer cannot see in one snapshot:

  - an organization running more than 20 simultaneous postings with generic descriptions, and
  - a requisition id reappearing live after a prior posting with that id was marked closed,

folds them (plus the republished-identical signal) back into the score via recompute_ghost, and
provides the badge the UI shows. ghost_score already reaches Block G of the evaluation prompt
(kestrel.pipeline.context build_tier2_values), so once recompute_ghost runs on a cadence the model
reasons with the accumulated cross-opportunity history rather than one snapshot. A high ghost score
is only ever a badge; it never rejects an opportunity (uncertainty escalates, it never rejects)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity
from kestrel.ingest.ghost import compute_ghost_score
from kestrel.ingest.normalize import mentions_team

_LIVE = ("live", "unknown", "unchecked")  # anything not explicitly closed still counts as posted


def _is_generic(opp: Opportunity) -> bool:
    """No compensation, no location, and no team detail: the moderate ghost signal, reused to decide
    whether an org's simultaneous postings are the generic pipeline-building kind."""
    return (
        opp.comp_min is None
        and not opp.location_raw
        and not mentions_team(opp.description_md)
    )


async def org_simultaneous_generic(
    session: AsyncSession, org_id: int | None, *, exclude_opp_id: int | None = None
) -> int:
    """How many currently-posted (not closed) opportunities this org has with generic descriptions.
    More than 20 is a moderate ghost signal: a company blanketing boards with contentless reqs."""
    if org_id is None:
        return 0
    rows = (
        (
            await session.execute(
                select(Opportunity).where(
                    Opportunity.org_id == org_id,
                    Opportunity.liveness.in_(_LIVE),
                )
            )
        )
        .scalars()
        .all()
    )
    return sum(1 for o in rows if o.id != exclude_opp_id and _is_generic(o))


async def req_reappeared(session: AsyncSession, opp: Opportunity) -> bool:
    """True when this live posting's requisition id (external_id) matches a DIFFERENT posting from
    the same source that was marked closed: a req that will not die is a very strong signal."""
    if not opp.external_id or opp.source_id is None:
        return False
    n = (
        await session.execute(
            select(func.count())
            .select_from(Opportunity)
            .where(
                Opportunity.external_id == opp.external_id,
                Opportunity.source_id == opp.source_id,
                Opportunity.id != opp.id,
                Opportunity.liveness == "closed",
            )
        )
    ).scalar_one()
    return n > 0


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def reposted_identical(opp: Opportunity) -> bool:
    """Republished with a fresh date_posted but an identical content_hash: the content never
    materially changed (version <= 1) yet it keeps being re-listed with a newer post date and has
    been seen several times. Detectable from the fields normalization already maintains."""
    if (opp.version or 1) > 1 or (opp.seen_count or 0) < 3:
        return False
    posted = _to_date(opp.date_posted)
    first = _to_date(opp.first_seen_at)
    return posted is not None and first is not None and posted > first


async def recompute_ghost(session: AsyncSession, opp: Opportunity) -> float:
    """Recompute ghost_score INCLUDING the cross-opportunity aggregates and persist it on the
    opportunity. This is the score the UI badges and that feeds Block G of the evaluation prompt."""
    simultaneous = await org_simultaneous_generic(session, opp.org_id, exclude_opp_id=opp.id)
    score = compute_ghost_score(
        seen_count=opp.seen_count,
        first_seen_at=opp.first_seen_at,
        last_seen_at=opp.last_seen_at,
        version=opp.version,
        has_comp=opp.comp_min is not None,
        has_location=bool(opp.location_raw),
        has_team=mentions_team(opp.description_md),
        reposted_identical=reposted_identical(opp),
        req_reappeared=await req_reappeared(session, opp),
        org_simultaneous=simultaneous,
    )
    opp.ghost_score = score
    return score


def ghost_badge(score: float | None) -> dict | None:
    """The badge the UI shows for a ghost score, or None below the threshold worth flagging. A badge
    is a warning, never a rejection: a high ghost score keeps the opportunity, it just labels it."""
    if score is None:
        return None
    if score >= 0.7:
        return {"label": "Likely ghost", "level": "warning", "score": round(score, 2)}
    if score >= 0.4:
        return {"label": "Possible ghost", "level": "info", "score": round(score, 2)}
    return None
