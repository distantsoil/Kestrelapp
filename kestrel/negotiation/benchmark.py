"""Offer benchmarking (SPEC-MODULES 4.2). Grounded entirely in the operator's OWN accumulated data,
never model priors: the comp corpus for the normalized title / geography / organization size band,
the posting's stated band, real open competing offers, and the operator's own history.

Constraint 6 is the whole point here: the output is a range WITH its sample size, and under five
observations it says "insufficient data" rather than presenting a precise-looking number from three
points. The corpus is narrowed as tightly as five observations allow (title+geo+size), widening only
when a tighter cut is too thin, and the scope is always stated so the operator knows what they are
looking at."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import EngagementOffer, Offer, Opportunity, Org, Pursuit
from kestrel.intelligence.comp import MIN_OBSERVATIONS, CompRange, comp_range
from kestrel.negotiation.valuation import engagement_value, first_year_comp

# CompRange / comp_range / MIN_OBSERVATIONS are the canonical comp-corpus query (SPEC-MODULES 6.1),
# re-exported here so the negotiation module keeps a single import surface.
__all__ = ["CompRange", "MIN_OBSERVATIONS", "OfferBenchmark", "benchmark_offer", "comp_range"]
_OPEN_STATUSES = ("verbal", "written")


@dataclass
class OfferBenchmark:
    pursuit_id: int
    track: str
    offer_first_year: float | None
    stated_band: tuple[float | None, float | None] | None
    inside_stated_band: bool | None
    comp: CompRange
    competing: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    note: str = ""


async def _latest_offer(session: AsyncSession, pursuit_id: int, track: str):
    model = EngagementOffer if track == "consulting" else Offer
    return (
        await session.execute(
            select(model)
            .where(model.pursuit_id == pursuit_id)
            .order_by(model.received_at.desc(), model.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def benchmark_offer(session: AsyncSession, pursuit_id: int) -> OfferBenchmark | None:
    """Benchmark the pursuit's latest offer against the operator's own data. Employment offers get
    the salary comp corpus; consulting offers get engagement value and any competing engagement
    offers (there is no day-rate corpus in this stack, stated in the private decision log)."""
    pursuit = await session.get(Pursuit, pursuit_id)
    if pursuit is None:
        return None
    track = pursuit.track or "employment"
    offer = await _latest_offer(session, pursuit_id, track)

    opp = await session.get(Opportunity, pursuit.opportunity_id) if pursuit.opportunity_id else None
    org = await session.get(Org, opp.org_id) if opp and opp.org_id else None

    if track == "consulting":
        first_year = engagement_value(offer) if offer else None
        comp = CompRange(n=0, sufficient=False, scope="no day-rate corpus", currency="USD")
        competing = await _competing(session, pursuit_id, "consulting")
        history = await _history(session, "consulting")
        note = "Consulting: no day-rate comp corpus here; compare against the stated scope."
        return OfferBenchmark(
            pursuit_id=pursuit_id, track=track, offer_first_year=first_year, stated_band=None,
            inside_stated_band=None, comp=comp, competing=competing, history=history, note=note,
        )

    first_year = first_year_comp(offer) if offer else None
    geo = (opp.state or opp.country or opp.city) if opp else None
    comp = await comp_range(
        session,
        normalized_title=(opp.title if opp else None),
        geo_bucket=geo,
        org_size_band=(org.size_band if org else None),
        currency=(opp.comp_currency if opp and opp.comp_currency else "USD"),
    )
    stated_band = None
    inside = None
    # A job-board estimate (comp_source "inferred") is not a stated band to hold an offer against.
    if (opp and opp.comp_source != "inferred"
            and (opp.comp_min is not None or opp.comp_max is not None)):
        stated_band = (opp.comp_min, opp.comp_max)
        base = getattr(offer, "base", None) if offer else None
        if base is not None:
            lo = opp.comp_min if opp.comp_min is not None else float("-inf")
            hi = opp.comp_max if opp.comp_max is not None else float("inf")
            inside = lo <= base <= hi

    competing = await _competing(session, pursuit_id, "employment")
    history = await _history(session, "employment")
    return OfferBenchmark(
        pursuit_id=pursuit_id, track=track, offer_first_year=first_year, stated_band=stated_band,
        inside_stated_band=inside, comp=comp, competing=competing, history=history,
        note=comp.summary(),
    )


async def _competing(session: AsyncSession, pursuit_id: int, track: str) -> list[dict]:
    """Real, currently-open offers on OTHER pursuits. Only these may ever justify a competing-offer
    script; the system never invents or implies an offer that does not exist (SPEC 4.3)."""
    model = EngagementOffer if track == "consulting" else Offer
    rows = (
        (
            await session.execute(
                select(model).where(
                    model.pursuit_id != pursuit_id, model.status.in_(_OPEN_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    out = []
    for o in rows:
        value = engagement_value(o) if track == "consulting" else first_year_comp(o)
        out.append({"pursuit_id": o.pursuit_id, "status": o.status, "value": value})
    return out


async def _history(session: AsyncSession, track: str) -> list[dict]:
    """What the operator has been offered and ACCEPTED before, for their own reference."""
    model = EngagementOffer if track == "consulting" else Offer
    rows = (
        (await session.execute(select(model).where(model.status == "accepted"))).scalars().all()
    )
    out = []
    for o in rows:
        value = engagement_value(o) if track == "consulting" else first_year_comp(o)
        out.append({"pursuit_id": o.pursuit_id, "value": value, "received_at": o.received_at})
    return out
