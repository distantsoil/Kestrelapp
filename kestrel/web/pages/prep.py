"""Interview prep (SPEC-INTERFACES 2.1, `/prep/{opportunity_id}`; D-UI-121). One screen, three
tabs: the prep pack, practice (`/mock`) and the story bank (`/stories`). This module serves the
shell and the Prep pack tab: the pack itself, the operator's notes (saved as they type), the
questions to ask them, who they may meet, the story chosen for each question, and the PDF.

Assembly (kestrel.interview.prep.assemble_prep_pack) is pure and offline. Only the PDF step
touches Playwright/Edge (local-required, CLAUDE.md platform traps), so only that call is guarded
against a missing browser; a build call is still wrapped defensively since a write path must never
turn an unexpected error into a 500. Nothing on this screen contacts anyone (hard rule 1): the
people the operator adds are their own notes."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, PrepPack, Story
from kestrel.interview import prep_state
from kestrel.interview.notes import save_notes
from kestrel.interview.render import PAPER_CSS, pack_pages
from kestrel.web import prep_view as pv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

router = APIRouter()


async def redacted_question_view(session: AsyncSession, opportunity_id: int) -> dict:
    """The Likely questions card's data (question_task.question_view), redacted like every other
    surface in confidential mode (the reply is restored from its stand-in codes, so it can name
    the employer). One helper for the opportunity page and this page, so both cards match."""
    from kestrel.interview.question_task import question_view
    from kestrel.web.redact import confidential, redact_json

    conf, mapping = await confidential(session)
    view = redact_json(await question_view(session, opportunity_id), mapping, enabled=conf)
    view["route"] = await questions_route(session)  # not redacted: provider and model names
    return view


async def questions_route(session: AsyncSession) -> dict:
    """Where the likely questions are written: on this computer, or by the online Tier 2 model,
    in which case the ask-first panel states what is sent."""
    from kestrel.interview.question_task import QUESTIONS_SENDS, TIER
    from kestrel.services.online_consent import online_route
    from kestrel.services.setting_overrides import load_settings_with_db

    return await online_route(
        session, await load_settings_with_db(session), TIER, this_send=QUESTIONS_SENDS
    )


def events(**kw: object) -> dict[str, str]:
    """An HX-Trigger header. ensure_ascii keeps it latin-1 safe for the header."""
    return {"HX-Trigger": json.dumps(kw, ensure_ascii=True)}


def toast(message: str, undo_url: str | None = None, tone: str | None = None) -> dict:
    body: dict[str, object] = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    if tone:
        body["tone"] = tone
    return body


def _tab_of(request: Request, default: str = "pack") -> str:
    tab = request.query_params.get("tab") or default
    return tab if tab in pv.TABS else default


async def page_response(request: Request, opportunity_id: int | None, tab: str):
    """The whole page, or just the body when HTMX asks for a tab or a refresh."""
    async with sessionmaker()() as s:
        if opportunity_id is None:
            opportunity_id = await pv.default_role_id(s)
        ctx = await pv.body_ctx(request, s, tab, opportunity_id)
        ctx["qview"] = (
            await redacted_question_view(s, opportunity_id) if ctx["opp"] is not None else None
        )
        ctx["qnote"] = None
    # "Interview prep" buttons link here with ?auto=1: the first open builds the pack through the
    # same build route the page's own button uses (D-UI-55).
    ctx["auto_build"] = (
        request.query_params.get("auto") == "1" and tab == "pack"
        and ctx["opp"] is not None and not ctx.get("has_pack")
    )
    hx = request.headers.get("HX-Request") and request.headers.get("HX-Target") == "pp-body"
    return templates().TemplateResponse(
        request, "partials/pp_body.html" if hx else "prep.html", ctx
    )


@router.get("/prep", response_class=HTMLResponse)
async def prep_home(request: Request):
    return await page_response(request, None, _tab_of(request))


@router.get("/prep/{opportunity_id}", response_class=HTMLResponse)
async def page(request: Request, opportunity_id: int):
    return await page_response(request, opportunity_id, _tab_of(request))


async def pack_response(
    request: Request, opportunity_id: int, *, message: str | None = None,
    undo_url: str | None = None, error: str | None = None, **extra: object,
):
    """The Prep pack tab body again, after a build or a PDF. Cards that change on their own
    (ticks, a swapped story, a note) answer with just that card instead."""
    async with sessionmaker()() as s:
        ctx = await pv.body_ctx(request, s, "pack", opportunity_id, **extra)
        ctx["qview"] = await redacted_question_view(s, opportunity_id)
        ctx["qnote"] = None
    ev = {"k-toast": toast(message, undo_url)} if message else None
    return templates().TemplateResponse(
        request, "partials/pp_body.html", {**ctx, "build_error": error or ctx.get("build_error")},
        headers=events(**ev) if ev else None,
    )


async def fragments_response(
    request: Request, opportunity_id: int, parts: tuple[str, ...], *, message: str | None = None,
    undo_url: str | None = None, tone: str | None = None, **extra: object,
):
    """Re-render only what a change touched, as out-of-band swaps by id: the cards named in
    `parts` (pp_questions, pp_ready, pp_ask, pp_people, pp_tabs). The page keeps its place."""
    async with sessionmaker()() as s:
        ctx = await pv.body_ctx(request, s, "pack", opportunity_id, **extra)
    ctx["oob"] = True
    ctx["parts"] = parts
    headers = events(**{"k-toast": toast(message, undo_url, tone)}) if message else None
    return templates().TemplateResponse(request, "partials/pp_parts.html", ctx, headers=headers)


# ------------------------------------------------------------------------------------ notes
@router.post("/prep/{opportunity_id}/notes", response_class=HTMLResponse)
async def save_prep_notes(request: Request, opportunity_id: int):
    """Save "Your notes" as the operator types: the operator's own text for this role, kept apart
    from the generated pack (so a rebuild never touches it) and read by the story builder.
    Answers with the little Saved indicator only; the box itself is never swapped."""
    form = await request.form()
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        notes = await save_notes(s, opportunity_id, str(form.get("notes") or ""))
    return templates().TemplateResponse(
        request, "partials/pp_saved.html", {"saved": True, "notes": notes}
    )


# ------------------------------------------------------------------------------------ build
@router.post("/prep/{opportunity_id}/build", response_class=HTMLResponse)
async def build(request: Request, opportunity_id: int):
    """Assemble a fresh prep pack row. assemble_prep_pack is pure/offline, but the write path is
    still wrapped: an unexpected assembly error must render a message here, never a 500 (an
    operator staring at a blank prep pack should see why, not a stack trace)."""
    from kestrel.interview.prep import assemble_prep_pack

    build_error: str | None = None
    async with sessionmaker()() as s:
        try:
            built = await assemble_prep_pack(s, opportunity_id)
            if built is None:
                build_error = "Opportunity not found."
        except Exception as exc:  # defends the write path the same way the PDF/import paths do
            log.warning("prep_build_failed", opportunity_id=opportunity_id,
                        error_type=type(exc).__name__, error=str(exc))
            build_error = f"Could not prepare this interview: {plain_error(exc)}"
    return await pack_response(request, opportunity_id, error=build_error)


# --------------------------------------------------------------------- the story for a question
@router.post("/prep/{opportunity_id}/choose", response_class=HTMLResponse)
async def choose_story(request: Request, opportunity_id: int):
    """Swap story: point a question at another story from the operator's bank. The choice is
    kept apart from the pack, so a rebuild never undoes it; Undo restores what was there, and only
    if nothing changed it since."""
    form = await request.form()
    raw = str(form.get("story_id") or "")
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        question = await pv.question_for(s, opportunity_id, str(form.get("qh") or "")) or ""
        story = await s.get(Story, int(raw)) if raw.isdigit() else None
        if not question or story is None or not story.is_canonical:
            return await fragments_response(
                request, opportunity_id, ("pp_questions", "pp_ready"),
                message="That story is no longer in your bank.", tone="warn",
            )
        was = (await prep_state.story_choices(s, opportunity_id)).get(question)
        await prep_state.set_story_choice(s, opportunity_id, question, story.id)
        await s.commit()
    from urllib.parse import urlencode

    undo = "/prep/undo/choice?" + urlencode(
        {"opp": opportunity_id, "q": question, "was": was or 0, "new": story.id}
    )
    return await fragments_response(
        request, opportunity_id, ("pp_questions", "pp_ready"),
        message="Story swapped for this question.", undo_url=undo,
    )


@router.post("/prep/undo/choice", response_class=HTMLResponse)
async def undo_choice(request: Request):
    """Undo a swap or a new story link: put back the story that was there, but only while the
    question still points at the story the action wrote. Answers with a line the page reads."""
    q = request.query_params
    try:  # a hand-built link is refused, never a 500
        opportunity_id = int(q.get("opp") or 0)
        was = int(q.get("was") or 0)
        new = int(q.get("new") or 0)
    except ValueError:
        return HTMLResponse('<span data-undo-msg data-ok="0">This can no longer be undone.</span>')
    question = q.get("q") or ""
    async with sessionmaker()() as s:
        now = (await prep_state.story_choices(s, opportunity_id)).get(question)
        ok = bool(question) and now == new
        if ok:
            await prep_state.set_story_choice(s, opportunity_id, question, was or None)
            await s.commit()
    return HTMLResponse(
        f'<span data-undo-msg data-ok="{1 if ok else 0}">'
        f'{"Undone." if ok else "This can no longer be undone."}</span>'
    )


# ----------------------------------------------------------------------- questions to ask them
@router.post("/prep/{opportunity_id}/ask/tick", response_class=HTMLResponse)
async def tick_ask(request: Request, opportunity_id: int):
    form = await request.form()
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        text = await pv.ask_text(s, opportunity_id, str(form.get("i") or ""))
        if text:
            await prep_state.tick_question(
                s, opportunity_id, text, str(form.get("on") or "") == "1"
            )
            await s.commit()
    return await fragments_response(request, opportunity_id, ("pp_ready",))


@router.post("/prep/{opportunity_id}/ask", response_class=HTMLResponse)
async def add_ask(request: Request, opportunity_id: int):
    form = await request.form()
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        added = await prep_state.add_question(s, opportunity_id, str(form.get("question") or ""))
        await s.commit()
    return await fragments_response(
        request, opportunity_id, ("pp_ask", "pp_ready"),
        message="Added to your questions." if added else "Write a question first.",
        tone=None if added else "warn",
    )


@router.post("/prep/{opportunity_id}/people", response_class=HTMLResponse)
async def add_person(request: Request, opportunity_id: int):
    """Add someone the operator heard will be in the room. It is their own note; Kestrel never
    contacts anyone (hard rule 1)."""
    form = await request.form()
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        ok = await prep_state.add_person(
            s, opportunity_id, str(form.get("name") or ""), str(form.get("title") or "")
        )
        await s.commit()
    return await fragments_response(
        request, opportunity_id, ("pp_people",),
        message=None if ok else "Write a name first.", tone=None if ok else "warn",
    )


# ------------------------------------------------------------------------------ the PDF
def _file_name(org: str, when: date | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (org or "").lower()).strip("-") or "interview"
    return f"prep-pack-{slug}-{(when or date.today()).isoformat()}.pdf"


@router.get("/prep/{opportunity_id}/preview", response_class=HTMLResponse)
async def preview(request: Request, opportunity_id: int):
    """The prep pack as the pages that will be saved, in a preview before the Save dialog. The
    paper is white with ink by design; the pages are the PDF's own, scaled, never reflowed."""
    async with sessionmaker()() as s:
        sh = await pv.shell_ctx(s, opportunity_id)
        model = await pv.pack_model(s, sh)
        pack = await pv.latest_pack(s, opportunity_id) if sh["opp"] is not None else None
    ctx = {
        "opp_id": opportunity_id, "pack": pack, "model": model, "pages": pack_pages(model)
        if model else [], "paper_css": PAPER_CSS,
        "file_name": _file_name(sh["org_name"], sh["when"]["date"]),
    }
    return templates().TemplateResponse(request, "partials/pp_preview.html", ctx)


