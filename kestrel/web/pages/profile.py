"""The Profile page (D-UI-21): who the operator is, what they want, and the documents that say
it. Documents is one tab; the others edit profile.yml through kestrel.services.profile_edit.
`/documents` keeps working and opens the Documents tab, so every existing link holds."""

from __future__ import annotations

import hashlib
import json
import re

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from kestrel.rules.margin import MARGIN_STOPS, normalize_margin
from kestrel.services import profile_edit as pe
from kestrel.services import profile_photo
from kestrel.web.deps import templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.documents import documents_context

log = structlog.get_logger()

router = APIRouter()

TABS = (
    ("about", "About you"),
    ("documents", "Documents"),
    ("employment", "Employment"),
    ("consulting", "Consulting"),
    ("location", "Location"),
    ("stories", "Stories"),
)
_TAB_KEYS = {k for k, _ in TABS}

# The near-floor margin control (D-RULES-2): Exact, then 2, 5, 10 and 15 percent.
MARGIN_OPTIONS = tuple(
    (str(pct), "Exact" if pct == 0 else f"{pct}%") for pct in MARGIN_STOPS
)


def _tabs() -> tuple[tuple[str, str], ...]:
    """The tabs shown. With consulting switched off (D-UI-28) its tab is not rendered; the
    /profile?tab=consulting route still works, so a saved link or the data is never lost."""
    from kestrel.services.tracks import consulting_enabled

    return TABS if consulting_enabled() else tuple(t for t in TABS if t[0] != "consulting")


def _track(request: Request, tab: str | None = None) -> str:
    """The track a tab edits. Employment and Consulting are their own track's terms whatever
    the query says (a stray track= would render blank terms and save them, reviewer finding 1);
    About you and Location follow the track switch, the same one the nav shows (D-UI-66)."""
    if tab in ("employment", "consulting"):
        return tab
    # Otherwise the active tab's track (D-UI-66): the nav switch and this page read the same
    # resolver (an explicit ?track=, then the remembered tab), so they never disagree.
    from kestrel.web.deps import active_track

    t = active_track(request)
    return t if t in pe.TRACKS else "employment"


def _section_ctx(track: str, state: pe.ProfileState | None = None) -> dict:
    state = state or pe.load_state()
    block = state.tracks[track]
    example = state.examples.get(track) or {}
    ex_geo = example.get("geo_rules") or {}
    return {
        "track": track, "profile_exists": state.exists, "profile_problem": state.problem,
        "block": block,
        # Work mode is the one answer to "remote or hybrid?"; Location reads it (D-UI-214).
        "terms": {**(block.get("employment_terms") or {}),
                  "work_modes": pe.effective_modes(block, "employment")},
        "cterms": {**(block.get("consulting_terms") or {}),
                   "work_modes": pe.effective_modes(block, "consulting")},
        "geo": pe.effective_geo(block, track),
        "metros_text": pe.metros_text(block),
        "about_missing": pe.about_missing(state),
        "from_example": state.from_example.get(track, ()),
        # The shipped example's values, shown ONLY as placeholder hints (release review 1).
        "ex_terms": example.get("employment_terms") or {},
        "ex_cterms": example.get("consulting_terms") or {},
        "ex_geo": ex_geo,
        "ex_metros_text": pe.metros_text({"geo_rules": ex_geo}),
        "seniority_floors": pe.SENIORITY_FLOORS, "employment_types": pe.EMPLOYMENT_TYPES,
        # Stored values read as one of the stops; a profile with none shows the default, 5.
        "margin_options": MARGIN_OPTIONS,
        "comp_margin": str(normalize_margin((block.get("employment_terms") or {}).get(
            pe.EMPLOYMENT_MARGIN))),
        "rate_margin": str(normalize_margin((block.get("consulting_terms") or {}).get(
            pe.CONSULTING_MARGIN))),
        "work_modes": pe.WORK_MODES,
        "photo_version": profile_photo.version(),
        "initials": _initials(block),
    }


