"""Prep pack page (SPEC-INTERFACES 2.1, `/prep/{opportunity_id}`). Shows the assembled interview
prep document for a shortlisted opportunity: content_md rendered as simple HTML, the likely
question -> canonical story mapping, the questions to ask (drawn from research unknowns), the
known panel, and PDF render/download.

Assembly (kestrel.interview.prep.assemble_prep_pack) is pure and offline — it only reads rows
already on hand (evaluation blocks, org research, canonical stories, contacts). Only the PDF step
touches Playwright/Edge (local-required, CLAUDE.md platform traps), so only that call is guarded
against a missing browser; a build call is still wrapped defensively since a write path must never
turn an unexpected error into a 500."""

from __future__ import annotations

import html
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Org, PrepPack, Story
from kestrel.interview.notes import get_notes, save_notes
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

router = APIRouter()


def _md_to_html(md: str) -> str:
    """content_md is generated in a small, known subset: `# ` / `## ` headings, `- ` bullets, bare
    paragraphs. Escaped by hand rather than pulling in a markdown dependency for three tag types."""
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        if line.startswith("## "):
            close_list()
            out.append(f"<h3>{html.escape(line[3:])}</h3>")
        elif line.startswith("# "):
            close_list()
            out.append(f"<h2>{html.escape(line[2:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            close_list()
            out.append(f"<p>{html.escape(line)}</p>")
    close_list()
    return "\n".join(out)


async def _story_titles(session: AsyncSession, story_ids: set[int]) -> dict[int, str]:
    if not story_ids:
        return {}
    rows = (await session.execute(select(Story).where(Story.id.in_(story_ids)))).scalars().all()
    return {s.id: s.title for s in rows}


async def _latest_prep_pack(session: AsyncSession, opportunity_id: int) -> PrepPack | None:
    return (
        await session.execute(
            select(PrepPack)
            .where(PrepPack.opportunity_id == opportunity_id)
            .order_by(PrepPack.created_at.desc(), PrepPack.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def redacted_question_view(session: AsyncSession, opportunity_id: int) -> dict:
    """The Likely questions card's data (question_task.question_view), redacted like every other
    surface in confidential mode (the reply is restored from its stand-in codes, so it can name
    the employer). One helper for the opportunity page and this page, so both cards match."""
    from kestrel.interview.question_task import question_view
    from kestrel.web.redact import confidential, redact_json

    conf, mapping = await confidential(session)
    return redact_json(await question_view(session, opportunity_id), mapping, enabled=conf)


async def _context(request: Request, session: AsyncSession, opportunity_id: int) -> dict:
    opportunity = await session.get(Opportunity, opportunity_id)
    org = await session.get(Org, opportunity.org_id) if opportunity and opportunity.org_id else None
    org_name = org.name if org else (opportunity.org_name if opportunity else None)

    prep = await _latest_prep_pack(session, opportunity_id) if opportunity is not None else None

    questions_map: list[dict] = []
    if prep is not None:
        stories_map = prep.stories if isinstance(prep.stories, dict) else {}
        titles = await _story_titles(session, {sid for sid in stories_map.values() if sid})
        questions_map = [
            {"question": q, "story_id": sid, "story_title": titles.get(sid)}
            for q, sid in stories_map.items()
        ]

    return {
        "title": "Interview prep",
        "query": dict(request.query_params),
        "opportunity": opportunity,
        "org_name": org_name,
        "prep": prep,
        "content_html": _md_to_html(prep.content_md) if prep and prep.content_md else "",
        "questions_map": questions_map,
        "questions_to_ask": (
            prep.questions_to_ask if prep and isinstance(prep.questions_to_ask, list) else []
        ),
        "panel": prep.panel if prep and isinstance(prep.panel, list) else [],
        "build_error": None,
        "auto_build": False,
        "pdf_error": None,
        "notes": await get_notes(session, opportunity_id) if opportunity is not None else "",
        "notes_saved": False,
        # The Likely interview questions card (P14), the same partial the opportunity page uses.
        "qview": (
            await redacted_question_view(session, opportunity_id) if opportunity is not None
            else None
        ),
        "opp_id": opportunity_id,
    }


@router.get("/prep/{opportunity_id}", response_class=HTMLResponse)
async def page(request: Request, opportunity_id: int):
    async with sessionmaker()() as s:
        ctx = await _context(request, s, opportunity_id)
    # "Interview prep" buttons link here with ?auto=1: the first open builds the pack through the
    # same build route the page's own button uses (D-UI-55).
    ctx["auto_build"] = request.query_params.get("auto") == "1" and ctx["prep"] is None \
        and ctx["opportunity"] is not None
    tmpl = "partials/prep_pack.html" if request.headers.get("HX-Request") else "prep.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/prep/{opportunity_id}/notes", response_class=HTMLResponse)
async def save_prep_notes(request: Request, opportunity_id: int):
    """Save "My notes for this interview": the operator's own text for this role, kept apart from
    the generated pack (so a rebuild never touches it) and read by the story builder."""
    form = await request.form()
    async with sessionmaker()() as s:
        if await s.get(Opportunity, opportunity_id) is None:
            return HTMLResponse("not found", status_code=404)
        notes = await save_notes(s, opportunity_id, str(form.get("notes") or ""))
        opportunity = await s.get(Opportunity, opportunity_id)
    ctx = {"opportunity": opportunity, "notes": notes, "notes_saved": True}
    return templates().TemplateResponse(request, "partials/prep_notes.html", ctx)


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
        ctx = await _context(request, s, opportunity_id)
    ctx["build_error"] = build_error
    return templates().TemplateResponse(request, "partials/prep_pack.html", ctx)


@router.post("/prep/{prep_pack_id}/pdf", response_class=HTMLResponse)
async def render_pdf(request: Request, prep_pack_id: int):
    """Render the stored prep pack to PDF via the Phase 8 Playwright/Edge pipeline. Local-required:
    a machine with no Edge installed (or Playwright not set up) must show a graceful message here,
    never a 500 (CLAUDE.md platform traps)."""
    from kestrel.interview.prep import render_prep_pack_pdf

    pdf_error: str | None = None
    async with sessionmaker()() as s:
        row = await s.get(PrepPack, prep_pack_id)
        if row is None:
            return HTMLResponse(
                '<div class="warnbox">Interview prep not found.</div>', status_code=404
            )
        opportunity_id = row.opportunity_id
        try:
            await render_prep_pack_pdf(s, prep_pack_id)
        except Exception as exc:  # e.g. Playwright/Edge not installed on this machine
            log.warning("prep_pdf_failed", prep_pack_id=prep_pack_id,
                        error_type=type(exc).__name__, error=str(exc))
            pdf_error = f"PDF render needs the local Edge/Playwright pipeline: {plain_error(exc)}"
        ctx = await _context(request, s, opportunity_id)
    ctx["pdf_error"] = pdf_error
    return templates().TemplateResponse(request, "partials/prep_pack.html", ctx)


@router.get("/prep/{prep_pack_id}/download")
async def download(prep_pack_id: int):
    async with sessionmaker()() as s:
        row = await s.get(PrepPack, prep_pack_id)
    if row is None or not row.pdf_path or not Path(row.pdf_path).exists():
        return HTMLResponse('<div class="warnbox">PDF not found.</div>', status_code=404)
    return FileResponse(
        row.pdf_path, media_type="application/pdf", filename=Path(row.pdf_path).name
    )
