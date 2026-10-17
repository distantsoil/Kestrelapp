"""The learning-loop surface (SPEC-MODULES 5.5): the /calibrate active-learning queue and the /api
endpoints the UI, CLI, and MCP consume — label capture (the round-trip that feeds everything),
weight-calibration propose/accept/reject, reranker train/status, and outcome analysis. Labeling is a
one-click side effect of reviewing, never a separate chore."""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings, get_settings
from kestrel.db.models import Opportunity
from kestrel.learning import calibration as calib
from kestrel.learning import outcomes as outcomes_mod
from kestrel.learning import reranker as reranker_mod
from kestrel.learning.active import rank_uncertain
from kestrel.learning.capture import record_label
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import active_track, sessionmaker, templates

router = APIRouter()


async def _promotion_panel_data(
    session: AsyncSession, track: str, settings: Settings
) -> tuple[reranker_mod.PromotionStatus | None, str | None]:
    """The promotion-progress panel must never take the whole /calibrate page down with it: an
    unexpected failure here (a malformed metrics blob from an old row, an unavailable DB) is caught
    and rendered as an inline message instead of a 500. A "not ready yet" status is NOT an error and
    never reaches this except-clause; promotion_status returns that as ordinary data."""
    try:
        return await reranker_mod.promotion_status(session, track, settings), None
    except Exception as exc:  # a status panel must degrade gracefully, never crash the page
        return None, f"Promotion status unavailable: {exc}"


@router.get("/calibrate", response_class=HTMLResponse)
async def calibrate_page(request: Request):
    track = active_track(request)  # the active tab (D-UI-57)
    settings = get_settings()
    hx = request.headers.get("HX-Request")
    async with sessionmaker()() as s:
        items = await rank_uncertain(s, track, settings, limit=10)
        # The promotion panel only renders on the full page, not the HTMX queue-only swap used when
        # labeling an item, so it is skipped there rather than computed and thrown away.
        promo, promo_error = (None, None) if hx else await _promotion_panel_data(s, track, settings)
    ctx = {
        "items": items, "track": track, "query": {"track": track},
        "promo": promo, "promo_error": promo_error,
    }
    tmpl = "partials/calibrate_queue.html" if hx else "calibrate.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.get("/api/learning/queue")
async def api_queue(request: Request):
    track = active_track(request)
    settings = get_settings()
    async with sessionmaker()() as s:
        items = await rank_uncertain(s, track, settings, limit=10)
    return {
        "track": track,
        "items": [
            {
                "opportunity_id": u.opportunity.id, "title": u.opportunity.title,
                "score": u.evaluation.score, "weighted": u.evaluation.weighted_score,
                "verdict": u.evaluation.verdict, "uncertainty": u.uncertainty,
                "components": u.components,
            }
            for u in items
        ],
    }


@router.post("/api/learning/label")
async def api_label(request: Request):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    else:
        data = dict(await request.form())
    try:
        opp_id = int(data.get("opportunity_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "opportunity_id required"}, status_code=400)
    score = data.get("score")
    try:
        score = float(score) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None
    async with sessionmaker()() as s:
        # Checked up front: the label row has a foreign key to opportunity, so an unknown id used
        # to surface as an IntegrityError on commit, a 500 with a database traceback.
        if await s.get(Opportunity, opp_id) is None:
            return JSONResponse({"error": "opportunity not found"}, status_code=404)
        row = await record_label(
            s,
            opportunity_id=opp_id,
            track=str(data.get("track") or "employment"),
            score=score,
            verdict=data.get("verdict") or None,
            rationale=data.get("rationale") or None,
            source=str(data.get("source") or "active_learning"),
        )
    if request.headers.get("HX-Request"):
        # The verdict is operator-supplied text echoed into an HTMX swap; escape it.
        verdict = html.escape(row.verdict or "-", quote=True)
        return HTMLResponse(
            f'<span class="done" data-testid="labeled">labeled ({verdict}'
            f'{" " + format(row.score, ".1f") if row.score is not None else ""})</span>'
        )
    return {"ok": True, "label_id": row.id}


@router.post("/api/learning/calibration/propose")
async def api_propose(request: Request):
    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)  # baseline reflects any accepted calibration
        cal = await calib.propose_calibration(s, track, settings)
    if cal is None:
        return {"proposed": False, "reason": "insufficient labeled examples"}
    return {
        "proposed": True, "id": cal.id, "track": cal.track, "n_samples": cal.n_samples,
        "previous_weights": cal.previous_weights, "proposed_weights": cal.proposed_weights,
        "cv_mae_previous": cal.cv_mae_previous, "cv_mae_proposed": cal.cv_mae_proposed,
    }


@router.post("/api/learning/calibration/{cal_id}/accept")
async def api_accept(cal_id: int):
    async with sessionmaker()() as s:
        ok = await calib.accept_calibration(s, cal_id)
    return JSONResponse({"accepted": ok}, status_code=200 if ok else 404)


@router.post("/api/learning/calibration/{cal_id}/reject")
async def api_reject(cal_id: int):
    async with sessionmaker()() as s:
        ok = await calib.reject_calibration(s, cal_id)
    return JSONResponse({"rejected": ok}, status_code=200 if ok else 404)


@router.post("/api/learning/train")
async def api_train(request: Request):
    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        result = await reranker_mod.train_reranker(s, track, settings)
    return {
        "track": result.track, "n_labeled": result.n_labeled, "promoted": result.promoted,
        "criteria": result.criteria, "metrics": result.metrics, "reason": result.reason,
    }


@router.get("/api/learning/reranker")
async def api_reranker_status(request: Request):
    track = active_track(request)
    from kestrel.llm.embeddings import current_model
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        # The same refusal the gate applies: a reranker trained on another embed model is not
        # the active one, whatever its row says.
        loaded = await reranker_mod.load_active_reranker(
            s, track, embed_model=current_model(settings))
    if loaded is None:
        return {"active": False}
    m = loaded.ml_model
    return {
        "active": True, "version": m.version, "n_samples": m.n_samples,
        "metrics": m.metrics, "sklearn_version": m.sklearn_version,
    }


@router.get("/api/learning/outcomes")
async def api_outcomes(request: Request):
    track = active_track(request)
    async with sessionmaker()() as s:
        report = await outcomes_mod.analyze_outcomes(s, track)
    return {
        "track": report.track, "n_outcomes": report.n_outcomes,
        "discriminating": report.discriminating, "headline": report.headline,
        "by_score_band": [
            {"label": b.label, "n": b.n, "responded": b.responded,
             "rate": b.rate, "insufficient": b.insufficient}
            for b in report.by_score_band
        ],
        "suggested_ghosting_days": report.suggested_ghosting_days,
    }
