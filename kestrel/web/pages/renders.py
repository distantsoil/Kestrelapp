"""Render review page (SPEC-INTERFACES.md lines 216-218). Chosen variant beside the alternatives
per slot, with the model's rationale; one-click swap that WRITES a `selection_override` row before
mutating the config (the gate item — this is the audit trail the learning loop reads to see every
place the operator corrected the model's document selection, SPEC-MODULES 5); a live HTML preview
built the same way the PDF was (resolve() then render_html(), byte-for-byte from doc_variant.text,
hard rule 2); a page-count warning; download; and attach-to-pursuit.

/renders/new is the entry point linked from the Documents page's "Test render" button: it creates a
DocRender via kestrel.docs.service.render_document and redirects here. Playwright/render failures
(no Edge on this machine, e.g. a cloud agent) are caught and shown inline, never a 500 — PDF
generation is local-required (CLAUDE.md environment split)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Opportunity,
    Org,
    Pursuit,
    SelectionOverride,
)
from kestrel.docs.catalog import (
    MasterCatalog,
    load_catalog,
    operator_slot_keys,
    redact_skill_groups,
    resolve,
)
from kestrel.docs.render import render_html
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()


def _look_params(params) -> dict:
    """`font`, `header_align` and `accent` from a query string or form, kept only when they name
    a listed value or a valid #rrggbb (kestrel.docs.styles), so nothing else can reach a render
    config's look."""
    from kestrel.docs.styles import ALIGNS, FONTS, accent_from_params

    out: dict = {}
    font = str(params.get("font") or "")
    if font in FONTS:
        out["font"] = font
    align = str(params.get("header_align") or "")
    if align in ALIGNS:
        out["header_align"] = align
    accent, _error = accent_from_params(params)
    if accent:
        out["accent"] = accent
    return out


def back_target(params) -> str | None:
    """Where the Back link goes: the `from` query parameter when it is one of this app's own
    pages (a role or the Applications list), else None for Documents. Anything else, including
    an absolute or protocol-relative URL, is ignored."""
    raw = str(params.get("from") or "")
    if re.fullmatch(r"/opportunities/\d+", raw) or raw in ("/pursuits", "/pursuits/"):
        return raw
    return None


def request_back(request: Request, form=None) -> str | None:
    """The Back target for a request on the review page: the query's `from`, else a posted
    `from` field, else the `from` on the page the HTMX request came from (HX-Current-URL), so a
    swap or a look change never loses the way back to the role or the board."""
    from urllib.parse import parse_qs, urlsplit

    back = back_target(request.query_params) or (back_target(form) if form is not None else None)
    if back:
        return back
    current = request.headers.get("HX-Current-URL") or ""
    values = parse_qs(urlsplit(current).query).get("from") or []
    return back_target({"from": values[0]}) if values else None


def _slot_rows(catalog: MasterCatalog, config: dict, rationale: dict) -> list[dict]:
    """Per slot: the chosen variant, the alternatives (everything else, so un-hiding is always
    possible), and the model's one-sentence rationale for that slot key (PROMPTS.md doc_select/v1's
    `rationale` output is an object keyed by slot key). Always the operator's REAL text: this is
    an editor (its "Edit wording" box saves what it shows), so confidential mode redacts only the
    preview and the PDF (D-PRIV-1)."""
    chosen = (config or {}).get("slots") or {}
    rows = []
    for sec in catalog.sections:
        for sl in sec.slots:
            key = chosen.get(sl.key, sl.default_variant_key)
            rows.append(
                {
                    "slot": sl,
                    # The operator's name for this part of the resume: the role's title when the
                    # slot sits under one, else the slot key spoken plainly.
                    "part": (sec.section.title or sl.key.replace("_", " ")).strip(),
                    "key_words": sl.key.replace("_", " "),
                    "chosen_key": key,
                    "chosen": next((v for v in sl.variants if v.key == key), None),
                    "alternatives": [v for v in sl.variants if v.key != key],
                    "n_wordings": len(sl.variants),
                    "all_wordings": list(sl.variants),
                    "hidden": key == "hide",
                    "rationale": (rationale or {}).get(sl.key),
                }
            )
    return rows