def _initials(block: dict) -> str:
    """The two letters on the avatar: first and last name, else the first letter alone."""
    first = str(block.get("first_name") or "").strip()[:1]
    last = str(block.get("last_name") or "").strip()[:1]
    return (first + last).upper() or "?"


async def _story_count() -> int:
    """How many stories the bank holds, for the Stories tab. 0 when it cannot be read."""
    from sqlalchemy import func, select
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.models import Story
    from kestrel.web.deps import sessionmaker

    try:
        async with sessionmaker()() as s:
            return int((await s.execute(
                select(func.count()).select_from(Story).where(Story.is_canonical.is_(True))
            )).scalar_one())
    except SQLAlchemyError as exc:
        log.warning("story_count_failed", error_type=type(exc).__name__)
        return 0


async def _confidential_ctx(note: str | None = None, problem: str | None = None) -> dict:
    """The About you privacy card: the existing confidential-mode setting and the current
    employer with its "Show it as" text (the marks the Documents role editor writes)."""
    from kestrel.docs.current_employer import current_employers, suggested_placeholder
    from kestrel.web.deps import sessionmaker
    from kestrel.web.pages.documents import _confidential

    async with sessionmaker()() as s:
        on = await _confidential(s)
        employers = await current_employers(s)
    first = employers[0] if employers else None
    return {"conf": {
        "on": on, "employer": first["employer"] if first else "",
        "shown": first["shown_as"] if first else suggested_placeholder("employment"),
        "inferred": bool(first and first["inferred"]), "note": note, "problem": problem}}


async def readiness_ctx(request: Request, track: str) -> dict:
    """What the next-step notice (partials/profile_next_step.html) prints, for any page that
    renders profile.html."""
    from kestrel.services.profile_readiness import load_readiness
    from kestrel.web.deps import sessionmaker
    from kestrel.web.pages.today import _rescoring

    async with sessionmaker()() as s:
        readiness = await load_readiness(s, track)
    return {"readiness": readiness, "rescoring": _rescoring(request)}


@router.get("/profile", response_class=HTMLResponse)
async def profile(request: Request):
    tab = request.query_params.get("tab") or "about"
    if tab not in _TAB_KEYS:
        tab = "about"
    if tab in ("employment", "consulting") and request.query_params.get("track") != tab:
        # The Employment and Consulting tabs are the track switch (D-UI-66): the page always
        # names its track, so the nav, which reads ?track= first, shows the same one.
        from fastapi.responses import RedirectResponse

        return RedirectResponse(f"/profile?tab={tab}&track={tab}", status_code=303)
    ctx = {
        "title": "Profile", "query": dict(request.query_params), "tab": tab, "tabs": _tabs(),
        **_section_ctx(_track(request, tab)),
    }
    if tab == "documents":
        ctx.update(await documents_context(request))
    if tab == "about":
        ctx.update(await _confidential_ctx())
    if tab == "stories":
        ctx.update(await _stories_ctx())
    ctx["story_count"] = await _story_count()
    ctx.update(await readiness_ctx(request, ctx["track"]))
    return templates().TemplateResponse(request, "profile.html", ctx)


@router.post("/api/profile/score-again")
async def score_again(request: Request):
    """Queue the roles scored before the profile's proof points for scoring again (issue #67).
    The list is read here again, never taken from the form, and `enqueue_rescore` is idempotent,
    so a second press queues nothing new. Plain redirect back; nothing is sent anywhere new:
    an online model still needs the operator's confirmation, which this route cannot give."""
    from fastapi.responses import RedirectResponse

    from kestrel.queue.rescore import enqueue_rescore
    from kestrel.services.online_consent import approval_problem
    from kestrel.services.profile_readiness import load_readiness
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.deps import sessionmaker

    form = await request.form()
    track = str(form.get("track") or "employment")
    back = str(form.get("back") or "/")
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    sep = "&" if "?" in back else "?"
    queued = 0
    async with sessionmaker()() as s:
        readiness = await load_readiness(s, track)
        if readiness.role_ids:
            settings = await load_settings_with_db(s)
            if await approval_problem(s, settings, ("tier1", "tier2"), {}):
                return RedirectResponse(f"{back}{sep}rescore=confirm", status_code=303)
            result = await enqueue_rescore(s, readiness.role_ids)
            await s.commit()
            queued = len(result.queued) + len(result.already)
    return RedirectResponse(
        f"{back}{sep}rescoring={queued}" if queued else back, status_code=303
    )


