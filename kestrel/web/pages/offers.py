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

from dataclasses import replace
from html import escape
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    EngagementOffer,
    Offer,
    Opportunity,
    Org,
    Pursuit,
    Setting,
)
from kestrel.llm.client import LLMClient
from kestrel.negotiation.benchmark import benchmark_offer
from kestrel.negotiation.compare import (
    AXES,
    QUAL_AXES,
    ComparisonMatrix,
    compare_offers,
    offer_comp_from,
)
from kestrel.negotiation.scripts import ScriptResult, draft_scripts
from kestrel.privacy import EmployerMap
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.redact import confidential_once, redact_json, redact_name, redact_text

# Human-readable labels for comparison matrix axes
AXIS_LABELS = {
    "total_first_year": "Total first-year comp",
    "four_year_value": "Four-year value",
    "level_scope": "Level and scope",
    "reporting_line": "Reporting line",
    "location_travel": "Location and travel",
    "org_risk": "Company risk",
    "trajectory_fit": "Fit with where you are headed",
}

log = structlog.get_logger()

router = APIRouter()

_OPEN_STATUSES = ("verbal", "written")
_OFFER_STATUSES = ("verbal", "written", "revised", "declined", "accepted")
# Statuses a pursuit can be in and still meaningfully hold an offer. A closed pursuit still
# accepts one (the operator may be closing the loop late, or keeping the record), so this widens
# the picker rather than excluding history.
_LIVE_PURSUIT_STATUSES = (
    "shortlisted", "preparing", "applied", "screening", "interviewing", "final", "offer",
    "negotiating", "scoping", "proposal_sent", "conversation",
)
# Which live stages an offer may be recorded against, per track's target stage. An offer most
# often arrives when the pursuit is already applied / screening / interviewing / final, so all of
# them move; closed stages are excluded so recording an offer never quietly reopens a pursuit.
_MOVABLE_TO_OFFER = {
    "offer": (
        "shortlisted", "preparing", "applied", "screening", "interviewing", "final",
    ),
    "negotiating": (
        "shortlisted", "researching", "outreach_sent", "conversation", "scoping",
        "proposal_sent",
    ),
}
_WEIGHTS_KEY = "negotiation.matrix_weights"
_QUAL_MAX = 5.0


def _model_for_track(track: str):
    """The offer table for a track. `Offer` and `EngagementOffer` have independent id sequences, so
    a bare id is ambiguous across them and the track is what disambiguates."""
    if track == "employment":
        return Offer
    if track == "consulting":
        return EngagementOffer
    return None


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
    ctx = await _offers_ctx(request)
    tmpl = "partials/offers_list.html" if request.headers.get("HX-Request") else "offers.html"
    return templates().TemplateResponse(request, tmpl, ctx)


async def _pursuit_names(session: AsyncSession, pursuit_id: int) -> tuple[str | None, str | None]:
    """(organization, role) for a pursuit, for the competing-offers line."""
    pursuit = await session.get(Pursuit, pursuit_id)
    opp = (
        await session.get(Opportunity, pursuit.opportunity_id)
        if pursuit and pursuit.opportunity_id
        else None
    )
    org = await session.get(Org, opp.org_id) if opp and opp.org_id else None
    return (org.name if org else (opp.org_name if opp else None)), (opp.title if opp else None)


async def _pickable_pursuits(session: AsyncSession) -> list[dict]:
    """Pursuits an offer can be recorded against: a live one, or anything with no offer yet."""
    rows = (
        (await session.execute(select(Pursuit).order_by(Pursuit.status, Pursuit.id)))
        .scalars()
        .all()
    )
    have_offer: set[int] = set()
    for model in (Offer, EngagementOffer):
        for pid in (
            await session.execute(select(model.pursuit_id).distinct())
        ).scalars().all():
            have_offer.add(pid)
    out: list[dict] = []
    for p in rows:
        if p.status not in _LIVE_PURSUIT_STATUSES and p.id in have_offer:
            continue
        opp = (
            await session.get(Opportunity, p.opportunity_id) if p.opportunity_id else None
        )
        org = await session.get(Org, opp.org_id) if opp and opp.org_id else None
        out.append(
            {
                "pursuit": p,
                "track": p.track or "employment",
                "org_name": org.name if org else (opp.org_name if opp else None),
                "title": opp.title if opp else "Untitled",
                "has_offer": p.id in have_offer,
            }
        )
    return out