def _summary_row(
    catalog: MasterCatalog, config: dict, scrub: Callable[[str], str] | None = None
) -> dict | None:
    """The summary as a part of its own, listed first: the chosen wording, the others, and
    whether it is hidden for this render. Summary wordings are the operator's text in
    DocMaster.summary_variants, chosen by key like a bullet. `scrub` is confidential mode's
    employer substitution (org_redactor), applied to every wording shown."""
    show = scrub or (lambda text: text)
    variants = [
        {"key": str(v.get("key")), "text": show(str(v.get("text") or ""))}
        for v in catalog.summary_variants if v.get("key")
    ]
    if not variants:
        return None
    key = (config or {}).get("summary")
    hidden = bool((config or {}).get("hide_summary"))
    chosen = next((v for v in variants if v["key"] == key), None)
    return {
        "chosen": chosen, "chosen_key": key,
        "alternatives": [v for v in variants if chosen is None or v["key"] != chosen["key"]],
        "n_wordings": len(variants), "all_wordings": variants, "hidden": hidden,
    }


def _skills_row(
    catalog: MasterCatalog, config: dict, scrub: Callable[[str], str] | None = None
) -> dict | None:
    """The skills groups as one part: what prints, and whether it is hidden for this render.
    `scrub` is confidential mode's employer substitution, as in the PDF."""
    groups = catalog.skill_groups
    if not groups:
        return None
    if scrub is not None:
        groups = redact_skill_groups(list(groups), scrub)
    visible = (config or {}).get("skill_groups") or {}
    shown = [
        f"{g.get('label') or g.get('key')}: {', '.join(g.get('items') or [])}"
        for g in groups if visible.get(g.get("key"), g.get("default_visible", True))
    ]
    return {"text": " \u00b7 ".join(shown), "hidden": bool((config or {}).get("hide_skills"))}


async def _build_ctx(
    request: Request, session: AsyncSession, render: DocRender | None, settings: Settings
) -> dict:
    ctx: dict = {
        "title": "Tailor resume",
        "query": dict(request.query_params),
        "back": request_back(request),
        "render": render,
        "master": None,
        "opportunity": None,
        "org_name": None,
        "pursuit": None,
        "slot_rows": [],
        "summary_row": None,
        "skills_row": None,
        "preview_html": None,
        "unresolved": [],
        "catalog_missing": False,
        "over_page_limit": False,
        "max_pages": settings.docs.max_pages,
    }
    if render is None:
        return ctx
    ctx["master"] = await session.get(DocMaster, render.doc_master_id)
    opp = await session.get(Opportunity, render.opportunity_id) if render.opportunity_id else None
    ctx["opportunity"] = opp
    if opp is not None:
        org = await session.get(Org, opp.org_id) if opp.org_id else None
        ctx["org_name"] = (org.name if org else None) or opp.org_name
        pursuit = (
            await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
        ).scalar_one_or_none()
        ctx["pursuit"] = pursuit
    catalog = await load_catalog(session, render.doc_master_id)
    if catalog is None:
        ctx["catalog_missing"] = True
        return ctx
    # Confidential mode redacts the current employer everywhere the operator's text is shown,
    # the Tailor rows and the live preview included, not only the PDF.
    view_config = dict(render.config or {})
    view_config["redact_orgs"] = bool(view_config.get("redact_orgs") or settings.confidential_mode)
    # The rows are the operator's editor: real text, saved as shown (D-PRIV-1). Only the preview
    # below (and the PDF) is redacted.
    ctx["slot_rows"] = _slot_rows(catalog, render.config or {}, render.rationale or {})
    ctx["summary_row"] = _summary_row(catalog, view_config)
    ctx["skills_row"] = _skills_row(catalog, view_config)
    doc = resolve(catalog, view_config)
    ctx["preview_html"] = render_html(doc)
    from kestrel.docs.styles import ACCENT_PRESETS, FONTS

    config = render.config or {}
    ctx["look"] = {
        "layouts": [lay.key for lay in catalog.layouts], "fonts": list(FONTS),
        "layout": config.get("layout") or "", "font": config.get("font") or "",
        "header_align": config.get("header_align") or "", "effective": doc.style,
        "accent": config.get("accent") or "", "accents": ACCENT_PRESETS,
    }
    # A configured key the catalog does not know (a variant deleted since, a hand-edited config)
    # makes resolve() fall back to the slot default. The preview then differs from what the config
    # names, and that must be VISIBLE on the page, never silent (hard rule 2).
    ctx["unresolved"] = [
        {"slot": what, "key": doc.unresolved_keys.get(what)} for what in doc.unresolved
    ]
    ctx["over_page_limit"] = bool(
        render.page_count is not None and render.page_count > settings.docs.max_pages
    )
    return ctx