async def _queue_recheck(tracks: list[str]) -> int:
    """After a save: queue the Tier 0 re-check when the rules' inputs changed and return how many
    set-aside roles it will look at again. Never fails the save."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.pipeline import recheck

    total = 0
    for track in tracks:
        try:
            total += await recheck.queue_after_save(get_sessionmaker(), track)
        except Exception as exc:  # a profile save must not fail on its side effect
            import structlog

            structlog.get_logger().warning("tier0_recheck_queue_failed", track=track,
                                           error=str(exc))
    return total


async def _rebuild_searches() -> bool:
    """Rebuild the profile-generated searches after a save (D-SRC-2, silent by the operator's
    choice: the save row says when they changed). Never fails the save."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.generated import rebuild_generated_sources

    try:
        async with get_sessionmaker()() as s:
            result = await rebuild_generated_sources(s)
            await s.commit()
        return result.changed
    except Exception as exc:  # a profile save must not fail on its side effect
        import structlog

        structlog.get_logger().warning("generated_sources_rebuild_failed", error=str(exc))
        return False


# The routes below sit before the two-segment save route on purpose: POST /profile/photo/delete
# and POST /profile/stories/build would otherwise be read as a track and a section.
# ------------------------------------------------------------------------------------ photo
def _photo_response(request: Request, track: str, problem: str | None = None):
    ctx = {**_section_ctx(track), "photo_problem": problem}
    page = templates().get_template("partials/profile_photo.html").render(
        {**ctx, "request": request})
    # The top bar shows the same picture: swapped out of band so it changes with the card, with
    # no page change (O18). A refused upload changed nothing, so it sends nothing.
    top = "" if problem else (
        templates().get_template("partials/topbar_avatar.html").render({"oob": True}))
    return HTMLResponse(page + top)


@router.get("/ui/avatar", response_class=HTMLResponse)
async def top_avatar_fragment(request: Request):
    """The top bar avatar as it is now: a page that changed the picture without htmx (Settings)
    fetches this and swaps it in place."""
    return templates().TemplateResponse(request, "partials/topbar_avatar.html", {})


@router.get("/profile/photo")
async def photo_file():
    """The photo, for the avatar on About you. It stays on this computer."""
    path = profile_photo.current()
    if path is None:
        return HTMLResponse("No photo.", status_code=404)
    return FileResponse(
        path, media_type=profile_photo.TYPES[path.suffix.lstrip(".")],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )


async def _set_avatar(value: str) -> None:
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import ui_state

    async with get_sessionmaker()() as s:
        await ui_state.set_value(s, "ui.avatar", value)
        await s.commit()


@router.post("/profile/photo", response_class=HTMLResponse)
async def photo_upload(request: Request):
    """Keep a picked picture as the photo (local file only; nothing is sent anywhere)."""
    track = _track(request)
    form = await request.form()
    upload = form.get("photo")
    if upload is None or not hasattr(upload, "read"):
        return _photo_response(request, track, "Choose a picture first.")
    data = await upload.read(profile_photo.MAX_BYTES + 1)
    try:
        await profile_photo.save_async(data)
    except profile_photo.PhotoError as exc:
        return _photo_response(request, track, plain_error(exc))
    await _set_avatar("photo")
    return _photo_response(request, track)


@router.post("/profile/photo/delete", response_class=HTMLResponse)
async def photo_remove(request: Request):
    profile_photo.remove()
    await _set_avatar("initial")
    return _photo_response(request, _track(request))


