"""Practice (SPEC-INTERFACES 2.1, "/mock/{opportunity_id}"; the Practice tab of Interview prep,
D-UI-121). A conversational loop: the operator picks a mode and starts a session, answers one
question at a time, and sees the critique, including any claim not in their own proof points or
stories (SPEC-MODULES 3.3). The answer is typed; voice is not part of the spec.

The model is asked only inside `submit_answer` (kestrel.interview.mock); starting, skipping and
ending never call it, so only the answer route needs to guard against a model that is not running.

Where it runs: on this computer by default. An online model is used only when the operator says so
in the ask-first dialog (it lists exactly what is sent, the provider and the cost) or when the
Models settings already point the full-review tier at one; either way the answer route checks the
confirmation, Hosted mode and the model on the server, and holds the send under confidential mode
when no employer is chosen (the same rule as the offers draft)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import MockSession, Opportunity
from kestrel.interview.mock import (
    MODES,
    acknowledge_claim,
    end_session,
    skip_question,
    start_session,
    submit_answer,
)
from kestrel.interview.prep import live_view
from kestrel.pipeline._shared import settings_with_override, tier_is_hosted
from kestrel.services.online_consent import confirmation_matches
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import prep_view as pv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.prep import events, page_response, toast

log = structlog.get_logger()

router = APIRouter()

TIER = pv.PRACTICE_TIER
route_info = pv.route_info


async def _client_settings(session: AsyncSession, form) -> tuple[object | None, str | None, str]:
    """(settings for this answer, why not, where it ran). The server decides from the settings and
    the posted confirmation; the page's own state is never trusted. A refusal says that nothing
    was sent."""
    settings = await load_settings_with_db(session)
    route = await route_info(session, settings)
    confirmed = str(form.get("confirmed") or "") == "1"
    pick = str(form.get("route_model") or "").strip()
    if pick:
        from kestrel.web.pages.needs_try import _offered_models
        from kestrel.web.pages.rescore import HOSTED_PREFIX, _override_for

        if not confirmed:
            return None, "Nothing was sent. Confirm the online model first.", ""
        if not settings.llm.hosted_mode.active:
            return None, "Nothing was sent. Online models need Hosted mode, which is off.", ""
        if not pick.startswith(HOSTED_PREFIX):
            return None, "Nothing was sent. That is not a model on the online list.", ""
        offered, _ = await _offered_models(settings, None)
        if pick.removeprefix(HOSTED_PREFIX) not in {m["id"] for m in offered}:
            return None, "Nothing was sent. That model is not on the online list.", ""
        override, _label, why = await _override_for(settings, pick)
        if override is None:
            return None, f"Nothing was sent. {why}", ""
        if route["employer"] == "held":
            return None, _HELD, ""
        effective = settings_with_override(settings, TIER, override)
        from kestrel.services.online_consent import approval_covers, approval_for

        if not approval_covers(approval_for(effective, (TIER,)), form.get("online_approval")):
            return None, "Nothing was sent. The online service changed. Open the dialog again.", ""
        return effective, None, "online"
    if route["native"]:
        if not confirmed or not confirmation_matches(route, form, model_field="route_model_native"):
            return None, "Nothing was sent. Confirm the online model first.", ""
        if route["employer"] == "held":
            return None, _HELD, ""
        return settings, None, "online"
    return settings, None, "computer"


_HELD = (
    "Nothing was sent. Confidential mode is on and no current employer is chosen, so Kestrel "
    "will not send this. Choose your employer in Settings, or practice on your computer."
)


async def chat_ctx(
    request: Request, s: AsyncSession, opportunity_id: int, *, ms: MockSession | None = None,
    just_answered: bool = False, error: str | None = None, **extra: object,
) -> dict:
    ctx = await pv.body_ctx(request, s, "practice", opportunity_id)
    if ms is not None:
        # a session just answered or ended may no longer be the open one
        ctx["active"] = ms if ms.ended_at is None else None
        pending = pv.pending_question(ms) if ms.ended_at is None else None
        ctx["pending"] = pv.redact_text(pending, ctx["emp"], enabled=ctx["conf"])
        ctx["turns"] = pv.turns_of(ms, ctx["emp"], ctx["conf"])
        ctx["live_mean"] = pv._mean(ms.mean_scores) if ms.ended_at is None else None
        ctx["hint"] = await pv.practice_hint(s, opportunity_id, pending)
    ctx.update(just_answered=just_answered, error=error, oob=False, **extra)
    return ctx


async def _chat(request: Request, opportunity_id: int, **kw):
    msg = kw.pop("message", None)
    parts = kw.pop("parts", ("pp_chat", "pp_hist"))
    async with sessionmaker()() as s:
        ctx = await chat_ctx(request, s, opportunity_id, **kw)
    ctx.update(oob=True, parts=parts)
    headers = events(**{"k-toast": toast(msg)}) if msg else None
    return templates().TemplateResponse(request, "partials/pp_parts.html", ctx, headers=headers)


@router.get("/mock/{opportunity_id}", response_class=HTMLResponse)
async def page(request: Request, opportunity_id: int):
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("Not found", status_code=404)
    return await page_response(request, opportunity_id, "practice")


@router.post("/mock/{opportunity_id}/start", response_class=HTMLResponse)
async def start(request: Request, opportunity_id: int):
    form = await request.form()
    mode = str(form.get("mode") or "").strip()
    if mode not in MODES:
        return await _chat(request, opportunity_id, error="Choose a mode first.")
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opportunity_id)
        if opp is None:
            return HTMLResponse("not found", status_code=404)
        first = await pv.question_for(s, opportunity_id, str(form.get("qh") or ""))
        ms = await start_session(s, opportunity_id, mode, first_question=first)
    return await _chat(request, opportunity_id, ms=ms)


@router.post("/mock/session/{mock_session_id}/answer", response_class=HTMLResponse)
async def answer(request: Request, mock_session_id: int):
    """Critique the pending answer (P9). Renders the scores, what worked and what to change, and
    any claim that is not in the operator's own proof points or stories, then the next question.
    A quarantined critique or an unreachable model degrades to a message here, never a 500."""
    import httpx

    from kestrel.llm.client import LLMClient

    form = await request.form()
    answer_text = str(form.get("answer") or "").strip()
    async with sessionmaker()() as s:
        ms = await s.get(MockSession, mock_session_id)
        if ms is None:
            return HTMLResponse("not found", status_code=404)
        opp_id = ms.opportunity_id
    if not answer_text:
        return await _chat(request, opp_id, ms=ms, error="Write an answer before submitting.")
    async with sessionmaker()() as s:
        settings, why, ran_on = await _client_settings(s, form)
        if settings is None:
            return await _chat(request, opp_id, ms=ms, error=why)
        try:
            async with httpx.AsyncClient() as http:
                client = LLMClient(settings, http=http)
                turn = await submit_answer(s, client, mock_session_id, answer_text, ran_on=ran_on)
        except Exception as exc:  # a model that is not running must not 500 the page
            log.warning("mock_answer_failed", mock_session_id=mock_session_id,
                        error_type=type(exc).__name__, error=str(exc))
            return await _chat(
                request, opp_id, ms=ms,
                error=f"Practice needs the model running: {plain_error(exc)}",
            )
        if turn is None:
            await s.rollback()
            return await _chat(
                request, opp_id, ms=ms,
                error="The feedback failed and was set aside for review. Try again.",
            )
        ms = await s.get(MockSession, mock_session_id)
        await s.refresh(ms)
    return await _chat(request, opp_id, ms=ms, just_answered=True)


@router.post("/mock/session/{mock_session_id}/skip", response_class=HTMLResponse)
async def skip(request: Request, mock_session_id: int):
    async with sessionmaker()() as s:
        ms = await s.get(MockSession, mock_session_id)
        if ms is None:
            return HTMLResponse("not found", status_code=404)
        lv = await live_view(s, ms.opportunity_id)
        new = await skip_question(s, mock_session_id, [q["question"] for q in (lv.questions if lv
                                                                              else [])])
        await s.refresh(ms)
    return await _chat(
        request, ms.opportunity_id, ms=ms,
        message=None if new else "No other question to ask. This one stays.",
    )


@router.post("/mock/session/{mock_session_id}/end", response_class=HTMLResponse)
async def end(request: Request, mock_session_id: int):
    async with sessionmaker()() as s:
        ms = await end_session(s, mock_session_id)
    if ms is None:
        return HTMLResponse("not found", status_code=404)
    n = ms.question_count or 0
    return await _chat(
        request, ms.opportunity_id,
        message=f"Session saved: {n} {'answer' if n == 1 else 'answers'}. "
        "You can review it later.",
    )


@router.get("/mock/session/{mock_session_id}", response_class=HTMLResponse)
async def review(request: Request, mock_session_id: int):
    """A finished session, read-only, in the chat area."""
    async with sessionmaker()() as s:
        ms = await s.get(MockSession, mock_session_id)
        if ms is None:
            return HTMLResponse("not found", status_code=404)
        ctx = await chat_ctx(request, s, ms.opportunity_id, ms=ms)
    ctx.update(oob=False, review=ms, active=None, pending=None)
    return templates().TemplateResponse(request, "partials/pp_chat.html", ctx)


@router.post("/mock/session/{mock_session_id}/claim", response_class=HTMLResponse)
async def claim_seen(request: Request, mock_session_id: int):
    form = await request.form()
    try:
        turn, claim = int(str(form.get("turn"))), int(str(form.get("claim")))
    except ValueError:
        return HTMLResponse("", status_code=400)
    async with sessionmaker()() as s:
        await acknowledge_claim(s, mock_session_id, turn, claim)
    return HTMLResponse("")


# --------------------------------------------------------------------- where it runs
@router.get("/mock/{opportunity_id}/online", response_class=HTMLResponse)
async def online_dialog(request: Request, opportunity_id: int):
    """The ask-first dialog: the provider, exactly what is sent (the Tier 2 line of the Hosted
    mode disclosure, the text the operator acknowledged), what is not, the cost and the credit
    left. Nothing is sent until the operator presses Use online model."""
    from kestrel.services import hosted_balance

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        info = await route_info(s, settings, with_models=True)
        credit = await hosted_balance.rail_row(s, settings)
        sh = await pv.shell_ctx(s, opportunity_id)
    ctx = {
        "opp_id": opportunity_id, "route": info, "credit": credit["label"] if credit else None,
        "role": sh["role_title"] or "this role",
    }
    return templates().TemplateResponse(request, "partials/pp_online.html", ctx)


@router.post("/mock/{opportunity_id}/route", response_class=HTMLResponse)
async def set_route(request: Request, opportunity_id: int):
    """Use an online model for this practice session (or confirm the one already configured).
    Nothing is stored: the confirmation rides on the page and the answer route checks it again."""
    form = await request.form()
    pick = str(form.get("model") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        info = await route_info(s, settings, with_models=not tier_is_hosted(settings, TIER))
        problem = None
        if str(form.get("confirmed") or "") != "1":
            problem = "Nothing was sent. Confirm in the dialog first."
        elif info["native"]:
            if not confirmation_matches(info, form):
                problem = (
                    "Nothing was sent. The model or online service changed. "
                    "Open the dialog again."
                )
            pick = ""
        elif not settings.llm.hosted_mode.active:
            problem = "Nothing was sent. Online models need Hosted mode, which is off."
        elif pick.removeprefix("hosted:") not in {m["id"] for m in info.get("models", [])}:
            problem = "Nothing was sent. That model is not on the online list."
        label = ""
        if not problem and pick:
            from kestrel.services.online_consent import approval_covers, approval_for
            from kestrel.web.pages.rescore import _override_for

            override, _label, why = await _override_for(settings, pick)
            if override is None:
                problem = f"Nothing was sent. {why}"
            else:
                effective = settings_with_override(settings, TIER, override)
                if not approval_covers(
                    approval_for(effective, (TIER,)), form.get("online_approval")
                ):
                    problem = "Nothing was sent. The online service changed. Open the dialog again."
            label = next(
                (m["name"] for m in info["models"] if m["id"] == pick.removeprefix("hosted:")),
                pick,
            )
    ctx = {
        "opp_id": opportunity_id, "route": info, "oob": False,
        "picked": pick if not problem else "", "picked_name": label,
        "confirmed": not problem, "route_error": problem,
        "online_approval": form.get("online_approval") or "{}",
    }
    headers = events(**{"k-toast": toast(problem or "Online model on for this session only.",
                                         tone="warn" if problem else None)})
    return templates().TemplateResponse(request, "partials/pp_route.html", ctx, headers=headers)


@router.get("/mock/{opportunity_id}/route", response_class=HTMLResponse)
async def local_route(request: Request, opportunity_id: int):
    """Back to this computer: the route line with no confirmation. Nothing more is sent."""
    async with sessionmaker()() as s:
        info = await route_info(s, await load_settings_with_db(s))
    ctx = {"opp_id": opportunity_id, "route": info, "oob": False, "picked": "",
           "picked_name": "", "confirmed": False, "route_error": None}
    return templates().TemplateResponse(
        request, "partials/pp_route.html", ctx,
        headers=events(**{"k-toast": toast("Back on your computer. Nothing more is sent.")}),
    )
