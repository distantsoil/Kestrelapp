"""Requirement corrections on the Role page (issue 170, D-UI-290, D-LEARN-3):

  POST /opportunities/{id}/role/correction             create or replace one correction
  POST /opportunities/{id}/role/correction/{cid}/undo  put back exactly what the save replaced,
                                                       or (no token) remove the correction
  GET  /opportunities/{id}/role/similar?hash=          the other open roles that ask for the same
                                                       thing, for the rescore offer
  GET  /opportunities/{id}/role/match                  the match area as it is now (what an Undo
                                                       from the toast reloads)

Invariants (each has a test in tests/test_role_corrections.py):
- Nothing here sends anything online, queues a task or opens a model: a correction is a local row.
  Scoring again always goes through /rescore/choose and its confirmed dialog (D-SEC-7).
- The requirement text, state and basis come from the stored Evaluation, never from the form: the
  form names a row by its hash. Text typed here (a note) is stored on the correction and nowhere
  else; it never reaches a resume or a PDF (hard rule 2).
- A correction never archives or rejects a role and never touches a Tier 0 or Tier 1 result
  (hard rule 4), and never rewrites the Evaluation JSON.
- Undo restores the exact prior rows (`kestrel.learning.corrections.undo_correction`)."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from kestrel.db.models import Opportunity, RequirementCorrection
from kestrel.learning import corrections as corr
from kestrel.match_text import requirement_hash
from kestrel.web import correction_undo as held_undo
from kestrel.web import correction_view as cvw
from kestrel.web import role_view as rv
from kestrel.web.deps import sessionmaker
from kestrel.web.errors import authored_error, plain_error
from kestrel.web.pages import role as rolepage
from kestrel.web.pages.pipeline import _events, _toast
from kestrel.web.queries import latest_evaluation
from kestrel.web.redact import confidential_once, redact_text

log = structlog.get_logger()
router = APIRouter()

REFUSED_TEXT = "This can no longer be undone."
CHANGED_TEXT = "This answer changed since, so it was left as it is."
REFUSED = f'<span data-undo-msg data-ok="0">{REFUSED_TEXT}</span>'
CHANGED = f'<span data-undo-msg data-ok="0">{CHANGED_TEXT}</span>'
GONE = "That role is gone."
NO_ROW = "That is no longer on this posting. Reload the page to see what it asks for now."
NEED_STORY = "Pick the story that shows it, or write a new one."
PICK_ONE = "Pick one of the answers first."
SIMILAR_SHOWN = 8


# The whole list of messages save_correction can raise that a person may act on (the other
# CorrectionError messages name an internal value and show the fallback instead).
_REFUSALS = ("They mean something else needs a note", "the requirement is empty")


def _warn(text: str) -> HTMLResponse:
    events = _events({"k-toast": _toast(text, tone="warn")})
    return HTMLResponse("", headers={"HX-Reswap": "none", **events})


def _match_url(opp_id: int) -> str:
    return f"/opportunities/{opp_id}/role/match"


async def _match_html(request: Request, opp_id: int, *, flash: str | None = None) -> str:
    """The match area (the row lists, the wheel, Evidence) and the Your view strip as an
    out-of-band swap, as the page would render them now. `flash` is the row to ring once."""
    ctx = await rolepage.page_context(request, opp_id, "evaluation")
    if ctx is None:
        return ""
    ctx.update({"swap": True, "flash": flash})
    body = rolepage._render(request, "partials/role_match.html", ctx)
    strip = rolepage._render(request, "partials/role_cx_strip.html", {**ctx, "oob": True})
    return body + strip


async def _store_undo(s, opp_id: int, undo: corr.Undo) -> str:
    """Hold an Undo for a day under a random token (one use), bound to the state the write left
    the rows in (kestrel.web.correction_undo)."""
    return await held_undo.hold(s, undo, opp=opp_id)


def _undo_url(opp_id: int, cid: int, token: str) -> str:
    return f"/opportunities/{opp_id}/role/correction/{cid}/undo?token={token}"


def _raw_rows(opp: Opportunity, ev) -> list[dict[str, Any]]:
    """The rows the stored (unredacted) Evaluation gives, before any correction."""
    return rv.requirements(opp, {"blocks": (ev.blocks if ev is not None else None) or {}})


# ------------------------------------------------------------------------------------- save
@router.post("/opportunities/{opp_id}/role/correction", response_class=HTMLResponse)
async def save(request: Request, opp_id: int):
    """Save one answer about one requirement. The form names the row by its hash (`h`) and gives
    the answer (`reason`), an optional `note` and, for I have all of it or Some of it, the
    `story_id`. A person answer replaces the track's earlier one for the same requirement; They
    mean something else (a note about this posting) replaces this role's earlier note. Answers
    the row is the model's state for come from the stored read, never from the form."""
    form = await request.form()
    h = str(form.get("h") or "")
    reason = str(form.get("reason") or "")
    note = " ".join(str(form.get("note") or "").split())[: cvw.NOTE_MAX]
    raw_story = str(form.get("story_id") or "")
    story_id = int(raw_story) if raw_story.isdigit() else None
    if reason not in cvw.LABELS:
        return _warn(PICK_ONE)
    flash = h[:12]
    try:
        async with sessionmaker()() as s:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _warn(GONE)
            ev = await latest_evaluation(s, opp_id)
            row = next((r for r in _raw_rows(opp, ev) if requirement_hash(r["text"]) == h), None)
            if row is None or ev is None:
                return _warn(NO_ROW)
            if reason == "all" and story_id is None:
                return _warn(NEED_STORY)
            if reason == "some" and story_id is None and not row.get("unverified"):
                return _warn(NEED_STORY)
            stories, variants = await rolepage.stored_sources(s, opp.track)
            hit = (
                rolepage.match_source(row["evidence"], stories, variants)
                if row.get("evidence")
                else None
            )
            basis = "quoted" if hit else ("inferred" if row.get("evidence") else "none")
            before = next(
                (
                    c
                    for c in await corr.corrections_for(s, opp)
                    if c.requirement_hash == h and corr.applies(c)
                ),
                None,
            )
            was = before.operator_state if before is not None else row["state"]
            try:
                saved, undo = await corr.save_correction(
                    s,
                    opportunity_id=opp_id,
                    requirement_text=row["text"],
                    kestrel_state=row["state"],
                    reason=reason,
                    note=note or None,
                    story_id=story_id,
                    basis=basis,
                    evaluation_id=ev.id,
                )
            except corr.CorrectionError as exc:
                log.info("role_correction_refused", opp_id=opp_id, error=str(exc))
                return _warn(
                    "Say what they mean first." if reason == "misread"
                    else authored_error(exc, _REFUSALS, "That could not be saved.")
                )
            token = await _store_undo(s, opp_id, undo)
            new_state = saved.operator_state if corr.applies(saved) else row["state"]
            person = saved.scope == corr.PERSON
    except SQLAlchemyError as exc:  # a write path must never 500 (CLAUDE.md hard rules)
        log.warning("role_correction_failed", opp_id=opp_id, error=str(exc))
        return _warn(f"Could not save that: {plain_error(exc)}")
    toast = _toast(
        cvw.toast_text(new_state != was, new_state),
        _undo_url(opp_id, saved.id, token),
    )
    toast["refresh"] = rolepage._refresh(("#rl-match", _match_url(opp_id), "outerHTML"))
    events: dict[str, Any] = {"k-toast": toast}
    if person:
        events["rl-similar"] = {"hash": h}
    html = await _match_html(request, opp_id, flash=flash)
    return HTMLResponse(html, headers=_events(events))