# ------------------------------------------------------------------- confidential mode
async def apply_confidential(on: bool, employer: str, shown: str) -> tuple[str | None, str | None]:
    """Save the confidential-mode switch and, when an employer is named, mark it as the current
    employer with its "Show it as" text. Returns (note, problem). Shared by the About you card and
    Settings > Privacy and access, so the two can never disagree."""
    from sqlalchemy import select

    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import DocMaster, DocSection
    from kestrel.docs.current_employer import (
        EMPLOYER_KINDS,
        _norm,
        set_current_employer,
        suggested_placeholder,
    )
    from kestrel.web.pages.settings import _set

    problem: str | None = None
    note = "Saved."
    async with get_sessionmaker()() as s:
        await _set(s, "confidential_mode", on)
        if employer:
            rows = (await s.execute(
                select(DocSection).join(DocMaster, DocMaster.id == DocSection.doc_master_id)
                .where(DocSection.section_kind.in_(EMPLOYER_KINDS))
                .order_by(DocMaster.id.desc(), DocSection.sort_order, DocSection.id)
            )).scalars().all()
            match = next((r for r in rows if _norm(r.org_label) == _norm(employer)), None)
            if match is None:
                problem = (f"No role at {employer} in your documents. Add that role on the "
                           "Documents tab first, then come back.")
            else:
                await set_current_employer(
                    s, match, shown or suggested_placeholder("employment"))
        await s.commit()
    if problem is None and on and not employer:
        note = "Saved. Type your current employer so Kestrel knows which one to hide."
    return (note if problem is None else None), problem


@router.post("/profile/confidential", response_class=HTMLResponse)
async def confidential_save(request: Request):
    """The About you privacy card. The switch is the existing confidential-mode setting; the
    employer and its "Show it as" label are the same marks the Documents role editor writes
    (kestrel.docs.current_employer), so the two places can never disagree."""
    from kestrel.docs.current_employer import clean_placeholder

    form = await request.form()
    on = "on" in form.getlist("conf_mode")
    employer = " ".join(str(form.get("conf_employer") or "").split())[:200]
    shown = clean_placeholder(form.get("conf_shown"))
    note, problem = await apply_confidential(on, employer, shown)
    return templates().TemplateResponse(
        request, "partials/profile_confidential.html",
        await _confidential_ctx(note=note, problem=problem),
    )


# ----------------------------------------------------------------------------------- stories
async def _stories_ctx(
    notice: str | None = None, error: str | None = None, undo_url: str | None = None
) -> dict:
    """The Stories tab: the same canonical story bank as /stories and Interview prep. Every
    word shown is the person's own; a draft the model proposed is marked as one."""
    from kestrel.web.deps import sessionmaker
    from kestrel.web.prep_view import build_ctx as _build_ctx
    from kestrel.web.prep_view import canonical_stories as _canonical_stories

    async with sessionmaker()() as s:
        stories = await _canonical_stories(s)
        build = await _build_ctx(s)
    return {"stories": stories, "story_notice": notice, "story_error": error,
            "story_undo_url": undo_url, **build}


def _stories_response(request: Request, ctx: dict):
    return templates().TemplateResponse(request, "partials/profile_stories_list.html", ctx)


@router.post("/profile/stories", response_class=HTMLResponse)
async def story_add(request: Request):
    """Add a story by hand: it is the person's own text from the first save, so it is created
    already edited and no later merge can touch it (same as /stories)."""
    from kestrel.db.models import Story
    from kestrel.web.deps import sessionmaker

    form = await request.form()
    title = str(form.get("title") or "").strip()[:200]
    if not title:
        return _stories_response(
            request, await _stories_ctx(error="Not added: give the story a title."))
    async with sessionmaker()() as s:
        s.add(Story(
            title=title, is_canonical=True, operator_edited=True, themes=[],
            **{f: str(form.get(f) or "").strip() or None
               for f in ("situation", "action", "result")},
        ))
        await s.commit()
    return _stories_response(request, await _stories_ctx(
        notice="Story added. It is yours; nothing rewrites it."))


