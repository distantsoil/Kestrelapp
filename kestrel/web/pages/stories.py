"""Story bank (SPEC-INTERFACES 2.1 `/stories`; the Story bank tab of Interview prep, D-UI-121).
Canonical stories with theme coverage and gap analysis, consolidation proposals awaiting review,
usage counts and the sheet that edits a story. `/stories` opens the Story bank tab; the same bank
is the Stories tab of Profile (there is one bank).

Theme coverage comes straight from `kestrel.interview.coverage.theme_coverage` (SPEC-MODULES 3.1):
gaps are shown first and loudest (a missing "failure" story is more useful to know than forty
mediocre ones), and the note states the total against the 8-12 target rather than implying a small
bank is solid coverage (hard rule 6).

A canonical story with `operator_edited is False` is a model proposal from consolidation
(kestrel/interview/stories.py `consolidate_stories`) and is shown as a proposal to review. The
moment the operator saves it through the sheet, `operator_edited` is set True and that story is
theirs: `consolidate_stories` checks this flag and only ever attaches new sketches as usage to an
operator-edited story, never rewrites its text (the never-overwrite guarantee). Kestrel selects and
suggests; it never writes a story for the operator (hard rule 2)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Setting, Story, StoryUsage
from kestrel.interview import prep_state
from kestrel.interview.coverage import COMMON_THEMES
from kestrel.interview.stories import (
    delete_story,
    dismiss_draft,
    dismissal_token,
    undo_dismissal,
)
from kestrel.interview.story_task import request_build
from kestrel.web import prep_view as pv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.pages.prep import events, page_response, toast
from kestrel.web.redact import redact_text

router = APIRouter()

_STORY_FIELDS = ("situation", "task", "action", "result", "reflection")
# Which cards a change touches, by where the sheet or button was opened from.
_PARTS = {
    "pack": ("pp_questions", "pp_ready", "pp_tabs"),
    "bank": ("pp_bank", "pp_tabs"),
    "practice": ("pp_tabs",),
}
_FIRST_SENTENCE = re.compile(r"^(.+?[.!?])(\s|$)", re.S)


def _from(form_or_query) -> str:
    v = str(form_or_query.get("from") or "bank")
    return v if v in _PARTS else "bank"


def _derive_title(*texts: str) -> str:
    """A short name for a story the operator wrote without one: the first sentence of what they
    wrote, cut at 52 characters. Their words, not a summary."""
    for text in texts:
        t = " ".join((text or "").split())
        if t:
            m = _FIRST_SENTENCE.match(t)
            first = (m.group(1) if m else t).rstrip(".!?")
            return first[:52].rstrip()
    return ""


async def refresh(
    request: Request, kind: str, opportunity_id: int | None, *, message: str | None = None,
    undo_url: str | None = None, tone: str | None = None, close: bool = False,
):
    """Re-render only the cards a story change touched, as out-of-band swaps by id, and say what
    happened in a toast. `close` also closes the story sheet."""
    tab = {"pack": "pack", "practice": "practice"}.get(kind, "bank")
    async with sessionmaker()() as s:
        ctx = await pv.body_ctx(request, s, tab, opportunity_id)
    ctx.update(oob=True, parts=_PARTS[kind])
    ev: dict = {}
    if message:
        ev["k-toast"] = toast(message, undo_url, tone)
    if close:
        ev["pp-close-sheet"] = True
    return templates().TemplateResponse(
        request, "partials/pp_parts.html", ctx, headers=events(**ev) if ev else None
    )


def _opp(form) -> int | None:
    raw = str(form.get("opp") or "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


@router.get("/stories", response_class=HTMLResponse)
async def page(request: Request):
    return await page_response(request, None, "bank")


# ----------------------------------------------------------------------------- the sheet
@router.get("/stories/sheet", response_class=HTMLResponse)
async def sheet(request: Request):
    """The story sheet: three prompts in the operator's own words. Opened to add a story (for a
    question, a theme, a claim from practice, or the bank) or, with `story`, to edit one with its
    words as written. Kestrel will not rewrite what is saved here."""
    q = request.query_params
    story = None
    async with sessionmaker()() as s:
        if (q.get("story") or "").isdigit():
            story = await s.get(Story, int(q["story"]))
    theme = q.get("theme") if q.get("theme") in COMMON_THEMES else ""
    question = ""
    opp = int(q["opp"]) if (q.get("opp") or "").isdigit() else 0
    if opp and q.get("qh"):
        async with sessionmaker()() as s:
            question = await pv.question_for(s, opp, q["qh"]) or ""
            sh = await pv.shell_ctx(s, opp)
        question = redact_text(question, sh["emp"], enabled=sh["conf"]) or ""
    if story is not None:
        label = ("Merge: " if not story.operator_edited else "Edit: ") + story.title
    elif q.get("claim"):
        label = f"Claim: {q['claim'][:120]}"
    elif theme:
        label = f"Theme: {pv.THEME_WORDS[theme]}"
    elif question:
        label = question[:200]
    else:
        label = "Your story bank"
    ctx = {
        "story": story, "label": label, "theme": theme, "qh": q.get("qh") or "",
        "opp": q.get("opp") or "", "from": _from(q),
    }
    return templates().TemplateResponse(request, "partials/pp_story_sheet.html", ctx)


@router.get("/stories/{story_id}/delete-confirm", response_class=HTMLResponse)
async def delete_confirm(request: Request, story_id: int):
    async with sessionmaker()() as s:
        story = await s.get(Story, story_id)
    if story is None:
        return HTMLResponse("That story is already gone.", status_code=404)
    ctx = {"story": story, "from": _from(request.query_params),
           "opp": request.query_params.get("opp") or ""}
    return templates().TemplateResponse(request, "partials/pp_delete.html", ctx)


# ------------------------------------------------------------------------------- add / edit
@router.post("/stories", response_class=HTMLResponse)
async def add_story(request: Request):
    """Add a story by hand. It is the operator's own words from the first save, so it is created
    already edited (`operator_edited`), canonical, and out of reach of any later merge. A story
    started from a question or a theme is linked or tagged as asked."""
    form = await request.form()
    kind, opp = _from(form), _opp(form)
    fields = {f: str(form.get(f) or "").strip() or None for f in _STORY_FIELDS}
    title = str(form.get("title") or "").strip()[:200] or _derive_title(
        fields["situation"] or "", fields["action"] or "", fields["result"] or ""
    )
    if not title:
        return await refresh(
            request, kind, opp, tone="warn",
            message="Not added: give the story a title, or write what the situation was.",
        )
    theme = str(form.get("theme") or "")
    question, was = "", None
    async with sessionmaker()() as s:
        if opp and form.get("qh"):
            question = await pv.question_for(s, opp, str(form.get("qh"))) or ""
        story = Story(
            title=title, is_canonical=True, operator_edited=True,
            themes=[theme] if theme in COMMON_THEMES else [], **fields,
        )
        s.add(story)
        await s.commit()
        await s.refresh(story)
        stamp, linked = story.updated_at, False
        if question and opp:
            was = (await prep_state.story_choices(s, opp)).get(question)
            await prep_state.set_story_choice(s, opp, question, story.id)
            await s.commit()
            linked = True
    from urllib.parse import urlencode

    undo = "/stories/undo?" + urlencode({
        "id": story.id, "stamp": stamp, "opp": opp or 0, "q": question if linked else "",
        "was": (was or 0) if linked else 0,
    })
    return await refresh(
        request, kind, opp, undo_url=undo, close=True,
        message="Story saved and linked to this question." if linked
        else "Story saved to your story bank.",
    )


@router.post("/stories/build", response_class=HTMLResponse)
async def build_stories(request: Request):
    """Queue a build of draft stories from the operator's interview notes and the interview
    sketches in each scored role. Only a task row is written here; the worker does the model
    work (kestrel.interview.story_task), so this returns at once."""
    from kestrel.services.online_consent import consent_payload, consent_problem
    from kestrel.web.prep_view import build_ctx

    form = await request.form()
    async with sessionmaker()() as s:
        route = (await build_ctx(s))["build_route"]
        problem = consent_problem(route, form)  # an online Tier 2 sends notes: ask first
        queued = False if problem else await request_build(s, consent=consent_payload(route))
    return await refresh(
        request, "bank", _opp(form),
        message=problem or (
            "Building stories in the background. They appear here as drafts when it finishes."
            if queued else "A build is already running. Drafts appear here when it finishes."
        ),
        tone="warn" if problem else None,
    )


@router.get("/stories/notes/add")
async def add_notes(opportunity_id: int = 0):
    """Open a role's prep notes box. Adding notes happens on the role's prep page."""
    if opportunity_id <= 0:
        return RedirectResponse("/stories", status_code=303)
    return RedirectResponse(f"/prep/{opportunity_id}#prep-notes", status_code=303)


@router.post("/stories/undo", response_class=HTMLResponse)
async def undo_add(request: Request):
    """Undo a story just saved: unlink the question it was linked to and remove the story, but
    only while both are as that save left them. A story edited since is the operator's work and
    stays; the page is told this can no longer be undone."""
    q = request.query_params
    ok = False
    try:  # a hand-built link is refused, never a 500
        story_id, opp, was = int(q.get("id") or 0), int(q.get("opp") or 0), int(q.get("was") or 0)
    except ValueError:
        return HTMLResponse('<span data-undo-msg data-ok="0">This can no longer be undone.</span>')
    async with sessionmaker()() as s:
        story = await s.get(Story, story_id)
        if story is not None and q.get("stamp") and story.updated_at == q.get("stamp"):
            question = q.get("q") or ""
            choices = (
                await s.execute(
                    select(Setting).where(Setting.key.like(prep_state.STORY_PREFIX + "%"))
                )
            ).scalars()
            links = [
                (row.key, question_text)
                for row in choices
                for question_text, value in (row.value or {}).items()
                if value == story.id
            ]
            expected = [(f"{prep_state.STORY_PREFIX}{opp}", question)] if opp and question else []
            used = (
                await s.execute(select(StoryUsage.id).where(StoryUsage.story_id == story.id))
            ).first()
            if links != expected or used:
                return HTMLResponse(
                    '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
                )
            if opp and question:
                await prep_state.set_story_choice(s, opp, question, was or None)
            ok = await delete_story(s, story.id)
    return HTMLResponse(
        f'<span data-undo-msg data-ok="{1 if ok else 0}">'
        f"{'Undone.' if ok else 'This can no longer be undone.'}</span>"
    )


@router.post("/stories/{story_id}/dismiss", response_class=HTMLResponse)
async def dismiss_story(request: Request, story_id: int):
    """Keep separate: remove a proposed story and remember it, so Build stories does not propose
    it again. A story the operator saved is theirs and is deleted, not dismissed."""
    form = await request.form()
    token = dismissal_token()
    async with sessionmaker()() as s:
        done = await dismiss_draft(s, story_id, undo_token=token)
    return await refresh(
        request, _from(form), _opp(form),
        message=(
            "Draft dismissed. Build stories will not propose it again." if done
            else "That draft is already gone, or it is a story you saved."
        ),
        tone=None if done else "warn",
        undo_url=f"/stories/undo-dismiss/{token}" if done else None,
    )


@router.post("/stories/undo-dismiss/{token}", response_class=HTMLResponse)
async def restore_dismissed_story(request: Request, token: str):
    async with sessionmaker()() as s:
        ok = await undo_dismissal(s, token)
    return HTMLResponse(
        f'<span data-undo-msg data-ok="{1 if ok else 0}">'
        f'{"Undone." if ok else "This can no longer be undone."}</span>'
    )


@router.post("/stories/{story_id}/delete", response_class=HTMLResponse)
async def delete_saved_story(request: Request, story_id: int):
    form = await request.form()
    async with sessionmaker()() as s:
        done = await delete_story(s, story_id)
    return await refresh(
        request, _from(form), _opp(form), close=True,
        message="Story deleted." if done else "That story is already gone.",
        tone=None if done else "warn",
    )


@router.post("/stories/{story_id}", response_class=HTMLResponse)
async def edit_story(request: Request, story_id: int):
    """Save the sheet. The operator's rewrite persists byte-for-byte into the STAR + Reflection
    fields, and `operator_edited` is set True UNCONDITIONALLY on any save, even a save that changes
    nothing, because from this point the story is the operator's own words, not a model merge, and
    must never be silently overwritten by a later consolidation run."""
    form = await request.form()
    async with sessionmaker()() as s:
        story = await s.get(Story, story_id)
        if story is None:
            return HTMLResponse("not found", status_code=404)
        await _apply_edit(s, story, form)
    return await refresh(
        request, _from(form), _opp(form), close=True, message="Saved. Your words, unchanged."
    )


async def _apply_edit(s: AsyncSession, story: Story, form) -> None:
    if "title" in form:
        story.title = str(form["title"])
    for f in _STORY_FIELDS:
        if f in form:
            setattr(story, f, str(form[f]))
    story.operator_edited = True
    await s.commit()
