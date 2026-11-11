"""The quick round's routes (issue 171, D-UI-272, D-LEARN-2): the round, its answers, its done
screen, the Today strip's Not now choices and the Calibrate offer settings.

Invariants, each pinned by tests/test_quick_round_routes.py:
- An answer only writes a local `label` row. No model is called and nothing leaves this computer.
- "Not sure" writes nothing.
- Kestrel's score is not read for the round and is never in its markup.
- A role from another track is refused (404), like Calibrate's own rating route."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Opportunity
from kestrel.learning import quick
from kestrel.learning.ledger import delete_label
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import quick_view as qv
from kestrel.web.db_retry import write_with_retry
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.words import words

log = structlog.get_logger()

router = APIRouter()


def _ids(raw: str | None, *, cap: int = 2000) -> list[int]:
    """Digits only, no repeats: the roles already shown, carried in a query string."""
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit() and len(part) < 12 and int(part) not in out:
            out.append(int(part))
        if len(out) >= cap:
            break
    return out


def _count(raw: str | None) -> int:
    return int(raw) if raw and raw.isdigit() and len(raw) < 6 else 0


def _toast(message: str, *, undo_url: str | None = None) -> dict[str, str]:
    body = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    return {"HX-Trigger": json.dumps({"k-toast": body}, ensure_ascii=True)}


# --------------------------------------------------------------------------------- the round
@router.get("/calibrate/quick", response_class=HTMLResponse)
async def round_page(request: Request):
    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        items = await quick.round_items(s, track, settings, seen=[])
        ctx = {
            "track": track,
            "query": {"track": track},
            "cards": await qv.build_cards(s, items),
            "size": quick.ROUND_SIZE,
            "w": words(track),
        }
    return templates().TemplateResponse(request, "quick_round.html", ctx)


@router.get("/calibrate/quick/cards", response_class=HTMLResponse)
async def more_cards(request: Request, seen: str = ""):
    """"Keep going: 10 more": the next roles, never one already shown in this round."""
    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        items = await quick.round_items(s, track, settings, seen=_ids(seen))
        ctx = {"cards": await qv.build_cards(s, items), "track": track}
    return templates().TemplateResponse(request, "partials/quick_cards.html", ctx)


@router.post("/calibrate/quick/answer")
async def answer(request: Request):
    """Save one answer at once. Not sure answers 200 and stores nothing."""
    form = await request.form()
    word = str(form.get("answer") or "")
    try:
        opp_id = int(str(form.get("opportunity_id")))
    except ValueError:
        return JSONResponse({"error": "opportunity_id and answer are required"}, status_code=400)
    if word not in quick.ANSWERS:
        return JSONResponse({"error": "answer must be top, yes, no or unsure"}, status_code=400)
    track = active_track(request)
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None or opp.track != track:
            return JSONResponse({"error": "opportunity not found"}, status_code=404)
        settings = await load_settings_with_db(s)

    async def _save(ws: AsyncSession):
        # The label and the offer bookkeeping are one transaction: either both commit or
        # neither does, so "not saved" on the client always means no label in the database
        # and a retry can never write a second one. Answering is taking the offer: the strip's
        # next ask follows the chosen cadence (mark_taken commits both).
        before = await quick.read_state(ws, track)
        row = await quick.save_answer(
            ws, opportunity_id=opp_id, track=track, answer=word, settings=settings,
            commit=False)
        if row is None:
            return None, before
        label_id = row.id
        await quick.mark_taken(ws, track)
        return label_id, before

    # The save waits out a worker's write in a fresh session instead of failing (D-DB-1).
    label_id, before = await write_with_retry(_save, path="/calibrate/quick/answer")
    async with sessionmaker()() as s:
        total = await quick.answer_count(s, track)
    # `offer` is the offer as it was before this answer, so an Undo of a round's only answer
    # can put it back (the strip is not left snoozed by an answer that no longer exists).
    return {"saved": label_id is not None, "label_id": label_id, "answers": total,
            "offer": before.as_params() if label_id is not None else None}


def _offer_from(enabled: str, runs: str, at: str) -> quick.OfferState | None:
    """The offer state an Undo carries back, or None when a value is malformed."""
    if enabled not in ("0", "1") or not runs.isdigit() or len(runs) > 6:
        return None
    if at:
        try:
            datetime.fromisoformat(at)
        except ValueError:
            return None
    return quick.OfferState(enabled == "1", int(runs), at or None)


@router.post("/calibrate/quick/undo/{label_id}")
async def undo(label_id: int, request: Request):
    """Undo the last saved answer: the same deletion as Calibrate's Undo, limited to a Quick
    label so a stale press can never remove a Detailed rating. When the client sends the offer
    as it was before the round (the Undo of the round's only remaining answer), it is put back
    in the same transaction, so the strip is not left snoozed by an answer that is gone."""
    from kestrel.db.models import Label

    form = await request.form()
    before = None
    if form.get("runs") is not None:
        before = _offer_from(str(form.get("enabled") or ""), str(form.get("runs") or ""),
                             str(form.get("at") or ""))
        if before is None:
            return JSONResponse({"undone": False}, status_code=400)
    track = active_track(request)
    async with sessionmaker()() as s:
        row = await s.get(Label, label_id)
        if row is None or row.source != quick.SOURCE or row.track != track:
            return JSONResponse({"undone": False}, status_code=404)

    async def _do(ws: AsyncSession) -> bool:
        if before is not None:
            await quick.write_state(ws, track, before, commit=False)
        return await delete_label(ws, label_id)  # commits both, or neither

    ok = await write_with_retry(_do, path="/calibrate/quick/undo")
    async with sessionmaker()() as s:
        total = await quick.answer_count(s, track)
    return JSONResponse({"undone": ok, "answers": total}, status_code=200 if ok else 404)


@router.get("/calibrate/quick/done", response_class=HTMLResponse)
async def done_screen(
    request: Request, yes: str = "", top: str = "", no: str = "", unsure: str = "",
    seen: str = "",
):
    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        total = await quick.answer_count(s, track)
        more = await quick.round_items(s, track, settings, seen=_ids(seen))
        done = qv.build_done(
            total=total, yes=_count(yes), top=_count(top), no=_count(no), unsure=_count(unsure),
            remaining=len(more), settings=settings,
            examples=await quick.examples_count(s, track, settings),
        )
    return templates().TemplateResponse(
        request, "partials/quick_done.html", {"done": done, "track": track, "w": words(track)})


# ------------------------------------------------------------------------------- the offer
async def offer_context(
    s: AsyncSession, track: str, settings: Settings, *, busy: bool, tour: bool = False
) -> dict | None:
    """What Today's strip needs, or None when it does not show: switched off, snoozed, no run
    finished yet, a run in progress, the walkthrough, or fewer than five roles to sort."""
    if busy or tour or not await quick.is_due(s, track):
        return None
    items = await quick.round_items(s, track, settings, seen=[])
    if len(items) < quick.MIN_OFFER:
        return None
    return {
        "n": len(items),
        "meter": quick.meter_for(await quick.answer_count(s, track), settings,
                                 await quick.examples_count(s, track, settings)),
    }


@router.get("/today/quick", response_class=HTMLResponse)
async def today_quick(request: Request):
    """The strip alone, asked again after a run ends, an Undo or a poll."""
    from kestrel.web.pages.today import run_panel

    track = active_track(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        run = (await run_panel(s, track, settings))["run"]
        ctx = {
            "track": track,
            "w": words(track),
            "quick": await offer_context(
                s, track, settings, busy=bool(run["active"] or run["paused"]),
                tour=request.query_params.get("tour") == "1"),
        }
    return templates().TemplateResponse(request, "partials/td_quick.html", ctx)


def _restore_url(before: quick.OfferState, track: str) -> str:
    return "/today/quick/restore?" + urlencode({**before.as_params(), "track": track})


@router.post("/today/quick/snooze")
async def snooze(request: Request):
    track = active_track(request)
    holder: dict[str, quick.OfferState] = {}

    async def _do(ws: AsyncSession):
        holder["before"] = await quick.read_state(ws, track)
        await quick.snooze(ws, track)

    await write_with_retry(_do, path="/today/quick/snooze")
    return Response(status_code=204, headers=_toast(
        "Okay. Kestrel will ask again after 3 more runs. The roles wait in Calibrate.",
        undo_url=_restore_url(holder["before"], track)))


@router.post("/today/quick/stop")
async def stop(request: Request):
    track = active_track(request)
    holder: dict[str, quick.OfferState] = {}

    async def _do(ws: AsyncSession):
        holder["before"] = await quick.read_state(ws, track)
        await quick.stop_asking(ws, track)

    await write_with_retry(_do, path="/today/quick/stop")
    return Response(status_code=204, headers=_toast(
        "Okay. Kestrel will stop asking. Turn it back on in Calibrate.",
        undo_url=_restore_url(holder["before"], track)))


@router.post("/today/quick/restore")
async def restore(request: Request, enabled: str = "1", runs: str = "1", at: str = "",
                  track: str = ""):
    """Undo of Not now: put the offer back exactly as it was before the press."""
    state = _offer_from(enabled, runs, at)
    if state is None:
        return JSONResponse({"undone": False}, status_code=400)
    want = track if track in ("employment", "consulting") else active_track(request)
    await write_with_retry(lambda ws: quick.write_state(ws, want, state), path="/today/quick")
    return JSONResponse({"undone": True})


@router.post("/calibrate/quick/settings")
async def offer_settings(request: Request):
    """The Calibrate toggle and "How often". One form, saved as it changes."""
    form = await request.form()
    track = active_track(request)
    kind = str(form.get("cadence") or "")
    if kind and kind not in quick.CADENCES:
        return JSONResponse({"error": "unknown cadence"}, status_code=400)

    async def _do(ws: AsyncSession):
        if kind:
            await quick.set_cadence(ws, kind)
        # An unchecked switch is simply absent from the form. Only a real change moves the offer.
        want = bool(form.get("enabled"))
        if (await quick.read_state(ws, track)).enabled != want:
            await quick.set_enabled(ws, track, want)

    await write_with_retry(_do, path="/calibrate/quick/settings")
    msg = ("Saved. Kestrel will offer a quick round after runs." if form.get("enabled")
           else "Saved. Kestrel will not offer a quick round.")
    return Response(status_code=204, headers=_toast(msg))