@router.post("/profile/stories/build", response_class=HTMLResponse)
async def stories_build(request: Request):
    from kestrel.interview.story_task import request_build
    from kestrel.services.online_consent import consent_payload, consent_problem
    from kestrel.web.deps import sessionmaker
    from kestrel.web.prep_view import build_ctx

    form = await request.form()
    async with sessionmaker()() as s:
        route = (await build_ctx(s))["build_route"]
        problem = consent_problem(route, form)  # an online Tier 2 sends notes: ask first
        queued = False if problem else await request_build(s, consent=consent_payload(route))
    if problem:
        return _stories_response(request, await _stories_ctx(error=problem))
    return _stories_response(request, await _stories_ctx(notice=(
        "Building stories in the background. They appear here as drafts when it finishes."
        if queued else "A build is already running. Drafts appear here when it finishes.")))


@router.post("/profile/stories/dismiss-all", response_class=HTMLResponse)
async def story_dismiss_all(request: Request):
    """Clear all drafts (#65): dismiss every proposed story so Build never proposes it again. A
    story the operator saved is never touched. The button asks first (hx-confirm)."""
    from kestrel.interview.stories import dismiss_all_drafts
    from kestrel.web.deps import sessionmaker

    async with sessionmaker()() as s:
        count, group = await dismiss_all_drafts(s)
    notice = (
        f"Cleared {count} draft{'' if count == 1 else 's'}. Build stories will not propose them "
        "again." if count else None
    )
    return _stories_response(request, await _stories_ctx(
        notice=notice, error=None if count else "There are no drafts to clear.",
        undo_url=f"/profile/stories/undo-dismiss-all/{group}" if group else None))


@router.post("/profile/stories/undo-dismiss-all/{group}", response_class=HTMLResponse)
async def story_undo_dismiss_all(request: Request, group: str):
    """Undo Clear all drafts from Profile: the same ten-minute undo as the bank, then the list."""
    from kestrel.interview.stories import undo_dismissal_group
    from kestrel.web.deps import sessionmaker

    async with sessionmaker()() as s:
        ok = await undo_dismissal_group(s, group)
    return _stories_response(request, await _stories_ctx(
        notice="Drafts restored." if ok else None,
        error=None if ok else "This can no longer be undone."))


@router.post("/profile/stories/{story_id}/delete", response_class=HTMLResponse)
async def story_delete(request: Request, story_id: int):
    """Delete a saved story, or dismiss a draft so Build never proposes it again."""
    from kestrel.db.models import Story
    from kestrel.interview.stories import delete_story, dismiss_draft
    from kestrel.web.deps import sessionmaker

    async with sessionmaker()() as s:
        story = await s.get(Story, story_id)
        if story is None:
            done = False
        elif story.operator_edited:
            done = await delete_story(s, story_id)
        else:
            done = await dismiss_draft(s, story_id)
    return _stories_response(request, await _stories_ctx(
        notice="Removed." if done else None,
        error=None if done else "That story is already gone."))


@router.post("/profile/{track}/{section}", response_class=HTMLResponse)
async def profile_save(request: Request, track: str, section: str):
    """Save one tab of one track. The partial re-renders with "Saved" or the reason it was
    refused; a refused save changes nothing on disk."""
    if track not in pe.TRACKS or section not in pe.SECTIONS:
        return HTMLResponse("Unknown profile section.", status_code=404)
    if section in ("employment", "consulting") and track != section:
        return HTMLResponse("That section belongs to the other track.", status_code=404)
    form = await request.form()
    problem: str | None = None
    searches_updated = False
    rechecking = 0
    try:
        values = pe.section_from_form(section, form)
        pe.save_section(track, section, values)
        searches_updated = await _rebuild_searches()
        rechecking = await _queue_recheck([track])
    except pe.ProfileEditError as exc:
        problem = plain_error(exc)
    except OSError as exc:
        log.warning("profile_write_failed", error_type=type(exc).__name__, error=str(exc))
        problem = f"Could not write profile.yml: {plain_error(exc)}"
    ctx = {**_section_ctx(track), "saved": problem is None, "problem": problem,
           "searches_updated": searches_updated, "rechecking": rechecking}
    resp = templates().TemplateResponse(request, f"partials/profile_{section}.html", ctx)
    if problem is not None:
        # Keep the actual controls, including edits made while this request was in flight.
        resp.headers["HX-Reswap"] = "none"
        resp.headers["HX-Trigger"] = json.dumps({"pf-save-refused": {"message": problem}})
    else:
        # The toast offers Undo, which restores profile.yml.bak (D-UI-119). The variants list
        # follows an Employment save (D-TTL-2).
        # profile.js shows the toast: it owns the Undo so it can say plainly when Undo fails.
        events: dict = {"pf-saved": {"message": "Saved", "undoUrl": "/profile/undo",
                                     "token": pe.undo_token()}}
        if section == "employment":
            events["titleVariantsChanged"] = True
        resp.headers["HX-Trigger"] = json.dumps(events)
    return resp