@router.post("/prep/{prep_pack_id}/pdf", response_class=HTMLResponse)
async def render_pdf(request: Request, prep_pack_id: int):
    """Render the stored prep pack to PDF via the Phase 8 Playwright/Edge pipeline, from the same
    pages the preview showed. Local-required: a machine with no Edge installed (or Playwright not
    set up) must show a graceful message here, never a 500 (CLAUDE.md platform traps)."""
    from kestrel.interview.prep import render_prep_pack_pdf

    async with sessionmaker()() as s:
        row = await s.get(PrepPack, prep_pack_id)
        if row is None:
            return HTMLResponse(
                '<div class="pp-pdf-error" role="alert">Interview prep not found.</div>',
                status_code=404,
            )
        sh = await pv.shell_ctx(s, row.opportunity_id)
        model = await pv.pack_model(s, sh)
        try:
            await render_prep_pack_pdf(s, prep_pack_id, model=model)
        except Exception as exc:  # e.g. Playwright/Edge not installed on this machine
            log.warning("prep_pdf_failed", prep_pack_id=prep_pack_id,
                        error_type=type(exc).__name__, error=str(exc))
            return HTMLResponse(
                '<div class="pp-pdf-error" role="alert" data-testid="pp-pdf-error">'
                "The PDF needs Microsoft Edge on this computer, and it could not be started: "
                f"{_esc(plain_error(exc))}</div>"
            )
    name = _file_name(sh["org_name"], sh["when"]["date"])
    return HTMLResponse(
        '<div class="pp-pdf-ok" role="status" data-testid="pp-pdf-ok">Saved.</div>',
        headers=events(**{
            "pp-download": {"url": f"/prep/{prep_pack_id}/download", "name": name},
            "k-toast": toast(f"{name} is ready. Your computer will ask where to save it."),
        }),
    )


def _esc(text: str) -> str:
    import html

    return html.escape(text)


@router.get("/prep/{prep_pack_id}/download")
async def download(prep_pack_id: int):
    async with sessionmaker()() as s:
        row = await s.get(PrepPack, prep_pack_id)
        sh = await pv.shell_ctx(s, row.opportunity_id) if row is not None else None
    if row is None or not row.pdf_path or not Path(row.pdf_path).exists():
        return HTMLResponse('<div class="warnbox">PDF not found.</div>', status_code=404)
    name = _file_name(sh["org_name"], sh["when"]["date"]) if sh else Path(row.pdf_path).name
    return FileResponse(row.pdf_path, media_type="application/pdf", filename=name)
