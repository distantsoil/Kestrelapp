"""The Profile page (D-UI-21): who the operator is, what they want, and the documents that say
it. Documents is one tab; the others edit profile.yml through kestrel.services.profile_edit.
`/documents` keeps working and opens the Documents tab, so every existing link holds."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kestrel.services import profile_edit as pe
from kestrel.web.deps import templates
from kestrel.web.pages.documents import documents_context

router = APIRouter()

TABS = (
    ("about", "About you"),
    ("documents", "Documents"),
    ("employment", "Employment"),
    ("consulting", "Consulting"),
    ("location", "Location"),
)
_TAB_KEYS = {k for k, _ in TABS}


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
        "terms": block.get("employment_terms") or {},
        "cterms": block.get("consulting_terms") or {},
        "geo": block.get("geo_rules") or {},
        "metros_text": pe.metros_text(block),
        "about_missing": pe.about_missing(state),
        "from_example": state.from_example.get(track, ()),
        # The shipped example's values, shown ONLY as placeholder hints (release review 1).
        "ex_terms": example.get("employment_terms") or {},
        "ex_cterms": example.get("consulting_terms") or {},
        "ex_geo": ex_geo,
        "ex_metros_text": pe.metros_text({"geo_rules": ex_geo}),
        "seniority_floors": pe.SENIORITY_FLOORS, "employment_types": pe.EMPLOYMENT_TYPES,
    }


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
    return templates().TemplateResponse(request, "profile.html", ctx)


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
        problem = str(exc)
    except OSError as exc:
        problem = f"Could not write profile.yml: {exc}"
    ctx = {**_section_ctx(track), "saved": problem is None, "problem": problem,
           "searches_updated": searches_updated, "rechecking": rechecking}
    resp = templates().TemplateResponse(request, f"partials/profile_{section}.html", ctx)
    if section == "employment" and problem is None:
        resp.headers["HX-Trigger"] = "titleVariantsChanged"  # the variants list follows (D-TTL-2)
    return resp


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
