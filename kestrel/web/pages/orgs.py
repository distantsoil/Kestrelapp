"""Organization page (SPEC-INTERFACES 2.1, lines 225-227). Everything the operator has ever
learned about one org in one place: accumulated signals over time, the latest research pass,
contacts with their drafted (never sent — hard rule 1) outreach, warm-path finder results, every
opportunity ever seen there, and the comp corpus that touches it.

Reference pattern followed: pursuits.py (HX-Request returns a partial, full load extends
base.html) and documents.py (a gather-into-dicts helper kept separate from the route)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import CompObservation, Contact, Opportunity, Org, OrgResearch, OutreachDraft
from kestrel.research.graph import warm_paths
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.redact import redact_name, redaction_map

router = APIRouter()

# Hard rule 6: any figure from fewer than five observations is labeled insufficient, never shown
# as if it were solid.
_MIN_SAMPLE = 5


def _group_comp(rows: list[CompObservation]) -> list[dict]:
    """Group raw comp_observation rows into (title, geo, interval) buckets with a sample size, so
    the page never renders a single stray observation as if it were a market range."""
    groups: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r.normalized_title, r.geo_bucket or "-", r.interval or "-")
        g = groups.setdefault(
            key,
            {
                "normalized_title": r.normalized_title,
                "geo_bucket": r.geo_bucket,
                "interval": r.interval,
                "n": 0,
                "min": None,
                "max": None,
                "currencies": set(),
            },
        )
        g["n"] += 1
        if r.min is not None:
            g["min"] = r.min if g["min"] is None else min(g["min"], r.min)
        if r.max is not None:
            g["max"] = r.max if g["max"] is None else max(g["max"], r.max)
        if r.currency:
            g["currencies"].add(r.currency)

    out = []
    for g in groups.values():
        g["insufficient"] = g["n"] < _MIN_SAMPLE
        currencies = g.pop("currencies")
        g["currency"] = next(iter(currencies)) if len(currencies) == 1 else (
            "mixed" if currencies else None
        )
        out.append(g)
    out.sort(key=lambda g: (-g["n"], g["normalized_title"] or ""))
    return out


async def _gather(session: AsyncSession, org: Org) -> dict:
    """Everything the template needs for one org, as plain dicts/lists so the template stays a
    dumb renderer. Every query is scoped to this one org_id."""
    signals = sorted(
        (org.signals or []),
        key=lambda s: (s.get("observed_at") or "") if isinstance(s, dict) else "",
        reverse=True,
    )

    research = (
        (
            await session.execute(
                select(OrgResearch)
                .where(OrgResearch.org_id == org.id)
                .order_by(OrgResearch.researched_at.desc(), OrgResearch.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    contacts = (
        (
            await session.execute(
                select(Contact).where(Contact.org_id == org.id).order_by(Contact.name)
            )
        )
        .scalars()
        .all()
    )
    drafts_by_contact: dict[int, list[OutreachDraft]] = {}
    contact_ids = [c.id for c in contacts]
    if contact_ids:
        drafts = (
            (
                await session.execute(
                    select(OutreachDraft)
                    .where(OutreachDraft.contact_id.in_(contact_ids))
                    .order_by(OutreachDraft.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for d in drafts:
            drafts_by_contact.setdefault(d.contact_id, []).append(d)
    contact_rows = [{"contact": c, "drafts": drafts_by_contact.get(c.id, [])} for c in contacts]

    warm_path_rows = await warm_paths(session, org.id)

    opportunities = (
        (
            await session.execute(
                select(Opportunity)
                .where(Opportunity.org_id == org.id)
                .order_by(Opportunity.first_seen_at.desc(), Opportunity.id.desc())
            )
        )
        .scalars()
        .all()
    )

    opp_ids_subq = select(Opportunity.id).where(Opportunity.org_id == org.id)
    conditions = [CompObservation.opportunity_id.in_(opp_ids_subq)]
    if org.size_band:
        conditions.append(CompObservation.org_size_band == org.size_band)
    comp_raw = (
        (await session.execute(select(CompObservation).where(or_(*conditions))))
        .scalars()
        .all()
    )
    # A row can satisfy both conditions (own opportunity AND matching size band); count it once.
    seen: set[int] = set()
    comp_deduped = []
    for r in comp_raw:
        if r.id in seen:
            continue
        seen.add(r.id)
        comp_deduped.append(r)

    return {
        "signals": signals,
        "research": research,
        "contacts": contact_rows,
        "warm_paths": warm_path_rows,
        "opportunities": opportunities,
        "comp_groups": _group_comp(comp_deduped),
        "comp_n_total": len(comp_deduped),
    }


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
async def page(request: Request, org_id: int):
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            if request.headers.get("HX-Request"):
                return HTMLResponse(
                    f'<div class="warnbox">Organization {org_id} not found.</div>', status_code=404
                )
            ctx = {
                "title": "Organization", "query": dict(request.query_params),
                "found": False, "org_id": org_id,
            }
            return templates().TemplateResponse(request, "orgs.html", ctx, status_code=404)

        data = await _gather(s, org)
        settings = await load_settings_with_db(s)
        display_name = org.name
        if settings.confidential_mode:
            mapping = await redaction_map(s)
            display_name = redact_name(org.name, mapping, enabled=True)

    ctx = {
        "title": display_name or f"Org {org.id}",
        "query": dict(request.query_params),
        "found": True,
        "org": org,
        "display_name": display_name,
        **data,
    }
    tmpl = "partials/org_detail.html" if request.headers.get("HX-Request") else "orgs.html"
    return templates().TemplateResponse(request, tmpl, ctx)
