"""The learning-loop surface (SPEC-MODULES 5.5): the /calibrate active-learning queue and the /api
endpoints the UI, CLI, and MCP consume — label capture (the round-trip that feeds everything),
weight-calibration propose/accept/reject, reranker train/status, and outcome analysis. Labeling is a
one-click side effect of reviewing, never a separate chore."""

from __future__ import annotations

import html
import json
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings, get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Calibration, Evaluation, Opportunity
from kestrel.learning import calibration as calib
from kestrel.learning import outcomes as outcomes_mod
from kestrel.learning import reranker as reranker_mod
from kestrel.learning import training
from kestrel.learning.active import UncertainItem, rank_uncertain
from kestrel.learning.capture import labeled_examples, record_label
from kestrel.learning.ledger import delete_label
from kestrel.pipeline.scoring import verdict_from_score
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import calibrate_view as cv
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

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
        log.warning(
            "promotion_status_failed", track=track, error_type=type(exc).__name__, error=str(exc)
        )
        return None, f"Promotion status unavailable: {plain_error(exc)}"


def _toast(
    message: str,
    *,
    undo_url: str | None = None,
    refresh: str | None = None,
    tone: str | None = None,
) -> dict:
    body: dict = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    if refresh:
        body["refresh"] = refresh  # where calibrate.js reloads the card after an Undo
    if tone:
        body["tone"] = tone
    return body


def _trigger(*events: dict) -> dict[str, str]:
    merged: dict = {}
    for e in events:
        merged.update(e)
    return {"HX-Trigger": json.dumps(merged, ensure_ascii=True)} if merged else {}