# -------------------------------------------------------------------------------------- undo
def _refused(request: Request, html: str) -> HTMLResponse:
    """A refused Undo. The toast's Undo (a plain fetch) reads the `data-undo-msg` span; the
    row's own Undo is an htmx post aimed at #rl-match, so it gets a warning toast and no swap,
    never the bare sentence in place of the match area (D-UI-322)."""
    if request.headers.get("HX-Request"):
        return _warn(REFUSED_TEXT if html == REFUSED else CHANGED_TEXT)
    return HTMLResponse(html)


@router.post("/opportunities/{opp_id}/role/correction/{cid}/undo", response_class=HTMLResponse)
async def undo(request: Request, opp_id: int, cid: int, token: str = ""):
    """Undo. With the `token` a save handed out, put back exactly what that save replaced (the
    row, or the rows it replaced), field for field, once. Without one (the row's own Undo),
    remove this role's correction and hand out a token that brings it back. A correction made on
    another role is changed in Profile, not here."""
    token = token or str((await request.form()).get("token") or "")
    removed = False
    try:
        async with sessionmaker()() as s:
            if token:
                held = await held_undo.find(s, token)
                if held is None or held.opp != opp_id or cid not in held.ids:
                    return _refused(request, REFUSED)
                # Refuse, changing nothing, when any row changed since the save (a later answer
                # from another role, another tab or Profile).
                if not await held_undo.still_as_left(s, held):
                    return _refused(request, CHANGED)
                await corr.undo_correction(s, held.undo)
                await s.delete(held.row)
                await s.commit()
            else:
                existing = await s.get(RequirementCorrection, cid)
                if existing is None or existing.opportunity_id != opp_id:
                    return _refused(request, REFUSED)
                held = await corr.remove_correction(s, cid)
                if held is None:
                    return _refused(request, REFUSED)
                new_token = await _store_undo(s, opp_id, held)
                removed = True
    except SQLAlchemyError as exc:
        log.warning("role_correction_undo_failed", opp_id=opp_id, cid=cid, error=str(exc))
        return _warn(f"Could not undo: {plain_error(exc)}")
    events: dict[str, Any] = {}
    if removed:
        toast = _toast(cvw.REMOVED, _undo_url(opp_id, cid, new_token))
        toast["refresh"] = rolepage._refresh(("#rl-match", _match_url(opp_id), "outerHTML"))
        events["k-toast"] = toast
    else:
        events["k-toast"] = _toast("Undone.")
    html = await _match_html(request, opp_id)
    return HTMLResponse(html, headers=_events(events))


