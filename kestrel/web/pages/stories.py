"""Story bank (SPEC-INTERFACES 2.1, lines 229-231). Canonical stories with theme coverage and gap
analysis, consolidation proposals awaiting review, usage counts, and an inline editor.

Theme coverage comes straight from `kestrel.interview.coverage.theme_coverage` (SPEC-MODULES 3.1):
gaps are shown first and loudest (a missing "failure" story is more useful to know than forty
mediocre ones), and the note states the total against the 8-12 target rather than implying a small
bank is solid coverage (hard rule 6).

A canonical story with `operator_edited is False` is a model proposal from consolidation
(kestrel/interview/stories.py `consolidate_stories`) and is marked "proposed" here. The moment the
operator edits it through the form below, `operator_edited` is set True and that story is theirs:
`consolidate_stories` checks this flag and only ever attaches new sketches as usage to an
operator-edited story, never rewrites its text (the never-overwrite guarantee)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Pursuit, Story
from kestrel.interview.coverage import theme_coverage
from kestrel.interview.notes import all_notes
from kestrel.interview.stories import delete_story, dismiss_draft
from kestrel.interview.story_task import build_state, last_build, request_build
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()


async def _canonical_stories(session: AsyncSession) -> list[Story]:
    """Canonical stories, proposals-awaiting-review first (so they surface for triage), then the
    operator's own most-used stories. Empty when the bank is empty (handled gracefully)."""
    stmt = (
        select(Story)
        .where(Story.is_canonical.is_(True))
        .order_by(Story.operator_edited.asc(), Story.use_count.desc(), Story.id.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _build_ctx(session: AsyncSession) -> dict:
    """The build line from the task row (story_task.build_state), so a build killed mid-run reads
    "failed" with its reason, not a stale "Last build" or nothing at all."""
    state = await build_state(session)
    return {
        "building": state["state"] == "building",
        "build_failed": state.get("reason") if state["state"] == "failed" else None,
        "last_build": await last_build(session),
    }


async def _notes_ctx(session: AsyncSession) -> dict:
    """"Your notes" (D-UI-55): each role with saved interview notes, linking to that role's prep
    page, and the roles on the shortlist or in Applications the operator can add notes to."""
    saved = await all_notes(session)
    titles: dict[int, tuple[str, str, str]] = {}
    ids = {opp_id for opp_id, _t, _at in saved}
    if ids:
        rows = (
            await session.execute(select(Opportunity).where(Opportunity.id.in_(ids)))
        ).scalars().all()
        titles = {o.id: (o.title or "Role", o.org_name or "", o.track) for o in rows}
    notes_list = [
        {
            "opportunity_id": opp_id,
            "title": titles.get(opp_id, ("Role", "", "employment"))[0],
            "org": titles.get(opp_id, ("", "", ""))[1],
            "saved_at": (at or "")[:10],
            "preview": " ".join(text.split())[:140],
        }
        for opp_id, text, at in reversed(saved)
        if opp_id in titles
    ]
    picks = (
        await session.execute(
            select(Opportunity)
            .join(Pursuit, Pursuit.opportunity_id == Opportunity.id)
            .where(Pursuit.closed_at.is_(None))
            .order_by(Pursuit.id.desc())
        )
    ).scalars().all()
    return {
        "notes_list": notes_list,
        "notes_count": len(notes_list),
        "note_choices": [
            {"id": o.id, "label": o.title or "Role", "org": o.org_name or ""} for o in picks
        ],
    }


async def _content_ctx(session: AsyncSession, **extra) -> dict:
    return {
        **await _notes_ctx(session),
        "report": await theme_coverage(session),
        "stories": await _canonical_stories(session),
        **await _build_ctx(session),
        "story_notice": None,
        "story_error": None,
        **extra,
    }


@router.get("/stories", response_class=HTMLResponse)
async def page(request: Request):
    async with sessionmaker()() as s:
        ctx = await _content_ctx(s)
    ctx.update({"title": "My stories", "query": dict(request.query_params)})
    tmpl = "partials/stories_content.html" if request.headers.get("HX-Request") else "stories.html"
    return templates().TemplateResponse(request, tmpl, ctx)


_STORY_FIELDS = ("situation", "task", "action", "result", "reflection")


@router.post("/stories", response_class=HTMLResponse)
async def add_story(request: Request):
    """Add a story by hand. It is the operator's own words from the first save, so it is created
    already edited (`operator_edited`), canonical, and out of reach of any later merge."""
    form = await request.form()
    title = str(form.get("title") or "").strip()[:200]
    async with sessionmaker()() as s:
        if not title:
            ctx = await _content_ctx(s, story_error="Not added: give the story a title.")
        else:
            story = Story(
                title=title, is_canonical=True, operator_edited=True, themes=[],
                **{f: str(form.get(f) or "").strip() or None for f in _STORY_FIELDS},
            )
            s.add(story)
            await s.commit()
            ctx = await _content_ctx(
                s, story_notice="Story added. It is yours; nothing rewrites it."
            )
    return templates().TemplateResponse(request, "partials/stories_content.html", ctx)


@router.post("/stories/build", response_class=HTMLResponse)
async def build_stories(request: Request):
    """Queue a build of draft stories from the operator's interview notes and the interview
    sketches in each scored role. Only a task row is written here; the worker does the model
    work (kestrel.interview.story_task), so this returns at once."""
    async with sessionmaker()() as s:
        queued = await request_build(s)
        ctx = await _content_ctx(s)
    ctx["story_notice"] = (
        "Building stories in the background. They appear here as drafts when it finishes."
        if queued else "A build is already running. Drafts appear here when it finishes."
    )
    return templates().TemplateResponse(request, "partials/stories_content.html", ctx)


@router.get("/stories/notes/add")
async def add_notes(opportunity_id: int = 0):
    """Open a role's prep notes box. Adding notes happens on the role's prep page."""
    if opportunity_id <= 0:
        return RedirectResponse("/stories", status_code=303)
    return RedirectResponse(f"/prep/{opportunity_id}#prep-notes", status_code=303)


@router.post("/stories/{story_id}/dismiss", response_class=HTMLResponse)
async def dismiss_story(request: Request, story_id: int):
    """Dismiss a proposed story. It is removed and remembered, so Build stories does not propose
    it again. A story the operator saved is theirs and is deleted, not dismissed."""
    async with sessionmaker()() as s:
        done = await dismiss_draft(s, story_id)
        ctx = await _content_ctx(
            s,
            story_notice=(
                "Draft dismissed. Build stories will not propose it again." if done else None
            ),
            story_error=(
                None if done else "That draft is already gone, or it is a story you saved."
            ),
        )
    return templates().TemplateResponse(request, "partials/stories_content.html", ctx)


@router.post("/stories/{story_id}/delete", response_class=HTMLResponse)
async def delete_saved_story(request: Request, story_id: int):
    async with sessionmaker()() as s:
        done = await delete_story(s, story_id)
        ctx = await _content_ctx(
            s,
            story_notice="Story deleted." if done else None,
            story_error=None if done else "That story is already gone.",
        )
    return templates().TemplateResponse(request, "partials/stories_content.html", ctx)


@router.post("/stories/{story_id}", response_class=HTMLResponse)
async def edit_story(request: Request, story_id: int):
    """Inline editor. The operator's rewrite persists byte-for-byte into the six STAR+Reflection
    fields, and `operator_edited` is set True UNCONDITIONALLY on any save — even a save that changes
    nothing — because from this point the story is the operator's own words, not a model merge, and
    must never be silently overwritten by a later consolidation run."""
    form = await request.form()
    async with sessionmaker()() as s:
        story = await s.get(Story, story_id)
        if story is None:
            return HTMLResponse("not found", status_code=404)
        if "title" in form:
            story.title = str(form["title"])
        if "situation" in form:
            story.situation = str(form["situation"])
        if "task" in form:
            story.task = str(form["task"])
        if "action" in form:
            story.action = str(form["action"])
        if "result" in form:
            story.result = str(form["result"])
        if "reflection" in form:
            story.reflection = str(form["reflection"])
        story.operator_edited = True
        await s.commit()
        await s.refresh(story)
        ctx = {"story": story}
    return templates().TemplateResponse(request, "partials/story_item.html", ctx)