@router.get("/renders/new", response_class=HTMLResponse)
async def new_render(request: Request):
    """Create a DocRender for a master (optionally for a specific opportunity) using the master's
    default selection, and redirect to its review page. Any failure — including a missing Edge,
    since render_document calls Playwright — is shown inline instead of raising, so this route
    never 500s even where Playwright is not installed (a cloud agent, a fresh checkout)."""
    from kestrel.docs.catalog import default_config
    from kestrel.docs.service import render_document

    master_id_raw = request.query_params.get("master_id")
    opportunity_id_raw = request.query_params.get("opportunity_id")
    if not master_id_raw:
        ctx = {
            "title": "Render review",
            "query": dict(request.query_params),
            "render": None,
            "new_render_error": "master_id is required to create a render.",
        }
        return templates().TemplateResponse(request, "renders.html", ctx, status_code=400)
    try:
        master_id = int(master_id_raw)
        opportunity_id = int(opportunity_id_raw) if opportunity_id_raw else None
        async with sessionmaker()() as s:
            catalog = await load_catalog(s, master_id)
            if catalog is None:
                raise ValueError(f"no such document master: {master_id}")
            settings = await load_settings_with_db(s)
            config = default_config(catalog)
            # The Documents page sends the layout its preview showed, so the review matches
            # what the operator just looked at (D-UI-22). Unknown keys fall to the default.
            wanted = request.query_params.get("layout")
            if wanted and any(lay.key == wanted for lay in catalog.layouts):
                config["layout"] = wanted
            # The font and header alignment chosen beside it (D-DOC-1); only listed values.
            config.update(_look_params(request.query_params))
            # Confidential mode redacts the current employer in the rendered PDF, not just the UI:
            # resolve() swaps org_label -> org_label_redacted when redact_orgs is set.
            config["redact_orgs"] = config.get("redact_orgs") or settings.confidential_mode
            result = await render_document(
                s, settings, master_id, config=config, opportunity_id=opportunity_id,
            )
    except Exception as exc:  # Playwright/Edge missing, bad id, or any render failure — never 500
        ctx = {
            "title": "Render review",
            "query": dict(request.query_params),
            "render": None,
            "new_render_error": f"Could not create a test render: {exc}",
        }
        return templates().TemplateResponse(request, "renders.html", ctx, status_code=200)
    if result is None:
        ctx = {
            "title": "Render review",
            "query": dict(request.query_params),
            "render": None,
            "new_render_error": f"No such document master: {master_id}",
        }
        return templates().TemplateResponse(request, "renders.html", ctx, status_code=404)
    back = back_target(request.query_params)
    suffix = f"?from={back}" if back else ""
    return RedirectResponse(f"/renders/{result.doc_render_id}{suffix}", status_code=303)