# ------------------------------------------------------------------------------- match, similar
@router.get("/opportunities/{opp_id}/role/match", response_class=HTMLResponse)
async def match(request: Request, opp_id: int):
    html = await _match_html(request, opp_id)
    return HTMLResponse(html, status_code=200 if html else 404)


@router.get("/opportunities/{opp_id}/role/similar", response_class=HTMLResponse)
async def similar(request: Request, opp_id: int, hash_: str = Query("", alias="hash")):
    """The rescore offer: the other open roles on this track that ask for the same thing (the exact
    requirement hash), listed before anything is rescored. Empty when there are none. Rescoring
    itself is the Score again dialog (/rescore/choose), which asks first for an online model."""
    hash = hash_
    if len(hash) != 40 or any(c not in "0123456789abcdef" for c in hash):
        return HTMLResponse("", status_code=400)
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        ids = [i for i in (await corr.roles_using(s, opp.track, [hash]))[hash] if i != opp_id]
        if not ids:
            return HTMLResponse("")
        conf, mapping = await confidential_once(s)
        rows = list(
            (
                await s.execute(
                    select(Opportunity.id, Opportunity.title, Opportunity.org_name)
                    .where(Opportunity.id.in_(ids))
                    .order_by(Opportunity.id)
                )
            ).all()
        )
    roles = [
        {
            "id": i,
            "title": redact_text(t, mapping, enabled=conf) or "Role",
            "org": redact_text(o, mapping, enabled=conf) or "",
        }
        for i, t, o in rows
    ]
    ctx = {
        "request": request,
        "opp": opp,
        "n": len(roles),
        "roles": roles[:SIMILAR_SHOWN],
        "more": max(0, len(roles) - SIMILAR_SHOWN),
        "ids": ",".join(str(r["id"]) for r in roles),
        "w": rolepage.words(opp.track),
    }
    html = rolepage._render(request, "partials/role_offer.html", ctx)
    return HTMLResponse(html)
