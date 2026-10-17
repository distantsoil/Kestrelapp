"""Negotiation (SPEC-INTERFACES 2.1). Offer records benchmarked against the operator's OWN
accumulated data (comp corpus, stated band, real competing offers, accepted history), scripts
drafted for the operator to edit and deliver themselves, and a comparison matrix when more than one
offer is open.

Three hard rules are enforced here, not merely in the modules underneath:
  - The walk-away number is the operator's. The scripts form requires it as plain text; nothing on
    this page computes, defaults, or adjusts it.
  - Scripts are drafts. There is no send action anywhere on this page, and a script whose
    justification rests on nothing supplied is shown flagged.
  - Sample size is always visible. The benchmark always shows n; under five observations it shows
    CompRange's own "insufficient data" message rather than a precise-looking number.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import EngagementOffer, Offer, Opportunity, Org, Pursuit
from kestrel.llm.client import LLMClient
from kestrel.negotiation.benchmark import benchmark_offer
from kestrel.negotiation.compare import compare_offers, offer_comp_from
from kestrel.negotiation.scripts import draft_scripts
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()

_OPEN_STATUSES = ("verbal", "written")


async def _open_offers(session: AsyncSession) -> list[dict]:
    """The latest offer per pursuit across both tracks' offer tables, kept only when that latest
    offer is still open. Mirrors the "latest offer per pursuit" rule benchmark_offer itself uses
    (kestrel/negotiation/benchmark.py `_latest_offer`) so the list and the benchmark never disagree
    about which offer is current."""
    out: list[dict] = []
    for track, model in (("employment", Offer), ("consulting", EngagementOffer)):
        rows = (
            (
                await session.execute(
                    select(model).order_by(
                        model.pursuit_id, model.received_at.desc(), model.id.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        seen: set[int] = set()
        for o in rows:
            if o.pursuit_id in seen:
                continue
            seen.add(o.pursuit_id)
            if o.status not in _OPEN_STATUSES:
                continue
            pursuit = await session.get(Pursuit, o.pursuit_id)
            if pursuit is None:
                continue
            opp = (
                await session.get(Opportunity, pursuit.opportunity_id)
                if pursuit.opportunity_id
                else None
            )
            org = await session.get(Org, opp.org_id) if opp and opp.org_id else None
            out.append(
                {
                    "pursuit": pursuit,
                    "offer": o,
                    "track": track,
                    "opp": opp,
                    "org_name": org.name if org else (opp.org_name if opp else None),
                }
            )
    return out


@router.get("/offers", response_class=HTMLResponse)
async def page(request: Request):
    async with sessionmaker()() as s:
        rows = await _open_offers(s)
        for row in rows:
            row["benchmark"] = await benchmark_offer(s, row["pursuit"].id)
        matrix = None
        if len(rows) > 1:
            comps = [
                offer_comp_from(
                    row["offer"],
                    offer_id=row["offer"].id,
                    label=(
                        f"{row['org_name'] or 'Unknown org'} · "
                        f"{row['opp'].title if row['opp'] else 'Untitled'}"
                    ),
                )
                for row in rows
            ]
            matrix = compare_offers(comps)
    ctx = {
        "title": "Negotiation",
        "query": dict(request.query_params),
        "offers": rows,
        "matrix": matrix,
    }
    tmpl = "partials/offers_list.html" if request.headers.get("HX-Request") else "offers.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/offers/{pursuit_id}/scripts", response_class=HTMLResponse)
async def scripts(request: Request, pursuit_id: int):
    """Draft negotiation scripts for one pursuit's offer. `walk_away` is REQUIRED and supplied by
    the operator as plain text; there is no code path here that computes or defaults it, and
    draft_scripts echoes it back unchanged. A model that is not running degrades to a message, never
    a 500."""
    form = await request.form()
    walk_away = str(form.get("walk_away") or "").strip()
    if not walk_away:
        ctx = {"error": "A walk-away number is required before drafting scripts.", "result": None}
        return templates().TemplateResponse(request, "partials/offer_scripts.html", ctx)
    try:
        async with httpx.AsyncClient() as http:
            async with sessionmaker()() as s:
                client = LLMClient(await load_settings_with_db(s), http=http)
                result = await draft_scripts(s, client, pursuit_id, walk_away=walk_away)
                await s.commit()  # persists a quarantine row if the model call failed validation
        if result is None:
            ctx = {
                "error": (
                    "No pursuit found, or the model could not produce a valid response "
                    "(quarantined for review)."
                ),
                "result": None,
            }
        else:
            ctx = {"error": None, "result": result}
    except Exception as exc:  # a model that is not running must not 500 the page
        ctx = {"error": f"Scripts need the local model running: {exc}", "result": None}
    return templates().TemplateResponse(request, "partials/offer_scripts.html", ctx)