@router.get("/renders/{render_id}", response_class=HTMLResponse)
async def page(request: Request, render_id: int):
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        settings = await load_settings_with_db(s)
        ctx = await _build_ctx(request, s, render, settings)
    ctx["render_id_requested"] = render_id
    tmpl = "partials/render_review.html" if request.headers.get("HX-Request") else "renders.html"
    return templates().TemplateResponse(request, tmpl, ctx)


def _choice_for(
    catalog: MasterCatalog, config: dict, slot_key: str
) -> tuple[set[str] | None, str | None]:
    """(the keys the operator may set this part to, the config's CURRENT value for it). The part
    is a bullet slot, "summary" or "skills". The current value is what the override row records
    as the model's choice."""
    if slot_key == "summary":
        keys = {str(v.get("key")) for v in catalog.summary_variants if v.get("key")}
        return (keys | {"hide"} if keys else None), (
            "hide" if config.get("hide_summary") else config.get("summary")
        )
    if slot_key == "skills":
        return {"show", "hide"}, ("hide" if config.get("hide_skills") else "show")
    return operator_slot_keys(catalog, slot_key), (config.get("slots") or {}).get(slot_key)


async def _model_choice(
    session: AsyncSession, render: DocRender, slot_key: str, current: str | None
) -> str | None:
    """What the MODEL chose for this part of this render: the model_choice of the render's first
    override row for the part (written before the config was first changed, so it holds the
    render's stored selection), else the config's current value, which nothing has changed yet.
    A later operator swap is never reported to the learning loop as the model's pick."""
    first = (
        await session.execute(
            select(SelectionOverride.model_choice)
            .where(
                SelectionOverride.doc_render_id == render.id,
                SelectionOverride.slot_key == slot_key,
            )
            .order_by(SelectionOverride.id)
            .limit(1)
        )
    ).first()
    return first[0] if first is not None else current


def _apply_choice(config: dict, slot_key: str, variant_key: str) -> dict:
    """The config with one part set. A summary or skills "hide" is a per-render flag the renderer
    honors; choosing a summary wording (or "show") clears it."""
    config = dict(config or {})
    if slot_key == "summary":
        if variant_key == "hide":
            config["hide_summary"] = True
        else:
            config["summary"] = variant_key
            config.pop("hide_summary", None)
    elif slot_key == "skills":
        if variant_key == "hide":
            config["hide_skills"] = True
        else:
            config.pop("hide_skills", None)
    else:
        slots = dict(config.get("slots") or {})
        slots[slot_key] = variant_key
        config["slots"] = slots
    return config


@router.post("/renders/{render_id}/swap", response_class=HTMLResponse)
async def swap(request: Request, render_id: int):
    """THE GATE ITEM. Write the selection_override BEFORE mutating the config, with the model's
    own selection for this render as model_choice (the first override's model_choice once one
    exists, else the config's current value), so the row is a true record of what the model
    picked versus what the operator picked instead, even after an earlier swap. Then update
    config, recompute the live preview the same way the original render did (resolve +
    render_html), and return the updated preview+slot partial. This does not touch the PDF on
    disk; the swap is reviewed live; a fresh PDF comes from a new test render, since regenerating
    one here would require Playwright/Edge on every swap.

    The part is a bullet slot, "summary" or "skills". Any part can be hidden for this render
    ("hide"), optional or not; a hide is never a deletion."""
    form = await request.form()
    slot_key = str(form.get("slot_key") or "").strip()
    variant_key = str(form.get("variant_key") or "").strip()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return HTMLResponse("not found", status_code=404)
        if not slot_key or not variant_key:
            return HTMLResponse("slot_key and variant_key are required", status_code=400)
        # Validate against the LIVE catalog before anything is written: a key that is not one of
        # this part's wordings (or "hide") is refused outright. Writing it would make resolve()
        # silently substitute the default while the config claimed otherwise (hard rule 2: an
        # unknown key is a surfaced failure, never a quiet default).
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return HTMLResponse(
                "the document master for this render no longer exists", status_code=404
            )
        config = dict(render.config or {})
        allowed, current = _choice_for(catalog, config, slot_key)
        if allowed is None:
            return HTMLResponse(f"unknown slot key: {slot_key!r}", status_code=400)
        if variant_key not in allowed:
            return HTMLResponse(
                f"variant key {variant_key!r} is not one of slot {slot_key!r}'s variants "
                f"({', '.join(sorted(allowed))}); nothing was changed",
                status_code=400,
            )
        model_choice = await _model_choice(s, render, slot_key, current)
        s.add(
            SelectionOverride(
                doc_render_id=render.id,
                opportunity_id=render.opportunity_id,
                slot_key=slot_key,
                model_choice=model_choice,
                operator_choice=variant_key,
            )
        )
        render.config = _apply_choice(config, slot_key, variant_key)
        await s.commit()
        await s.refresh(render)
        settings = await load_settings_with_db(s)
        ctx = await _build_ctx(request, s, render, settings)
    return templates().TemplateResponse(request, "partials/render_review.html", ctx)