async def _latest_eval(s: AsyncSession, opp_id: int) -> Evaluation | None:
    return (
        (
            await s.execute(
                select(Evaluation)
                .where(Evaluation.opportunity_id == opp_id, Evaluation.tier == 2)
                .order_by(Evaluation.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def _page_ctx(
    request: Request,
    s: AsyncSession,
    *,
    since: str,
    skip: str,
    change: int | None = None,
    full: bool,
) -> dict:
    """Everything the card, the strips, the tune-up and the history draw from."""
    track = active_track(request)
    since = since or utcnow_iso()  # a blank value would count every rating ever made
    settings = await load_settings_with_db(s)
    promo, promo_error = await _promotion_panel_data(s, track, settings)
    queue = await cv.build_queue(s, track, settings, since=since, skip=skip)
    tune = None
    if len(await labeled_examples(s, track)) >= settings.learning.min_labels_for_calibration:
        try:
            cal = await cv.tune_proposal(s, track, settings)
        except Exception as exc:  # the suggestion must never take the rating card down with it
            log.warning("tune_proposal_failed", error_type=type(exc).__name__, error=str(exc))
            cal = None
        tune = cv.tune_view(cal) if cal is not None else None
    strips = await cv.build_strips(s, track, settings, promo=promo, promo_error=promo_error)
    ctx: dict = {
        "track": track,
        "query": {"track": track},
        "since": since,
        "queue": queue,
        "skip": ",".join(str(i) for i in queue.skipped),
        "strips": strips,
        "tune": tune,
        "history": await cv.build_history(s, track),
        "cv": cv,
        "full": full,
        "revisit": None,
        "reasons": cv.REASONS.get(track, cv.REASONS["employment"]),
        "card": None,
        "training": await training.state(s, track),
        "build_model": settings.learning.build_model,
    }
    item = queue.current
    if change is not None:
        revisit = await _revisit_item(s, track, change)
        if revisit is not None:  # a role with no evaluation cannot be changed; keep the queue
            item = ctx["revisit"] = revisit
    if item is not None:
        ctx["card"] = await _card_ctx(s, track, item, revisit=ctx["revisit"])
    elif queue.done:
        ctx["done_line"] = await cv.session_line(s, track, since)
    return ctx


async def _revisit_item(s: AsyncSession, track: str, opp_id: int):
    """One already-rated role, shaped like a queue item so the same card draws it."""
    opp = await s.get(Opportunity, opp_id)
    if opp is None or opp.track != track:
        return None
    ev = await _latest_eval(s, opp_id)
    prev = next(
        (e for e in reversed(await labeled_examples(s, track)) if e.opportunity_id == opp_id), None
    )
    if ev is None or prev is None:
        return None
    item = UncertainItem(opp, ev, 0.0, {})
    item.previous = round(prev.operator_score)  # type: ignore[attr-defined]
    return item


async def _card_ctx(s: AsyncSession, track: str, item, *, revisit) -> dict:
    opp, ev = item.opportunity, item.evaluation
    dims = cv.dim_scores(ev, track)
    text = (opp.description_md or "").strip()
    return {
        "opp": opp,
        "org": await cv.org_name(s, opp),
        "pay": cv.pay_line(opp),
        "place": cv.place_line(opp),
        "text": text,
        "excerpt": text if len(text) <= 900 else text[:900].rsplit(" ", 1)[0] + "...",
        "why": cv.why_asked(item) if revisit is None else "",
        "dims": dims,
        "read": cv.kestrel_read(ev, dims),
        "kestrel": ev.score,
        "previous": getattr(item, "previous", None),
    }


async def _swap_response(
    request: Request, s: AsyncSession, ctx: dict, *, events=None, all_parts: bool = True
):
    """The card in place, with the strips, the tune-up, the history and the sidebar count swapped
    out of band. The page itself never reloads (updates happen in place)."""
    from kestrel.web.routes_api import oob_nav_counts

    ctx["all_parts"] = all_parts
    resp = templates().TemplateResponse(request, "partials/calibrate_swap.html", ctx)
    resp.body += (await oob_nav_counts(s)).encode()
    resp.headers["content-length"] = str(len(resp.body))
    resp.headers.update(_trigger(*(events or [])))
    return resp


@router.get("/calibrate", response_class=HTMLResponse)
async def calibrate_page(request: Request):
    async with sessionmaker()() as s:
        ctx = await _page_ctx(request, s, since=utcnow_iso(), skip="", full=True)
    return templates().TemplateResponse(request, "calibrate.html", ctx)


@router.get("/calibrate/training", response_class=HTMLResponse)
async def training_status(request: Request):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = {"training": await training.state(s, active_track(request)),
               "build_model": settings.learning.build_model}
    return templates().TemplateResponse(request, "partials/calibrate_training.html", ctx)


@router.post("/calibrate/train", response_class=HTMLResponse)
async def train_from_calibrate(request: Request):
    async with sessionmaker()() as s:
        message = await training.request_training(s, active_track(request))
        settings = await load_settings_with_db(s)
        ctx = {"training": {**(await training.state(s, active_track(request))), "message": message},
               "build_model": settings.learning.build_model}
    return templates().TemplateResponse(request, "partials/calibrate_training.html", ctx)


@router.get("/calibrate/card", response_class=HTMLResponse)
async def calibrate_card(
    request: Request, since: str, skip: str = "", change: int | None = None, all: int = 0
):
    """The rating card for the session: the next role, or one earlier role to change. Cancel on a
    change, Undo and a finished save all come back here."""
    async with sessionmaker()() as s:
        ctx = await _page_ctx(request, s, since=since, skip=skip, change=change, full=False)
        # Change and Cancel swap the card alone, so the history keeps its page; an Undo (all=1)
        # also refreshes the strips and the history.
        return await _swap_response(request, s, ctx, all_parts=bool(all))


@router.get("/calibrate/reveal", response_class=HTMLResponse)
async def calibrate_reveal(request: Request, opportunity_id: int, score: int):
    """Kestrel's score, shown only after the operator has answered (it never travels with the
    card, so it cannot anchor the answer). Writes nothing."""
    if score not in cv.SCORE_WORDS:
        return JSONResponse({"error": "score must be 1 to 5"}, status_code=400)
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opportunity_id)
        if opp is None or opp.track != active_track(request):
            return JSONResponse({"error": "opportunity not found"}, status_code=404)
        ev = await _latest_eval(s, opportunity_id)
        dims = cv.dim_scores(ev, opp.track)
        ctx = {
            "said": score,
            "said_word": cv.SCORE_WORDS[score],
            "dims": dims,
            "cv": cv,
            "kestrel": ev.score if ev else None,
            "read": cv.kestrel_read(ev, dims),
            "opp": opp,
            "reasons": cv.REASONS.get(opp.track, cv.REASONS["employment"]),
        }
    return templates().TemplateResponse(request, "partials/calibrate_reveal.html", ctx)


@router.post("/calibrate/rate", response_class=HTMLResponse)
async def calibrate_rate(request: Request):
    """Save a rating: a `label` (source active_learning, or manual for a change to an earlier
    one) with the score 1 to 5, optional reasons as the rationale and optional score nudges as
    dimensions. The verdict is derived from the score with the operator's cutoffs."""
    form = await request.form()
    try:
        opp_id, score = int(form.get("opportunity_id")), int(form.get("score"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "opportunity_id and score are required"}, status_code=400)
    if score not in cv.SCORE_WORDS:
        return JSONResponse({"error": "score must be 1 to 5"}, status_code=400)
    since, skip = str(form.get("since") or ""), str(form.get("skip") or "")
    change = bool(form.get("change"))
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        opp = await s.get(Opportunity, opp_id)
        if opp is None or opp.track != active_track(request):
            return JSONResponse({"error": "opportunity not found"}, status_code=404)
        allowed = cv.REASONS.get(opp.track, cv.REASONS["employment"])
        reasons = [r for r in form.getlist("reason") if r in allowed]
        ev = await _latest_eval(s, opp_id)
        nudged = {}
        for d in cv.dim_scores(ev, opp.track):
            try:
                step = int(form.get(f"nudge_{d['key']}") or 0)
            except ValueError:
                step = 0
            if step:
                nudged[d["key"]] = {"score": max(1, min(5, d["score"] + step)), "why": d["why"]}
        dims = {**dict(ev.dimensions or {}), **nudged} if nudged and ev is not None else None
        row = await record_label(
            s,
            opportunity_id=opp_id,
            track=opp.track,
            score=float(score),
            verdict=verdict_from_score(float(score), opp.track, settings),
            dimensions=dims,
            rationale=("Reasons: " + ", ".join(reasons) + ".") if reasons else None,
            source="manual" if change else "active_learning",
        )
        close = cv.is_close(ev.score if ev else None, float(score))
        ctx = await _page_ctx(request, s, since=since, skip=skip, full=False)
        strips = ctx["strips"]
        if change:
            msg = "Rating changed. Kestrel uses the new one from now on."
        elif strips.n == strips.tune_at:
            msg = f"Saved. That was rating {strips.tune_at}, so a weight tune-up is ready."
        else:
            msg = (
                "Saved. You and Kestrel were close."
                if close
                else (f"Saved. You said {cv.SCORE_WORDS[score]}.")
            )
            if strips.n < strips.tune_at:
                msg += f" {strips.tune_at - strips.n} to the tune-up."
        return await _swap_response(
            request,
            s,
            ctx,
            events=[
                {
                    "k-toast": _toast(
                        msg,
                        undo_url=f"/calibrate/undo/{row.id}",
                        refresh=f"/calibrate/card?since={quote(since, safe='')}&skip={skip}&all=1",
                    )
                }
            ],
        )


@router.post("/calibrate/undo/{label_id}")
async def calibrate_undo(label_id: int):
    """Undo of a save: the rating goes, the role and its evaluations stay."""
    async with sessionmaker()() as s:
        ok = await delete_label(s, label_id)
    return JSONResponse(
        {"undone": ok, "message": "Undone." if ok else "This can no longer be undone."},
        status_code=200 if ok else 404,
    )


@router.post("/calibrate/skip", response_class=HTMLResponse)
async def calibrate_skip(request: Request):
    """Skip: the role moves to the end of this session's queue. Nothing is stored; the skipped
    ids travel with the card, so a new visit starts fresh."""
    form = await request.form()
    since, prior = str(form.get("since") or ""), str(form.get("skip") or "")
    try:
        opp_id = int(form.get("opportunity_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "opportunity_id required"}, status_code=400)
    prior = ",".join(str(i) for i in cv._ids(prior))  # digits only, also in the Undo URL
    skip = ",".join([x for x in prior.split(",") if x and x != str(opp_id)] + [str(opp_id)])
    async with sessionmaker()() as s:
        ctx = await _page_ctx(request, s, since=since, skip=skip, full=False)
        return await _swap_response(
            request,
            s,
            ctx,
            events=[
                {
                    "k-toast": _toast(
                        "Skipped. It moves to the end of the queue.",
                        undo_url="/calibrate/skip/undo",
                        refresh=f"/calibrate/card?since={quote(since, safe='')}&skip={prior}&all=1",
                    )
                }
            ],
        )


@router.post("/calibrate/skip/undo")
async def calibrate_skip_undo():
    """Nothing to undo on the server (a skip is not stored); calibrate.js reloads the card."""
    return JSONResponse({"undone": True})


@router.post("/calibrate/tune/{cal_id}/{decision}", response_class=HTMLResponse)
async def calibrate_tune(request: Request, cal_id: int, decision: str):
    """Use the new weights, or keep the current ones. The operator's own choice, never silent."""
    if decision not in ("accept", "reject"):
        return JSONResponse({"error": "decision must be accept or reject"}, status_code=400)
    form = await request.form()
    since, skip = str(form.get("since") or ""), str(form.get("skip") or "")
    async with sessionmaker()() as s:
        decide = calib.accept_calibration if decision == "accept" else calib.reject_calibration
        cal = await s.get(Calibration, cal_id)
        if cal is None or cal.track != active_track(request) or not await decide(s, cal_id):
            return JSONResponse({"error": "no such proposal waiting"}, status_code=404)
        ctx = await _page_ctx(request, s, since=since, skip=skip, full=False)
        msg = (
            "New weights in use from the next scoring run."
            if decision == "accept"
            else "Kept your current weights. Kestrel will check again after more ratings."
        )
        since_q, skip_q = quote(since, safe=""), quote(skip, safe=",")
        return await _swap_response(
            request,
            s,
            ctx,
            events=[
                {
                    "k-toast": _toast(
                        msg,
                        undo_url=f"/calibrate/tune-undo/{cal_id}",
                        refresh=f"/calibrate/card?since={since_q}&skip={skip_q}&all=1",
                    )
                }
            ],
        )


@router.post("/calibrate/tune-undo/{cal_id}")
async def calibrate_tune_undo(request: Request, cal_id: int):
    """Undo of a tune-up choice: the proposal waits again and, if the new weights had been taken,
    the earlier ones are back in use."""
    async with sessionmaker()() as s:
        cal = await s.get(Calibration, cal_id)
        if cal is None or cal.track != active_track(request):
            return JSONResponse({"undone": False}, status_code=404)
        ok = await calib.undo_calibration(s, cal_id)
    return JSONResponse(
        {"undone": ok, "message": "Undone." if ok else "This can no longer be undone."},
        status_code=200 if ok else 404,
    )


@router.get("/calibrate/history", response_class=HTMLResponse)
async def calibrate_history(request: Request, since: str = "", skip: str = "", page: int = 0):
    track = active_track(request)
    async with sessionmaker()() as s:
        hist = await cv.build_history(s, track, page=page)
    return templates().TemplateResponse(
        request,
        "partials/calibrate_history.html",
        {"history": hist, "cv": cv, "track": track, "since": since, "skip": skip},
    )


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
                "opportunity_id": u.opportunity.id,
                "title": u.opportunity.title,
                "score": u.evaluation.score,
                "weighted": u.evaluation.weighted_score,
                "verdict": u.evaluation.verdict,
                "uncertainty": u.uncertainty,
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
            f"{' ' + format(row.score, '.1f') if row.score is not None else ''})</span>"
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
        "proposed": True,
        "id": cal.id,
        "track": cal.track,
        "n_samples": cal.n_samples,
        "previous_weights": cal.previous_weights,
        "proposed_weights": cal.proposed_weights,
        "cv_mae_previous": cal.cv_mae_previous,
        "cv_mae_proposed": cal.cv_mae_proposed,
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
        "track": result.track,
        "n_labeled": result.n_labeled,
        "promoted": result.promoted,
        "criteria": result.criteria,
        "metrics": result.metrics,
        "reason": result.reason,
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
            s, track, embed_model=current_model(settings)
        )
    if loaded is None:
        return {"active": False}
    m = loaded.ml_model
    return {
        "active": True,
        "version": m.version,
        "n_samples": m.n_samples,
        "metrics": m.metrics,
        "sklearn_version": m.sklearn_version,
    }


@router.get("/api/learning/outcomes")
async def api_outcomes(request: Request):
    track = active_track(request)
    async with sessionmaker()() as s:
        report = await outcomes_mod.analyze_outcomes(s, track)
    return {
        "track": report.track,
        "n_outcomes": report.n_outcomes,
        "discriminating": report.discriminating,
        "headline": report.headline,
        "by_score_band": [
            {
                "label": b.label,
                "n": b.n,
                "responded": b.responded,
                "rate": b.rate,
                "insufficient": b.insufficient,
            }
            for b in report.by_score_band
        ],
        "suggested_ghosting_days": report.suggested_ghosting_days,
    }