async def _weights(session: AsyncSession) -> dict[str, float]:
    """The operator's matrix weights, or None so compare_offers applies its equal defaults."""
    row = await session.get(Setting, _WEIGHTS_KEY)
    if row is None or not isinstance(row.value, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in row.value.items():
        if k in AXES and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    return out


def _opt_float(form: Any, name: str) -> float | None:
    """A blank or absent numeric field is None (not recorded), never 0. A 0 base or a 0 bonus
    target is a real figure the operator typed and must survive as 0."""
    raw = form.get(name)
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _opt_int(form: Any, name: str) -> int | None:
    v = _opt_float(form, name)
    return int(v) if v is not None else None


def _opt_str(form: Any, name: str) -> str | None:
    raw = form.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


@router.post("/offers", response_class=HTMLResponse)
async def record_offer(request: Request):
    """Record an offer against a pursuit. This is the only place an Offer/EngagementOffer row is
    ever created; everything else on this page (benchmark, comparison matrix, competing-offer
    detection, negotiation scripts) reads what is recorded here.

    Nothing is sent anywhere and nothing is submitted on the operator's behalf: this records what
    the operator was told, so Kestrel can value it and draft scripts they deliver themselves."""
    form = await request.form()
    pursuit_id = _opt_int(form, "pursuit_id")
    if pursuit_id is None:
        return _offer_form_error(request, "Choose which pursuit this offer is for.")

    async with sessionmaker()() as s:
        pursuit = await s.get(Pursuit, pursuit_id)
        if pursuit is None:
            return _offer_form_error(request, "That pursuit no longer exists.")
        # D-RR1-2: the PURSUIT's track is authoritative, never the form's. The track select
        # defaults to employment regardless of the pursuit, so a consulting pursuit's offer was
        # filed in the employment table -- which `benchmark_offer` and `_latest_offer`, both of
        # which pick the table from `pursuit.track`, never read. The offer was invisible.
        track = pursuit.track or "employment"
        if track not in ("employment", "consulting"):
            return _offer_form_error(request, "That pursuit has an unknown track.")
        status = str(form.get("status") or "verbal")
        if status not in _OFFER_STATUSES:
            return _offer_form_error(request, "Unknown offer status.")
        received = _opt_str(form, "received_at") or utcnow_iso()
        qual = _parse_qual(form)

        if track == "consulting":
            row = EngagementOffer(
                pursuit_id=pursuit_id,
                day_rate=_opt_float(form, "day_rate"),
                days_committed=_opt_int(form, "days_committed"),
                duration_weeks=_opt_int(form, "duration_weeks"),
                payment_terms=_opt_str(form, "payment_terms"),
                expenses=_opt_str(form, "expenses"),
                renewal_terms=_opt_str(form, "renewal_terms"),
                exclusivity=_opt_str(form, "exclusivity"),
                ip_terms=_opt_str(form, "ip_terms"),
                notice_period=_opt_str(form, "notice_period"),
                status=status,
                notes_md=_opt_str(form, "notes_md"),
                qual_json=qual,
                received_at=received,
            )
        else:
            row = Offer(
                pursuit_id=pursuit_id,
                base=_opt_float(form, "base"),
                bonus_target_pct=_opt_float(form, "bonus_target_pct"),
                equity_description=_opt_str(form, "equity_description"),
                equity_value_estimate=_opt_float(form, "equity_value_estimate"),
                signing=_opt_float(form, "signing"),
                benefits_notes=_opt_str(form, "benefits_notes"),
                pto_days=_opt_int(form, "pto_days"),
                start_date=_opt_str(form, "start_date"),
                deadline=_opt_str(form, "deadline"),
                level_title=_opt_str(form, "level_title"),
                reports_to=_opt_str(form, "reports_to"),
                location_policy=_opt_str(form, "location_policy"),
                status=status,
                notes_md=_opt_str(form, "notes_md"),
                qual_json=qual,
                received_at=received,
            )
        s.add(row)
        # A recorded offer means the pursuit is being negotiated: move it there so the pursuit
        # board and the next-action sweep agree with what is recorded here. Through
        # `move_to_status`, THE one sanctioned way a card changes (review): setting
        # `pursuit.status` directly wrote no `status_event`, so the transition had no history.
        #
        # Every live stage an offer actually arrives at moves, not just the first four: an offer
        # most often lands when the pursuit is already `applied`, `screening`, `interviewing` or
        # `final`, and leaving the card behind meant the board disagreed with /offers at exactly
        # the moment it mattered (review). Closed stages are left alone, so recording an
        # offer on a rejected pursuit does not quietly reopen it.
        from kestrel.services.pursuit_board import move_to_status

        target = "negotiating" if track == "consulting" else "offer"
        if pursuit.status in _MOVABLE_TO_OFFER.get(target, ()):
            await move_to_status(s, pursuit, target, note="Offer recorded")
        await s.commit()
        row_id = row.id

    return await _list_response(
        request, f"Offer recorded (id {row_id}). It is benchmarked against your own data below."
    )


@router.post("/offers/{offer_id}/qual", response_class=HTMLResponse)
async def save_qual(request: Request, offer_id: int):
    """Save the operator's 0-5 ratings for the comparison matrix's qualitative axes. These are
    judgments the tool must not make for them (compare.OfferComp.qual).

    The posted `track` selects the table, and the lookup resolves inside that ONE table.
    `Offer.id` and `EngagementOffer.id` are independent sequences, so both tables hand out id 1 to
    their first row: resolving a bare id against `Offer` first meant rating a consulting offer
    silently overwrote an unrelated employment offer's ratings (review). With no `track`
    posted, an id that exists in BOTH tables is refused rather than guessed.
    """
    form = await request.form()
    qual = _parse_qual(form)
    track = str(form.get("track") or "").strip()
    async with sessionmaker()() as s:
        models: list = []
        model = _model_for_track(track)
        if model is not None:
            models = [model]
        elif not track:
            models = [Offer, EngagementOffer]
        else:
            return HTMLResponse(
                f'<div class="warnbox" role="status">Unknown track: {escape(track)}</div>'
            )
        row = None
        for candidate in models:
            found = await s.get(candidate, offer_id)
            if found is not None:
                if row is None:
                    row = found
                else:
                    # Ambiguous without a track: refuse rather than write to a coin toss.
                    return HTMLResponse(
                        '<div class="warnbox" role="status">That id matches both an employment '
                        "and a consulting offer. Re-save from the offer's own card.</div>"
                    )
        if row is None:
            return HTMLResponse(
                '<div class="warnbox" role="status">That offer no longer exists.</div>'
            )
        row.qual_json = qual
        await s.commit()
    return await _list_response(request, "Ratings saved.")


@router.post("/offers/weights", response_class=HTMLResponse)
async def save_weights(request: Request):
    """The operator's own matrix weights. Blank means 0 for that axis; the module renormalizes,
    so a blank column simply stops mattering to the total."""
    form = await request.form()
    raw = {
        k: (_opt_float(form, k) or 0.0) for k in AXES if _opt_float(form, k) is not None
    }
    async with sessionmaker()() as s:
        row = await s.get(Setting, _WEIGHTS_KEY)
        if row is None:
            s.add(Setting(key=_WEIGHTS_KEY, value=raw))
        else:
            row.value = raw
        await s.commit()
    return await _list_response(request, "Weights saved.")


def _parse_qual(form: Any) -> dict[str, float] | None:
    """The five qualitative ratings from a form, or None when the operator left them all blank.
    A blank is 'not rated' and stays out of the matrix; compare_offers renormalizes over the axes
    present, so an unrated axis is not scored as zero."""
    out: dict[str, float] = {}
    for axis in QUAL_AXES:
        v = _opt_float(form, f"qual_{axis}")
        if v is None:
            continue
        out[axis] = max(0.0, min(_QUAL_MAX, v))
    return out or None


def _offer_form_error(request: Request, message: str) -> HTMLResponse:
    return HTMLResponse(f'<div class="warnbox" role="status">{escape(message)}</div>')


_OFFER_TEXT_FIELDS = ("payment_terms", "exclusivity", "equity_description", "level_title")


def _redact_scripts(result: ScriptResult, mapping: EmployerMap, *, enabled: bool) -> ScriptResult:
    """Every model-written string the scripts partial renders, with the current employer shown as
    the operator's "Show it as" text. The walk-away is the operator's own figure, left unchanged."""
    if not enabled or not mapping:
        return result
    return replace(
        result,
        situation_read=redact_text(result.situation_read, mapping, enabled=True) or "",
        scripts=redact_json(result.scripts, mapping, enabled=True),
        leverage_inventory=redact_json(result.leverage_inventory, mapping, enabled=True),
        risks=redact_json(result.risks, mapping, enabled=True),
        unsupported_scenarios=redact_json(result.unsupported_scenarios, mapping, enabled=True),
    )


async def _offers_ctx(request: Request, message: str | None = None) -> dict:
    """The list's context: open offers with their benchmarks, the comparison matrix when there is
    more than one, and the pickers. Built in one place so `page` and every mutation render from
    the same stored state and can never disagree."""
    async with sessionmaker()() as s:
        rows = await _open_offers(s)
        conf_on, emp_map = await confidential_once(s)
        for row in rows:
            row["benchmark"] = await benchmark_offer(s, row["pursuit"].id)
            for c in row["benchmark"].competing if row["benchmark"] else []:
                c["org_name"], c["title"] = await _pursuit_names(s, c["pursuit_id"])
        # Confidential mode: the current employer is shown as the operator's "Show it as" text
        # in every name on this page, the competing-offer lines included.
        for row in rows:
            row["org_name"] = redact_name(row["org_name"], emp_map, enabled=conf_on)
            if row["opp"] is not None:
                row["title_shown"] = redact_text(row["opp"].title, emp_map, enabled=conf_on)
            # The operator's typed terms are free text and can name the employer too.
            row["terms"] = {
                f: redact_text(getattr(row["offer"], f, None), emp_map, enabled=conf_on)
                for f in _OFFER_TEXT_FIELDS
            }
            for c in row["benchmark"].competing if row["benchmark"] else []:
                c["org_name"] = redact_name(c["org_name"], emp_map, enabled=conf_on)
                c["title"] = redact_text(c["title"], emp_map, enabled=conf_on)
        weights = await _weights(s)
        pickable = await _pickable_pursuits(s)
        for p in pickable:
            p["org_name"] = redact_name(p["org_name"], emp_map, enabled=conf_on)
            p["title"] = redact_text(p["title"], emp_map, enabled=conf_on)
        matrix: ComparisonMatrix | None = None
        if len(rows) > 1:
            comps = [
                offer_comp_from(
                    row["offer"],
                    offer_id=row["offer"].id,
                    label=(
                        f"{row['org_name'] or 'Unknown org'} · "
                        f"{row.get('title_shown') or 'Untitled'}"
                    ),
                    # The operator's own ratings for the qualitative axes, stored with the offer.
                    # Without them every qualitative axis reads None and the matrix ranks offers
                    # on money alone, which is not what the table says it does.
                    qual=row["offer"].qual_json or None,
                    track=row["track"],
                )
                for row in rows
            ]
            matrix = compare_offers(comps, weights=weights)
        return {
            "title": "Negotiation",
            "query": dict(request.query_params),
            "offers": rows,
            "matrix": matrix,
            "pursuits": pickable,
            "axes": AXES,
            "qual_axes": QUAL_AXES,
            "axis_labels": AXIS_LABELS,
            "offer_statuses": _OFFER_STATUSES,
            "weights": weights,
            "message": message,
        }


async def _list_response(request: Request, message: str | None = None) -> Any:
    """Every mutation re-renders the whole list partial, so the benchmark and matrix shown always
    come from what is actually stored rather than from a stale client view."""
    return templates().TemplateResponse(
        request, "partials/offers_list.html", await _offers_ctx(request, message)
    )


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
                if result is not None:
                    # Confidential mode: model prose names the current employer like anything
                    # else on this page does, so it is redacted the same way.
                    conf_on, emp_map = await confidential_once(s)
                    result = _redact_scripts(result, emp_map, enabled=conf_on)
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
        log.warning("offer_scripts_failed", pursuit_id=pursuit_id, error_type=type(exc).__name__,
                    error=str(exc))
        ctx = {"error": f"Scripts need the local model running: {plain_error(exc)}",
               "result": None}
    return templates().TemplateResponse(request, "partials/offer_scripts.html", ctx)