_WORDING_MAX = 2000


def _next_key(taken: set[str]) -> str:
    n = 1
    while f"mine{n}" in taken:
        n += 1
    return f"mine{n}"


@router.post("/renders/{render_id}/wording", response_class=HTMLResponse)
async def edit_wording(request: Request, render_id: int):
    """Edit wording: the operator types their own text for one part of this render. It is saved as
    a NEW wording (a bullet gets a variant with derived False; the summary gets a new summary
    wording), chosen for this render through the same override path as a swap, and the original
    stays available under Swap. The operator wrote it, so it is the source of truth: the
    verbatim check that guards model output and imports does not apply here, the same as the
    Documents page's own "edit wording" route. The model is not involved."""
    form = await request.form()
    slot_key = str(form.get("slot_key") or "").strip()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return HTMLResponse("not found", status_code=404)
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return HTMLResponse(
                "the document master for this render no longer exists", status_code=404
            )
        config = dict(render.config or {})
        notice = None
        from kestrel.privacy import contains_live_code

        if contains_live_code(text):  # D-PRIV-1 save boundary
            notice = "Not saved: it contains a stand-in code instead of a name."
        elif not text or len(text) > _WORDING_MAX:
            notice = (
                "Not saved: type the wording first." if not text
                else f"Not saved: a wording can be at most {_WORDING_MAX} characters."
            )
        elif slot_key == "summary":
            master = await s.get(DocMaster, render.doc_master_id)
            current = [dict(v) for v in (master.summary_variants or [])]
            key = _next_key({str(v.get("key")) for v in current})
            _allowed, current_choice = _choice_for(catalog, config, "summary")
            model_choice = await _model_choice(s, render, "summary", current_choice)
            master.summary_variants = [*current, {"key": key, "text": text}]  # a new list
            s.add(SelectionOverride(
                doc_render_id=render.id, opportunity_id=render.opportunity_id,
                slot_key="summary", model_choice=model_choice, operator_choice=key,
            ))
            render.config = _apply_choice(config, "summary", key)
            notice = "Saved as a new summary wording. The original is still under Swap."
        else:
            slot = (
                await s.execute(
                    select(DocSlot)
                    .join(DocSection, DocSection.id == DocSlot.doc_section_id)
                    .where(
                        DocSlot.key == slot_key,
                        DocSection.doc_master_id == render.doc_master_id,
                    )
                )
            ).scalars().first()
            if slot is None:
                return HTMLResponse(f"unknown slot key: {slot_key!r}", status_code=400)
            variants = (
                await s.execute(select(DocVariant).where(DocVariant.doc_slot_id == slot.id))
            ).scalars().all()
            key = _next_key({v.key for v in variants})
            chosen = (config.get("slots") or {}).get(slot_key, slot.default_variant_key)
            base = next((v for v in variants if v.key == chosen), None)
            s.add(DocVariant(
                doc_slot_id=slot.id, key=key, text=text,
                tags=list(base.tags or []) if base else [], derived=False,
            ))
            s.add(SelectionOverride(
                doc_render_id=render.id, opportunity_id=render.opportunity_id,
                slot_key=slot_key,
                model_choice=await _model_choice(s, render, slot_key, chosen),
                operator_choice=key,
            ))
            render.config = _apply_choice(config, slot_key, key)
            notice = "Saved as a new wording of your own. The original is still under Swap."
        await s.commit()
        await s.refresh(render)
        settings = await load_settings_with_db(s)
        ctx = await _build_ctx(request, s, render, settings)
    ctx["wording_notice"] = notice
    return templates().TemplateResponse(request, "partials/render_review.html", ctx)


