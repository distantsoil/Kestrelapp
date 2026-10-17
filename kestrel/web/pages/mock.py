"""Mock interview page (SPEC-INTERFACES 2.1, "/mock/{opportunity_id}"). A conversational practice
loop: the operator picks a mode and starts a session, answers one question at a time, and sees the
per-answer critique — including the unsupported_claims flag, the check that a claim is not backed
by the operator's own proof points or canonical stories (SPEC-MODULES 3.3). History across past
sessions is listed below.

The model is asked only inside `submit_answer` (kestrel.interview.mock); `start_session` and
`end_session` never call it, so only the answer route needs to guard against a model that is not
running (constraint: a dead backend must render a message, never a 500)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import MockSession, Opportunity, Org
from kestrel.interview.mock import MODES, end_session, start_session, submit_answer
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()


async def _history(session: AsyncSession, opportunity_id: int) -> list[MockSession]:
    rows = (
        await session.execute(
            select(MockSession)
            .where(MockSession.opportunity_id == opportunity_id)
            .order_by(MockSession.started_at.desc(), MockSession.id.desc())
        )
    ).scalars().all()
    return list(rows)


def _pending_question(ms: MockSession | None) -> str | None:
    """The question awaiting an answer (transcript always carries at most one), so a killed
    session resumes on exactly this turn (hard rule 5)."""
    if ms is None:
        return None
    for turn in ms.transcript or []:
        if turn.get("answer") is None:
            return turn.get("question")
    return None


@router.get("/mock/{opportunity_id}", response_class=HTMLResponse)
async def page(request: Request, opportunity_id: int):
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opportunity_id)
        if opp is None:
            return HTMLResponse("Not found", status_code=404)
        org = await s.get(Org, opp.org_id) if opp.org_id else None
        history = await _history(s, opportunity_id)
    active = next((h for h in history if h.ended_at is None), None)
    ctx = {
        "title": "Mock interview",
        "query": dict(request.query_params),
        "opportunity_id": opportunity_id,
        "opp": opp,
        "org_name": (org.name if org else opp.org_name),
        "modes": MODES,
        "history": history,
        "active": active,
        "ms": active,
        "pending_question": _pending_question(active),
        "turn": None,
        "error": None,
    }
    tmpl = "partials/mock_body.html" if request.headers.get("HX-Request") else "mock.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/mock/{opportunity_id}/start", response_class=HTMLResponse)
async def start(request: Request, opportunity_id: int):
    form = await request.form()
    mode = str(form.get("mode") or "").strip()
    if mode not in MODES:
        return HTMLResponse('<div class="warnbox">Choose a mode first.</div>', status_code=400)
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opportunity_id)
        if opp is None:
            return HTMLResponse("not found", status_code=404)
        ms = await start_session(s, opportunity_id, mode)
    ctx = {"ms": ms, "pending_question": _pending_question(ms), "turn": None, "error": None}
    return templates().TemplateResponse(request, "partials/mock_active.html", ctx)


@router.post("/mock/session/{mock_session_id}/answer", response_class=HTMLResponse)
async def answer(request: Request, mock_session_id: int):
    """Critique the pending answer (P9). Renders the scores, worked_well/improvements, and the
    unsupported_claims list in a warnbox, then the next pending question. A quarantined critique or
    an unreachable local model must degrade to a message here, never a 500."""
    import httpx

    from kestrel.llm.client import LLMClient
    from kestrel.services.setting_overrides import load_settings_with_db

    form = await request.form()
    answer_text = str(form.get("answer") or "").strip()
    async with sessionmaker()() as s:
        ms = await s.get(MockSession, mock_session_id)
        if ms is None:
            return HTMLResponse("not found", status_code=404)
        if not answer_text:
            ctx = {"ms": ms, "pending_question": _pending_question(ms), "turn": None,
                   "error": "Write an answer before submitting."}
            return templates().TemplateResponse(request, "partials/mock_active.html", ctx)
        try:
            async with httpx.AsyncClient() as http:
                client = LLMClient(await load_settings_with_db(s), http=http)
                turn = await submit_answer(s, client, mock_session_id, answer_text)
        except Exception as exc:  # a model that is not running must not 500 the page
            ctx = {"ms": ms, "pending_question": _pending_question(ms), "turn": None,
                   "error": f"Mock interview needs the local model running: {exc}"}
            return templates().TemplateResponse(request, "partials/mock_active.html", ctx)
        if turn is None:
            ctx = {"ms": ms, "pending_question": _pending_question(ms), "turn": None,
                   "error": "The critique failed and was quarantined for review. Try again."}
            return templates().TemplateResponse(request, "partials/mock_active.html", ctx)
        ctx = {"ms": ms, "pending_question": _pending_question(ms), "turn": turn, "error": None}
    return templates().TemplateResponse(request, "partials/mock_active.html", ctx)


@router.post("/mock/session/{mock_session_id}/end", response_class=HTMLResponse)
async def end(request: Request, mock_session_id: int):
    async with sessionmaker()() as s:
        ms = await end_session(s, mock_session_id)
    if ms is None:
        return HTMLResponse("not found", status_code=404)
    return templates().TemplateResponse(request, "partials/mock_finished.html", {"ms": ms})