@router.post("/profile/undo", response_class=HTMLResponse)
async def profile_undo(request: Request):
    """Undo of the last save: restore profile.yml.bak, then rebuild what the profile feeds.
    Undo goes back one save: a toast from an earlier save carries a stamp the file no longer
    has, and is refused rather than restoring the wrong version."""
    token = str((await request.form()).get("token") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        return HTMLResponse(
            "Undo needs the Undo button on the Saved message. Nothing was changed.",
            status_code=409)
    if token != pe.undo_token():
        return HTMLResponse(
            "Undo is no longer available because you saved again. "
            "Your saved profile is unchanged.", status_code=409)
    try:
        pe.restore_backup()
    except pe.NoEarlierVersion:
        return HTMLResponse(pe.NO_EARLIER_VERSION, status_code=409)
    except pe.EarlierVersionUnreadable:
        return HTMLResponse(pe.EARLIER_VERSION_UNREADABLE, status_code=409)
    except pe.ProfileEditError:  # not raised today; kept so a new refusal is still a 409
        return HTMLResponse("The earlier version could not be restored.", status_code=409)
    except OSError as exc:
        log.warning("profile_undo_failed", error_type=type(exc).__name__, error=str(exc))
        return HTMLResponse("Could not restore the earlier version.", status_code=500)
    await _rebuild_searches()
    await _queue_recheck(list(pe.TRACKS))
    return HTMLResponse("", status_code=204)


# ------------------------------------------------------------------ near-title search variants
# D-TTL-2: the other phrasings of the target titles the generated searches also ask for, listed
# under Profile > Employment. Each can be removed (stored, never searched again) or put back.
# The writes live at /title-variants/..., not under /profile/, where POST /profile/{track}/
# {section} would take them.
async def _variants_ctx() -> dict:
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.generated import variants_view

    async with get_sessionmaker()() as s:
        return {"variants": await variants_view(s)}


@router.get("/profile/title-variants", response_class=HTMLResponse)
async def title_variants(request: Request):
    return templates().TemplateResponse(
        request, "partials/profile_title_variants.html", await _variants_ctx()
    )


@router.post("/title-variants/{action}", response_class=HTMLResponse)
async def title_variant_change(request: Request, action: str):
    """Remove one variant from the searches, or put a removed one back, then rebuild the
    generated searches so the change applies on the next run."""
    if action not in ("remove", "restore"):
        return HTMLResponse("Unknown action.", status_code=404)
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.generated import removed_variants, save_removed_variants

    form = await request.form()
    value = " ".join(str(form.get("variant") or "").split())[:200]
    if value:
        async with get_sessionmaker()() as s:
            gone = await removed_variants(s)
            if action == "remove":
                gone.append(value)
            else:
                gone = [g for g in gone if g.casefold() != value.casefold()]
            await save_removed_variants(s, gone)
            await s.commit()
        await _rebuild_searches()
    return templates().TemplateResponse(
        request, "partials/profile_title_variants.html", await _variants_ctx()
    )


# ----------------------------------------------------------------- suggested target titles
# P15 (D-TTL-3, #31): the list of titles the model suggested from the resume. Accept is the only
# way a suggestion reaches the profile. The writes live at /title-suggestions/..., not under
# /profile/, where POST /profile/{track}/{section} would take them.
async def _suggestions_response(
    request: Request, note: str | None = None, *, accepted_title: str | None = None
):
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import title_suggest

    async with get_sessionmaker()() as s:
        ctx = {**(await title_suggest.view(s)), "note": note, "route": await _suggest_route(s)}
    # Poll persisted state, so completions from any worker survive reconnects. Unchanged
    # cards are left alone, preserving open details and online confirmation controls.
    ctx["version"] = hashlib.sha256(json.dumps(
        {k: v for k, v in ctx.items() if k != "note"}, sort_keys=True,
    ).encode()).hexdigest()
    if request.method == "GET" and request.query_params.get("version") == ctx["version"]:
        return HTMLResponse(status_code=204)
    resp = templates().TemplateResponse(request, "partials/profile_title_suggestions.html", ctx)
    if accepted_title is not None:
        resp.headers["HX-Trigger"] = json.dumps({
            "pf-title-accepted": {"title": accepted_title}, "titleVariantsChanged": True,
        })
    return resp


_SUGGEST_SENDS = "your resume text, your target titles and the titles of roles you pursued"


async def _suggest_route(session) -> dict:
    """Where Suggest again would be answered: on this computer, or by the online Tier 2 model,
    in which case the ask-first panel states what is sent."""
    from kestrel.services.online_consent import online_route
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.services.title_suggest import PROMPT, tier_of

    return await online_route(
        session, await load_settings_with_db(session), tier_of(PROMPT),
        this_send=_SUGGEST_SENDS,
    )


@router.get("/profile/title-suggestions", response_class=HTMLResponse)
async def title_suggestions(request: Request):
    return await _suggestions_response(request)


@router.post("/title-suggestions/refresh", response_class=HTMLResponse)
async def title_suggestions_refresh(request: Request):
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import title_suggest
    from kestrel.services.online_consent import consent_payload, consent_problem

    note: str | None
    form = await request.form()
    try:
        async with get_sessionmaker()() as s:
            route = await _suggest_route(s)
            if await title_suggest.employment_resume(s) is None:
                note = "Import a resume first. Suggestions are read from it."
            elif problem := consent_problem(route, form):
                # An online Tier 2 sends the resume: the server asks again, whatever the page said.
                note = problem
            elif await title_suggest.request_title_suggestions(
                s, "manual", consent=consent_payload(route)
            ):
                await s.commit()
                note = "Queued. New suggestions appear after the next background pass."
            else:
                note = "A pass is already queued."
    except SQLAlchemyError as exc:
        log.warning("title_suggest_queue_failed", error_type=type(exc).__name__)
        note = "Could not queue that. Try again."
    return await _suggestions_response(request, note)


@router.post("/title-suggestions/{suggestion_id}/accept", response_class=HTMLResponse)
async def title_suggestion_accept(request: Request, suggestion_id: int):
    """Add the title to the target titles. The generated searches and the Tier 0 re-check follow
    the change, as after an Employment save. Never a 500: a refusal is a line on the list."""
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import title_suggest

    try:
        async with get_sessionmaker()() as s:
            ok, message = await title_suggest.accept(s, suggestion_id)
            row = await s.get(title_suggest.TitleSuggestion, suggestion_id) if ok else None
            accepted_title = row.title if row is not None else None
    except SQLAlchemyError as exc:
        log.warning("title_accept_failed", error_type=type(exc).__name__)
        return await _suggestions_response(request, "Could not save that. Try again.")
    if not ok:
        return await _suggestions_response(request, message)
    await _rebuild_searches()
    await _queue_recheck(["employment"])
    return await _suggestions_response(request, message, accepted_title=accepted_title)


@router.post("/title-suggestions/{suggestion_id}/dismiss", response_class=HTMLResponse)
async def title_suggestion_dismiss(request: Request, suggestion_id: int):
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import title_suggest

    try:
        async with get_sessionmaker()() as s:
            await title_suggest.dismiss(s, suggestion_id)
    except SQLAlchemyError as exc:
        log.warning("title_dismiss_failed", error_type=type(exc).__name__)
        return await _suggestions_response(request, "Could not save that. Try again.")
    return await _suggestions_response(request)
