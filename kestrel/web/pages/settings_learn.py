"""Settings > Scoring and learning (D-UI-108, SPEC-INTERFACES 2.6 item 3).

  GET  /settings/scoring/weights?track=          What matters most, and Kestrel's suggestion
  POST /settings/scoring/suggestion/{id}/{do}    accept, or later (Not now)
  POST /settings/scoring/suggestion/{id}/undo    take either back
  POST /settings/scoring/weights/restore         Restore original weights
  POST /settings/scoring/weights/restore/undo
  GET  /settings/scoring/learned?track=          What Kestrel has learned (three switches)
  POST /settings/scoring/learn                   one switch
  GET  /settings/scoring/export                  Export my ratings (JSON file)
  POST /settings/scoring/forget                  Forget my ratings (needs RESET)

The weight bars are read only: there are no sliders. A suggested change is fitted from the
operator's ratings and tested by replaying them first (kestrel.learning.calibration); it appears
only when it matches the operator's calls better. All learning uses only the operator's ratings
and stays on this computer. Forgetting never touches roles, scores, applications or documents."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from kestrel.config import ConsultingWeights, EmploymentWeights
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Calibration, Label, Setting, VerdictOverride
from kestrel.learning import calibration as calib
from kestrel.learning import ledger
from kestrel.services import tracks as tracks_svc
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import settings_io as io
from kestrel.web import settings_view as sv
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.pages.settings_roles import trigger
from kestrel.web.redact import confidential

log = structlog.get_logger()
router = APIRouter()

RESET_WORD = "RESET"
LEARN_KEYS = {
    "use_examples": ("Uses your ratings as examples", "few_shot", 15),
    "tune_weights": ("Tunes to your taste", "calibration", 30),
    "build_model": ("Your own sorting model", "reranker", 200),
}
LEARN_MSG = {
    "use_examples": ("Kestrel will use your ratings as examples.",
                     "Kestrel will stop using your ratings as examples."),
    "tune_weights": ("Kestrel will tune to your taste.",
                     "Kestrel will stop tuning to your taste."),
    "build_model": ("Kestrel will build your own sorting model.",
                    "Kestrel will not build your own sorting model."),
}


def _track(request: Request) -> str:
    asked = request.query_params.get("track")
    return asked if asked in sv.TRACKS else active_track(request)


def _defaults(track: str) -> dict[str, float]:
    return (ConsultingWeights() if track == "consulting" else EmploymentWeights()).model_dump()


def _day(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).date()
    except ValueError:
        return ""
    from kestrel.date_display import format_display

    return format_display(d, "region")


# ------------------------------------------------------------------------------- weights
async def weights_ctx(request: Request, track: str, *, note: str | None = None) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        enabled = tuple(await tracks_svc.load_enabled(s))
        current = getattr(settings.scoring.weights, track).model_dump()
        overrides = (
            await s.execute(select(Setting.key).where(
                Setting.key.like(f"scoring.weights.{track}.%")))
        ).scalars().all()
        proposal = (
            await s.execute(
                select(Calibration)
                .where(Calibration.track == track, Calibration.status == "proposed")
                .order_by(Calibration.id.desc()).limit(1)
            )
        ).scalars().first()
        accepted = (
            await s.execute(
                select(Calibration)
                .where(Calibration.track == track, Calibration.status == "accepted")
                .order_by(Calibration.decided_at.desc()).limit(1)
            )
        ).scalars().first()
    defaults = _defaults(track)
    labels = sv.WEIGHT_LABELS[track]
    bars = [{"key": k, "label": lab, "pct": sv.pct(current.get(k, 0.0)),
             "default": sv.pct(defaults.get(k, 0.0))} for k, lab in labels]
    sug = None
    # Only a change that matches the operator's calls better is ever offered (the replay).
    if (
        proposal is not None
        and proposal.n_samples >= ledger.INSUFFICIENT_N
        and (proposal.cv_mae_proposed or 0) < (proposal.cv_mae_previous or 0)
    ):
        pw = proposal.proposed_weights or {}
        rows = []
        for b in bars:
            new = sv.pct(pw.get(b["key"], current.get(b["key"], 0.0)))
            if new != b["pct"]:
                rows.append({"label": b["label"], "now": b["pct"], "new": new})
        if rows:
            sug = {"id": proposal.id, "n": proposal.n_samples, "rows": rows,
                   "mae_was": proposal.cv_mae_previous, "mae_now": proposal.cv_mae_proposed}
    return {
        "track": track, "tracks": enabled, "bars": bars, "suggestion": sug,
        "updated_on": _day(accepted.decided_at) if (accepted and overrides) else "",
        "weights_note": note, "keys": [(b["key"], b["label"]) for b in bars],
        "is_default": not overrides,
    }


def _w_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_weights.html", ctx,
                                        headers=headers or {})


@router.get("/settings/scoring/weights", response_class=HTMLResponse)
async def weights_card(request: Request):
    return _w_card(request, await weights_ctx(request, _track(request)))


def _refresh(track: str) -> dict:
    return {"refresh": f"/settings/scoring/weights?track={track}", "target": "#st-wts"}


@router.post("/settings/scoring/suggestion/{cal_id}/{do}", response_class=HTMLResponse)
async def suggestion(request: Request, cal_id: int, do: str):
    if do == "undo":  # the same path shape: take the choice back
        return await suggestion_undo(cal_id)
    if do not in ("accept", "later"):
        return HTMLResponse("Unknown action.", status_code=404)
    async with sessionmaker()() as s:
        cal = await s.get(Calibration, cal_id)
        if cal is None or cal.status != "proposed":
            track = cal.track if cal is not None else _track(request)
            return _w_card(request, await weights_ctx(request, track))
        track = cal.track
        done = (await calib.accept_calibration(s, cal_id) if do == "accept"
                else await calib.reject_calibration(s, cal_id))
    if not done:
        return _w_card(request, await weights_ctx(request, track))
    msg = ("Weights updated. Scores use them from your next run." if do == "accept"
           else "Okay. Kestrel will suggest again after you rate more roles.")
    undo = f"/settings/scoring/suggestion/{cal_id}/undo"
    return _w_card(request, await weights_ctx(request, track),
                   trigger(msg, undo=undo, refresh=_refresh(track)))


@router.post("/settings/scoring/suggestion/{cal_id}/undo")
async def suggestion_undo(cal_id: int):
    """Put the suggestion back. An accepted one only while the weights are still the ones it
    wrote; a later change of the weights is never overwritten."""
    async with sessionmaker()() as s:
        cal = await s.get(Calibration, cal_id)
        if cal is None or cal.status not in ("accepted", "rejected"):
            return io.refuse(io.NO_UNDO)
        if cal.status == "accepted":
            for key, value in (cal.proposed_weights or {}).items():
                row = await s.get(Setting, f"scoring.weights.{cal.track}.{key}")
                if row is None or row.value != value:
                    return io.refuse(io.NO_UNDO)
        if not await calib.undo_calibration(s, cal_id):
            return io.refuse(io.NO_UNDO)
    return io.reply("Put back.")


@router.post("/settings/scoring/weights/restore", response_class=HTMLResponse)
async def restore_weights(request: Request):
    form = await request.form()
    track = str(form.get("track") or "")
    track = track if track in sv.TRACKS else _track(request)
    prefix = f"scoring.weights.{track}."
    async with sessionmaker()() as s:
        rows = (await s.execute(select(Setting).where(Setting.key.like(prefix + "%")))).scalars()
        saved = {r.key[len(prefix):]: r.value for r in rows}
        if not saved:
            return _w_card(request, await weights_ctx(request, track),
                           trigger("You are already using the original weights."))
        for key in saved:
            row = await s.get(Setting, prefix + key)
            if row is not None:
                await s.delete(row)
        await s.commit()
    undo = "/settings/scoring/weights/restore/undo?" + urlencode(
        {"track": track, "rows": json.dumps(saved, sort_keys=True)})
    return _w_card(request, await weights_ctx(request, track),
                   trigger("Original weights restored.", undo=undo, refresh=_refresh(track)))


@router.post("/settings/scoring/weights/restore/undo")
async def restore_undo(request: Request):
    q = request.query_params
    track = q.get("track") or ""
    try:
        saved = json.loads(q.get("rows") or "{}")
    except ValueError:
        return io.refuse(io.NO_UNDO)
    if track not in sv.TRACKS or not isinstance(saved, dict) or not saved:
        return io.refuse(io.NO_UNDO)
    prefix = f"scoring.weights.{track}."
    allowed = set(_defaults(track))
    async with sessionmaker()() as s:
        for key, value in saved.items():
            if key not in allowed or not io.plausible(prefix + key, value) \
                    or value is io.ABSENT:
                return io.refuse(io.NO_UNDO)
            if await s.get(Setting, prefix + key) is not None:
                return io.refuse(io.NO_UNDO)  # changed since: never overwritten
        for key, value in saved.items():
            s.add(Setting(key=prefix + key, value=value))
        await s.commit()
    return io.reply("Put back.")


# ------------------------------------------------------------------------------- learning
async def learned_ctx(request: Request, track: str) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        enabled = tuple(await tracks_svc.load_enabled(s))
        summ = await ledger.track_summary(s, track, settings)
    floors = {f.key: f for f in summ.floors}
    rows = []
    for key, (label, fkey, _need) in LEARN_KEYS.items():
        f = floors[fkey]
        on_switch = bool(getattr(settings.learning, key))
        ready = f.n >= f.needed
        if not ready:
            pill, tone = "Not yet", "muted"
        elif not on_switch:
            pill, tone = "Off", "muted"
        elif fkey == "reranker":
            live = bool(summ.reranker.active and summ.reranker.loadable)
            pill, tone = ("In use", "good") if live else ("Ready", "info")
        else:
            pill, tone = "Active", "good"
        rows.append({
            "key": key, "label": label, "n": f.n, "needed": f.needed, "shown": min(f.n, f.needed),
            "ready": ready, "on": ready and on_switch, "switch": on_switch,
            "pill": pill, "tone": tone,
            "pct": min(100, int(100 * f.n / f.needed)) if f.needed else 100,
            "note": f.note, "fkey": fkey,
        })
    rk = summ.reranker
    model_note = (
        f"In use since {_day(rk.trained_at)}. Checked after every run."
        if rows[2]["on"] and rk.active and rk.loadable
        else "Once ready, Kestrel only uses it if it beats the built-in scoring on roles you've "
             "rated, and stops using it if it starts to slip."
    )
    ag = summ.agreement
    return {
        "track": track, "tracks": enabled, "learn_rows": rows, "model_note": model_note,
        "agree_n": ag.n, "agree_insufficient": ag.insufficient,
        "agree_pct": None if ag.rate is None else int(round(ag.rate * 100)),
        "n_total": summ.n_total, "min_n": ledger.INSUFFICIENT_N,
    }


def _l_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_learned.html", ctx,
                                        headers=headers or {})


@router.get("/settings/scoring/learned", response_class=HTMLResponse)
async def learned_card(request: Request):
    return _l_card(request, await learned_ctx(request, _track(request)))


@router.post("/settings/scoring/learn", response_class=HTMLResponse)
async def learn_switch(request: Request):
    form = await request.form()
    key = str(form.get("key") or "")
    track = str(form.get("track") or "")
    track = track if track in sv.TRACKS else _track(request)
    if key not in LEARN_KEYS:
        return HTMLResponse("That switch is not one Kestrel has.", status_code=404)
    on = str(form.get("on") or "") in ("1", "true", "on")
    row_key = f"learning.{key}"
    async with sessionmaker()() as s:
        prev = await io.read(s, row_key)
        await io.write(s, row_key, on)
        await s.commit()
    refresh = {"refresh": f"/settings/scoring/learned?track={track}", "target": "#st-learn"}
    msg = LEARN_MSG[key][0 if on else 1]
    return _l_card(request, await learned_ctx(request, track),
                   trigger(msg, undo=io.undo_url(row_key, prev, on), refresh=refresh))


# ----------------------------------------------------------------------------- ratings
@router.get("/settings/scoring/export")
async def export_ratings():
    """Every rating as a JSON file. With Confidential mode on the employer name is replaced the
    same way every other output replaces it, so the export is redacted."""
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        redact = bool(settings.confidential_mode)
        if redact and not (await confidential(s))[1]:
            return Response(
                content=("Confidential mode is on but no employer is set, so nothing could be "
                         "hidden. Add your current employer in Profile, then export again."),
                media_type="text/plain", status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        rows = await ledger.export_labels(s, redact=redact)
    body = json.dumps(
        {"exported_at": utcnow_iso(), "n": len(rows), "redacted": redact, "ratings": rows},
        indent=2,
    )
    name = f"kestrel-ratings-{datetime.now(UTC).date().isoformat()}.json"
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "no-store"},
    )


@router.post("/settings/scoring/forget", response_class=HTMLResponse)
async def forget(request: Request):
    """Forget what the operator chose: their ratings, their own sorting model, suggested weight
    changes. Permanent, refused unless RESET was typed and something was ticked. Never touches
    roles, scores, applications or documents, and weights already accepted stay."""
    form = await request.form()
    chosen = {
        "ratings": "on" in form.getlist("ratings"),
        "model": "on" in form.getlist("model"),
        "weights": "on" in form.getlist("weights"),
    }
    if str(form.get("confirm") or "").strip() != RESET_WORD:
        return HTMLResponse(
            f'<p class="st-bad" role="alert">Nothing was forgotten. Type {RESET_WORD} in '
            "capitals to continue.</p>")
    if not any(chosen.values()):
        return HTMLResponse('<p class="st-bad" role="alert">Nothing was forgotten. Tick at least '
                            "one thing.</p>")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        n_ratings = n_model = n_sugg = 0
        if chosen["ratings"]:
            for model in (Label, VerdictOverride):
                rows = list((await s.execute(select(model))).scalars())
                n_ratings += len(rows)
                for row in rows:
                    await s.delete(row)
            await s.commit()
        if chosen["model"]:
            res = await ledger.reset_learning(s, settings, labels=False, reranker=True,
                                              calibrations=False)
            n_model = res["ml_models_deleted"]
        if chosen["weights"]:
            props = list((await s.execute(
                select(Calibration).where(Calibration.status == "proposed"))).scalars())
            for cal in props:
                cal.status = "rejected"
                cal.decided_at = utcnow_iso()
            n_sugg = len(props)
            await s.commit()
    parts = [w for w, on in (("your ratings", chosen["ratings"]),
                             ("your own sorting model", chosen["model"]),
                             ("suggested weight changes", chosen["weights"])) if on]
    said = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    msg = (f"Forgot {said}. Roles, scores, applications and documents are kept.")
    log.info("ratings_forgotten", ratings=n_ratings, model=n_model, suggestions=n_sugg)
    return HTMLResponse(
        '<p class="st-ok" role="status" data-testid="forget-done">' + msg + "</p>",
        headers=trigger(msg, refresh={"refresh": "/settings/section/scoring",
                                      "target": "#st-pane-scoring"}),
    )


async def scoring_pane_ctx(request: Request) -> dict:
    track = _track(request)
    ctx = await weights_ctx(request, track)
    ctx.update(await learned_ctx(request, track))
    ctx["reset_word"] = RESET_WORD
    return ctx
