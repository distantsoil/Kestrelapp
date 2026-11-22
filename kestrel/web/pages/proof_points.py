"""Profile > Documents: the proof points card (issue #208 item 6, D-SCORE-26, D-SCORE-27).

GET  /proof-points?track=            the card: counts, warnings, the narrative, the Refresh button
GET  /proof-points/refresh?track=    the reviewed diff against the current resume
GET  /proof-points/cancel            Cancel on the diff (empty reply; the panel deletes itself)
POST /proof-points/apply             apply the ticked rows (form: track, row) and re-render
POST /proof-points/narrative         save the narrative (form: track, narrative)

INVARIANT (hard rule 2): a claim written here is one of the operator's own resume bullets byte for
byte, and only after the operator ticks it and presses Apply. The form carries row ids, never text;
the plan is recomputed from the database and profile.yml at apply time. Nothing is sent anywhere.
Paths sit outside /profile/ so POST /profile/{track}/{section} never takes them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kestrel.services import profile_edit as pe
from kestrel.services import proof_refresh as pr
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()
router = APIRouter()

NARRATIVE_MAX = 2000


def _track(value: Any) -> str:
    return value if value in pe.TRACKS else "employment"


def _block(track: str) -> dict:
    state = pe.load_state()
    return dict(state.tracks.get(track) or {}) if not state.problem else {}


def _points(block: dict) -> list[dict]:
    return [dict(p) for p in (block.get("proof_points") or []) if isinstance(p, dict)]


async def proof_ctx(track: str) -> dict:
    """The card's context: the points, the narrative and the warnings for this track."""
    block = await asyncio.to_thread(_block, track)
    points = _points(block)
    narrative = str(block.get("narrative") or "")
    return {"pp_track": track, "pp_count": len(points), "pp_narrative": narrative,
            "pp_warnings": pr.warnings(points, narrative)}


async def _card(request: Request, track: str, **extra) -> HTMLResponse:
    ctx = {**await proof_ctx(track), **extra}
    return templates().TemplateResponse(request, "partials/proof_points_card.html", ctx)


@router.get("/proof-points", response_class=HTMLResponse)
async def card(request: Request):
    return await _card(request, _track(request.query_params.get("track")))


async def _plan(track: str) -> tuple[list[dict], pr.Plan]:
    block = await asyncio.to_thread(_block, track)
    points = _points(block)
    async with sessionmaker()() as s:
        bullets = await pr.resume_bullets(s, track)
    return points, pr.plan_refresh(points, bullets)


@router.get("/proof-points/refresh", response_class=HTMLResponse)
async def refresh(request: Request):
    track = _track(request.query_params.get("track"))
    _points_now, plan = await _plan(track)
    return templates().TemplateResponse(request, "partials/proof_points_diff.html",
                                        {"pp_track": track, "plan": plan})


@router.get("/proof-points/cancel", response_class=HTMLResponse)
async def cancel() -> HTMLResponse:
    """Cancel on the reviewed diff: nothing to do on the server. The button deletes its own
    panel (hx-swap delete) after the leave animation; no inline script (CSP, #212)."""
    return HTMLResponse("")


@router.post("/proof-points/apply", response_class=HTMLResponse)
async def apply(request: Request):
    form = await request.form()
    track = _track(form.get("track"))
    approved = {str(v) for v in form.getlist("row")}
    points, plan = await _plan(track)
    new, applied = pr.apply_rows(points, plan, approved)
    if not applied:
        return await _card(request, track, pp_note="Nothing was checked, so nothing changed.")
    try:
        await asyncio.to_thread(pe.save_section, track, "proof_points", {"proof_points": new})
    except pe.ProfileEditError as exc:
        return await _card(request, track, pp_problem=plain_error(exc))
    from kestrel.services.profile_readiness import mark_proof_points_changed

    async with sessionmaker()() as s:
        # Roles scored before this change can then offer Score again (profile_readiness).
        await mark_proof_points_changed(s, track)
        await s.commit()
    log.info("proof_points_refreshed", track=track, applied=applied)
    resp = await _card(request, track, pp_note=(
        f"Saved {applied} change{'' if applied == 1 else 's'} to your proof points."))
    resp.headers["HX-Trigger"] = json.dumps({"pf-saved": {
        "message": "Proof points updated.", "undoUrl": "/profile/undo", "token": pe.undo_token()}})
    return resp


@router.post("/proof-points/narrative", response_class=HTMLResponse)
async def narrative(request: Request):
    form = await request.form()
    track = _track(form.get("track"))
    text = str(form.get("narrative") or "").strip()[:NARRATIVE_MAX]
    try:
        await asyncio.to_thread(pe.save_section, track, "narrative", {"narrative": text})
    except pe.ProfileEditError as exc:
        return await _card(request, track, pp_problem=plain_error(exc))
    from kestrel.services.profile_readiness import mark_proof_points_changed

    async with sessionmaker()() as s:
        await mark_proof_points_changed(s, track)  # scoring reads it: offer Score again
        await s.commit()
    resp = await _card(request, track, pp_note="Narrative saved.")
    resp.headers["HX-Trigger"] = json.dumps({"pf-saved": {
        "message": "Narrative saved.", "undoUrl": "/profile/undo", "token": pe.undo_token()}})
    return resp