@router.post("/renders/{render_id}/look", response_class=HTMLResponse)
async def change_look(request: Request, render_id: int):
    """Change the review's layout, font or header alignment (D-DOC-1). Presentation only: no
    variant changes, so no selection_override row. A layout the catalog does not have is refused;
    an empty font or alignment returns to the layout's default. Like a swap, this updates the live
    preview; a fresh PDF comes from a new test render."""
    form = await request.form()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return HTMLResponse("not found", status_code=404)
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return HTMLResponse(
                "the document master for this render no longer exists", status_code=404
            )
        config = dict(render.config or {})
        layout = str(form.get("layout") or "")
        if layout:
            if not any(lay.key == layout for lay in catalog.layouts):
                from html import escape

                return HTMLResponse(f"Unknown layout {escape(layout)}; nothing was changed.")
            config["layout"] = layout
        config.pop("font", None)
        config.pop("header_align", None)
        config.pop("accent", None)
        config.update(_look_params(form))
        if render.pdf_path:
            # This render's PDF exists and may already be attached to a pursuit; its config must
            # keep describing that PDF (review finding 15). A new look is a new test render.
            from urllib.parse import urlencode

            q = {"master_id": render.doc_master_id, "layout": config.get("layout") or ""}
            q.update({k: config[k] for k in ("font", "header_align", "accent") if config.get(k)})
            if render.opportunity_id:
                q["opportunity_id"] = render.opportunity_id
            back = request_back(request, form)
            if back:
                q["from"] = back  # the new render keeps the way back to the role or the board
            resp = HTMLResponse("")
            resp.headers["HX-Redirect"] = f"/renders/new?{urlencode(q)}"
            return resp
        render.config = config
        await s.commit()
        await s.refresh(render)
        settings = await load_settings_with_db(s)
        ctx = await _build_ctx(request, s, render, settings)
    ctx["look_changed"] = True
    return templates().TemplateResponse(request, "partials/render_review.html", ctx)


@router.post("/renders/{render_id}/attach", response_class=HTMLResponse)
async def attach_to_pursuit(request: Request, render_id: int):
    """Attach this render as the pursuit's active document (sets pursuit.document_id). No-op with a
    clear message when the render has no opportunity, or the opportunity has no pursuit yet."""
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return HTMLResponse("not found", status_code=404)
        if render.opportunity_id is None:
            return HTMLResponse(
                '<span class="muted">This render is not tied to an opportunity.</span>'
            )
        pursuit = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == render.opportunity_id))
        ).scalar_one_or_none()
        opp = await s.get(Opportunity, render.opportunity_id)
        word = "lead" if opp is not None and opp.track == "consulting" else "application"
        if pursuit is None:
            return HTMLResponse(
                f'<span class="muted">Add this role to your list first; there is no {word} '
                "to attach it to yet.</span>"
            )
        pursuit.document_id = render.id
        await s.commit()
    return HTMLResponse(f'<span class="covered">Attached to this {word}.</span>')


@router.get("/renders/{render_id}/download")
async def download(render_id: int):
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
    if render is None or not render.pdf_path:
        return HTMLResponse("No PDF on record for this render.", status_code=404)
    path = Path(render.pdf_path)
    if not path.is_file():
        return HTMLResponse("The PDF file is missing from disk.", status_code=404)
    return FileResponse(
        path, media_type="application/pdf", filename=render.filename or path.name
    )
