"""Document manager (SPEC-INTERFACES 2.1). Per track and doc kind: the section/slot/variant tree
with inline variant editing (the operator edits their OWN text, byte-for-byte — the model never
touches it here), tag editing, layout picker, a live HTML preview, a test PDF render, and the
markdown-import flow with its reviewable proposal and verbatim flagging.

This is a reference page for the Phase 13 fan-out: it exercises a nested editing tree, HTMX partial
updates that persist, and a form (import) whose proposal is reviewed before anything is written."""

from __future__ import annotations

import re
from html import escape

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant
from kestrel.docs import convert as convert_mod
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()
log = structlog.get_logger()


_ROLE_PREFIX = re.compile(r"^r\d+_")


def slot_label(key: str) -> str:
    """A readable name for a slot key: `r2_m_and_a_integration` reads "M and a integration".
    The key itself is an internal identifier (the P3 schema and the render config use it), so
    the tree keeps it only as a tooltip and a data attribute, never as visible text (D-UI-29)."""
    text = _ROLE_PREFIX.sub("", key or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else key


async def _tree(session: AsyncSession, master_id: int) -> list[dict]:
    """The section -> slot -> variant tree for one master, as plain dicts for the template."""
    sections = (
        (
            await session.execute(
                select(DocSection)
                .where(DocSection.doc_master_id == master_id)
                .order_by(DocSection.sort_order, DocSection.id)
            )
        )
        .scalars()
        .all()
    )
    from kestrel.docs.current_employer import also_marked, marks_elsewhere

    marks = await marks_elsewhere(session)
    out: list[dict] = []
    for sec in sections:
        slots = (
            (
                await session.execute(
                    select(DocSlot)
                    .where(DocSlot.doc_section_id == sec.id)
                    .order_by(DocSlot.sort_order, DocSlot.id)
                )
            )
            .scalars()
            .all()
        )
        slot_dicts = []
        for slot in slots:
            variants = (
                (
                    await session.execute(
                        select(DocVariant)
                        .where(DocVariant.doc_slot_id == slot.id)
                        .order_by(DocVariant.id)
                    )
                )
                .scalars()
                .all()
            )
            slot_dicts.append({
                "slot": slot, "variants": variants, "n": len(slot_dicts) + 1,
                "label": slot_label(slot.key),
            })
        out.append({"section": sec, "slots": slot_dicts, "also": also_marked(marks, sec)})
    return out


async def _masters(session: AsyncSession, track: str | None) -> list[DocMaster]:
    stmt = select(DocMaster).order_by(DocMaster.track, DocMaster.doc_kind, DocMaster.id)
    if track:
        stmt = stmt.where(DocMaster.track == track)
    return list((await session.execute(stmt)).scalars().all())


async def _layouts(session: AsyncSession, master: DocMaster) -> list:
    """The layouts this master's renders can use, from the catalog (never a hardcoded list:
    the render review and the preview must agree, D-UI-22)."""
    from kestrel.docs.catalog import load_catalog

    catalog = await load_catalog(session, master.id)
    return list(catalog.layouts) if catalog else []


async def documents_context(request: Request) -> dict:
    """The Documents tab's context: the masters, the selected one and its tree."""
    track = request.query_params.get("track") or None
    master_id = request.query_params.get("master_id")
    async with sessionmaker()() as s:
        masters = await _masters(s, track)
        selected = None
        if master_id and master_id.isdigit():
            selected = await s.get(DocMaster, int(master_id))
        elif masters:
            selected = masters[0]
        await _ensure_entry_ids(s, selected)
        tree = await _tree(s, selected.id) if selected else []
        layouts = [lay.key for lay in await _layouts(s, selected)] if selected else []
        assess_host = await _assess_hosted_backend(s)
        conf = await _confidential(s)
        banner = await _employer_banner(s) if conf else []
    from kestrel.docs.current_employer import suggested_placeholder

    # ?section=<id> (the Settings "Choose your current employer" link) opens that role's details.
    wanted = request.query_params.get("section") or ""
    return {"masters": masters, "selected": selected, "tree": tree, "layouts": layouts,
            "open_section": int(wanted) if wanted.isdigit() else None,
            "employer_banner": banner,
            "edit_open_id": int(wanted) if wanted.isdigit() else None,
            "placeholder_hint": suggested_placeholder(selected.track if selected else None),
            "assess_host": assess_host, **_style_choices(layouts),
            **_accent_ctx(selected), **_optional_ctx(selected, confidential=conf)}


async def _assess_hosted_backend(session: AsyncSession) -> str | None:
    """The hosted backend the resume assessment would send to, or None when it runs locally
    (review finding 14: the button must say the resume text leaves the machine)."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.services.setting_overrides import load_settings_with_db

    try:
        settings = await load_settings_with_db(session)
        tier = load_prompt("resume_assess/v1").tier
        tcfg = settings.llm.tiers.get(tier)
        backend = settings.llm.backends.get(tcfg.backend) if tcfg else None
    except Exception:  # a settings problem is the Settings page's to report
        return None
    if backend is None or not getattr(backend, "external", False):
        return None
    return tcfg.backend


def _style_choices(layouts: list[str]) -> dict:
    """The Font and Header selects beside Layout (D-DOC-1): the fixed font list, and each
    layout's own default so the select can say what "Layout default" means."""
    from kestrel.docs.styles import FONTS, layout_defaults

    return {"fonts": list(FONTS), "layout_looks": {k: layout_defaults(k) for k in layouts}}


def _accent_ctx(master: DocMaster | None) -> dict:
    """The accent picker (D-DOC-4): the presets, and the master's saved accent, shown as its
    preset when it is one and as a custom value otherwise."""
    from kestrel.docs.styles import ACCENT_PRESETS, valid_accent

    saved = valid_accent(((master.look if master else None) or {}).get("accent"))
    return {"accent_presets": ACCENT_PRESETS, "accent_saved": saved,
            "accent_is_custom": bool(saved and saved not in ACCENT_PRESETS.values())}


def _optional_ctx(master: DocMaster | None, *, note: str | None = None,
                  open_kind: str | None = None, error: str | None = None,
                  confidential: bool = False) -> dict:
    """The optional sections editor (D-DOC-3): each kind's fields and its stored entries."""
    from kestrel.docs.sections import FIELDS, KINDS

    def rows(kind: str) -> list[tuple[str, dict]]:
        # (entry id, entry): the routes address an entry by its stable id, never its position,
        # so two windows editing one list cannot overwrite the wrong entry. Ids are assigned on
        # read (_ensure_entry_ids) before this runs.
        raw = (getattr(master, kind, None) or []) if master else []
        return [(str(e.get("id") or ""), e) for e in raw if isinstance(e, dict)]

    kinds = [{"kind": k, "heading": FIELDS[k][0], "fields": FIELDS[k][1], "entries": rows(k)}
             for k in KINDS]
    return {"optional_kinds": kinds, "optional_note": note, "optional_open": open_kind,
            "optional_error": error, "optional_confidential": confidential}


async def _ensure_entry_ids(session: AsyncSession, master: DocMaster | None) -> None:
    """Give every stored optional-section entry an id (entries saved before ids existed), and
    persist it, so every window that renders the list sees the same ids. Commits only when
    something was assigned."""
    from kestrel.docs.sections import KINDS, with_ids

    if master is None:
        return
    changed = False
    for kind in KINDS:
        fixed, assigned = with_ids(list(getattr(master, kind, None) or []))
        if assigned:
            setattr(master, kind, fixed)  # a new list, so the JSON change is persisted
            changed = True
    if changed:
        await session.commit()


async def _employer_banner(session: AsyncSession) -> list[dict]:
    """The inferred current employers (D-PRIV-1 fail closed), for the Confirm or change banner."""
    from kestrel.docs.current_employer import current_employers

    return [e for e in await current_employers(session) if e["inferred"]]


async def _confidential(session: AsyncSession) -> bool:
    from kestrel.services.setting_overrides import load_settings_with_db

    return bool((await load_settings_with_db(session)).confidential_mode)


async def _optional_response(request: Request, master_id: int, **kw):
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        await _ensure_entry_ids(s, master)
        conf = await _confidential(s)
    ctx = {"selected": master, **_optional_ctx(master, confidential=conf, **kw)}
    return templates().TemplateResponse(request, "partials/doc_optional_sections.html", ctx)


async def _tree_response(request: Request, master_id: int, *, note: str | None = None,
                         open_section: int | None = None, edit_open: bool = False):
    """Re-render the whole tree for one master after an add or a remove, so the numbering and
    the forms stay consistent with what is stored. Roles render collapsed; the one just worked
    on (`open_section`) stays open so the operator does not lose their place."""
    async with sessionmaker()() as s:
        selected = await s.get(DocMaster, master_id)
        tree = await _tree(s, master_id) if selected else []
    from kestrel.docs.current_employer import suggested_placeholder

    ctx = {"selected": selected, "tree": tree, "tree_note": note, "open_section": open_section,
           "edit_open_id": open_section if edit_open else None,
           "placeholder_hint": suggested_placeholder(selected.track if selected else None)}
    return templates().TemplateResponse(request, "partials/doc_tree.html", ctx)


@router.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    """Documents is the first tab of the Profile page (D-UI-21); this path stays so every link
    into it, with or without a master_id, keeps working."""
    from kestrel.web.pages.profile import TABS, _section_ctx, _track

    data = await documents_context(request)
    if request.headers.get("HX-Request"):
        return templates().TemplateResponse(request, "partials/doc_tree.html", data)
    ctx = {
        "title": "Profile", "query": dict(request.query_params), "tab": "documents",
        "tabs": TABS, **_section_ctx(_track(request)), **data,
    }
    return templates().TemplateResponse(request, "profile.html", ctx)


@router.post("/documents/variant/{variant_id}", response_class=HTMLResponse)
async def edit_variant(request: Request, variant_id: int):
    """Persist an inline edit to a variant's own text/tags. The operator's words go in verbatim;
    no model is involved, so there is nothing to hallucinate here (constraint 2 guards the SELECT
    path, not the operator editing their own source text)."""
    form = await request.form()
    async with sessionmaker()() as s:
        v = await s.get(DocVariant, variant_id)
        if v is None:
            return HTMLResponse("not found", status_code=404)
        if "text" in form:
            from kestrel.privacy import contains_live_code

            if contains_live_code(str(form["text"])):  # D-PRIV-1 save boundary
                return HTMLResponse('<span class="warn" role="alert">Not saved: it contains a '
                                    "stand-in code instead of a name.</span>")
            v.text = str(form["text"])
        if "tags" in form:
            v.tags = [t.strip() for t in str(form["tags"]).split(",") if t.strip()]
        await s.commit()
        await s.refresh(v)
        ctx = {"v": v, "saved": True}
    return templates().TemplateResponse(request, "partials/doc_variant.html", ctx)


@router.post("/documents/section/{section_id}", response_class=HTMLResponse)
async def edit_section(request: Request, section_id: int):
    """Save a role's own details (company, title, dates, context line): what the import copied
    from the header, now the operator's to correct (D-UI-18). Operator text, no model."""
    from kestrel.docs.current_employer import (
        EMPLOYER_KINDS,
        also_marked,
        clean_placeholder,
        marks_elsewhere,
        set_current_employer,
        suggested_placeholder,
    )
    from kestrel.privacy import default_names

    form = await request.form()
    async with sessionmaker()() as s:
        sec = await s.get(DocSection, section_id)
        if sec is None:
            return HTMLResponse("not found", status_code=404)
        master = await s.get(DocMaster, sec.doc_master_id)
        hint = suggested_placeholder(master.track if master else None)
        # The current-employer box (D-PRIV-1) is only judged when its form sent it: an older
        # client posting just the details must not unmark the employer by omission.
        judged = "current_employer_form" in form and sec.section_kind in EMPLOYER_KINDS
        ticked = judged and "on" in form.getlist("current_employer")
        shown_as = clean_placeholder(form.get("org_label_redacted"))
        org_after = str(form.get("org_label", sec.org_label or "")).strip()
        error = None
        if ticked and not shown_as:
            error = "Say how your current employer should be shown, or untick the box."
        elif ticked and not org_after:
            error = "Add the company name first, so Kestrel knows which name to hide."
        if error:
            ctx = {"section": sec, "saved": False, "edit_open": True, "employer_error": error,
                   "placeholder_hint": hint, "form_shown_as": shown_as,
                   "also": also_marked(await marks_elsewhere(s), sec)}
            return templates().TemplateResponse(request, "partials/doc_section_edit.html", ctx)
        previous_name = sec.org_label
        was_marked = sec.org_label_redacted
        was_names = list(sec.hide_names or []) or None
        for name in ("org_label", "title", "start_date", "end_date", "context_line"):
            if name in form:
                value = str(form[name]).strip()
                setattr(sec, name, value or None)
        renamed = (previous_name or "") != (sec.org_label or "")
        siblings = await _sibling_marks(s, sec)
        if judged and (ticked or was_marked is not None):
            # The chips the form sent, plus "Add a name"; a form without chips (an older
            # client, or a first tick) gets the default list for the label.
            posted = [str(x) for x in form.getlist("hide_name")]
            extra = str(form.get("hide_name_new") or "").strip()
            chosen: list[str] | None = None
            if posted:
                chosen = [*posted, extra]
            elif extra:
                chosen = [*default_names(sec.org_label), extra]
            if renamed and posted and posted == list(was_names or []):
                # Chips untouched on a rename: keep them and add the new label's defaults.
                chosen = [*posted, *default_names(sec.org_label), extra]
            await set_current_employer(s, sec, shown_as if ticked else None,
                                       previous_name=previous_name, names=chosen)
        elif was_marked is not None and renamed:
            # G: the mark lives on the row; a rename carries it (and its "Show it as") to the
            # promotion rows that shared the old name.
            await set_current_employer(s, sec, was_marked, previous_name=previous_name)
        changed_elsewhere = siblings != await _sibling_marks(s, sec)
        from kestrel.docs.current_employer import touch_master

        await touch_master(s, sec.doc_master_id)  # D-PRIV-1: the newest document wins
        unticked = judged and not ticked and was_marked is not None
        await s.commit()
        await s.refresh(sec)
        master_id = sec.doc_master_id
        # Another role in this document changed with it (unmarked, or marked as the same
        # company): re-render the tree so no box is left showing a stale state.
        note = None
        if changed_elsewhere:
            note = ("Saved. This is now the only current employer in this document." if ticked
                    else "Saved. No role in this document is marked as your current employer.")
        also = also_marked(await marks_elsewhere(s), sec)
        still_hidden = None
        if unticked and also:
            # F: the map is the union of every document's marks, so the name stays hidden while
            # another document marks it. Say so rather than leave the operator guessing.
            still_hidden = ("Still hidden in " + "; ".join(e["doc"] for e in also)
                            + ", which also marks this employer. Untick it there to show the "
                            "name in that document.")
            if note:
                note = f"{note} {still_hidden}"
        ctx = {"section": sec, "saved": True, "placeholder_hint": hint, "also": also,
               "still_hidden": still_hidden}
    if note:
        resp = await _tree_response(request, master_id, note=note, open_section=section_id,
                                    edit_open=True)
        resp.headers["HX-Retarget"] = "#tree"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    return templates().TemplateResponse(request, "partials/doc_section_edit.html", ctx)


async def _sibling_marks(s: AsyncSession, sec: DocSection) -> dict[int, str | None]:
    """{section id: org_label_redacted} for every OTHER section in this master, to tell whether
    marking `sec` changed one of them."""
    rows = (
        await s.execute(
            select(DocSection).where(DocSection.doc_master_id == sec.doc_master_id,
                                     DocSection.id != sec.id)
        )
    ).scalars().all()
    return {r.id: r.org_label_redacted for r in rows}


@router.post("/documents/section/{section_id}/confirm-current", response_class=HTMLResponse)
async def confirm_current(request: Request, section_id: int):
    """The banner's Confirm (D-PRIV-1): the inferred current role becomes a real mark with the
    "Show it as" text it was shown with."""
    from kestrel.docs.current_employer import (
        clean_placeholder,
        set_current_employer,
        suggested_placeholder,
    )

    form = await request.form()
    async with sessionmaker()() as s:
        sec = await s.get(DocSection, section_id)
        if sec is None or not (sec.org_label or "").strip():
            return HTMLResponse("not found", status_code=404)
        master = await s.get(DocMaster, sec.doc_master_id)
        shown = (clean_placeholder(form.get("shown_as"))
                 or suggested_placeholder(master.track if master else None))
        await set_current_employer(s, sec, shown)
        await s.commit()
        name = sec.org_label
    return HTMLResponse(
        f'<p class="saved" role="status">Confirmed: hiding {escape(name)} as {escape(shown)}.</p>'
    )


# ------------------------------------------------- add and remove roles and bullets (D-UI-29)
_KEY_PREFIX = {"role": "r", "service_line": "s", "proof_engagement": "p", "credential": "e"}
_LABEL_MAX = 200


async def _master_slot_keys(s: AsyncSession, master_id: int) -> set[str]:
    rows = await s.execute(
        select(DocSlot.key).join(DocSection, DocSlot.doc_section_id == DocSection.id)
        .where(DocSection.doc_master_id == master_id)
    )
    return {k for (k,) in rows.all()}


async def _section_ordinal(s: AsyncSession, sec: DocSection) -> int:
    """1-based position of a section among its master's sections of the same kind."""
    ids = (
        await s.execute(
            select(DocSection.id).where(DocSection.doc_master_id == sec.doc_master_id,
                                        DocSection.section_kind == sec.section_kind)
            .order_by(DocSection.sort_order, DocSection.id)
        )
    ).scalars().all()
    return (list(ids).index(sec.id) + 1) if sec.id in ids else len(ids) + 1


async def _add_bullet(s: AsyncSession, sec: DocSection, text: str) -> DocSlot:
    """A new slot at the end of the section with one variant, `original`, holding the
    operator's own text exactly as typed (derived: false). The key is generated from the text's
    first words, unique across the master, the way the import names slots."""
    from kestrel.docs.imports import _slug, _unique

    taken = await _master_slot_keys(s, sec.doc_master_id)
    n = await _section_ordinal(s, sec)
    words = " ".join(text.split()[:5])
    prefix = _KEY_PREFIX.get(sec.section_kind, "x")
    key = _unique(f"{prefix}{n}_{_slug(words, f'b{len(taken) + 1}')}", taken)
    last = (
        await s.execute(select(DocSlot.sort_order).where(DocSlot.doc_section_id == sec.id)
                        .order_by(DocSlot.sort_order.desc()).limit(1))
    ).scalar_one_or_none()
    slot = DocSlot(doc_section_id=sec.id, key=key,
                   sort_order=(last + 1) if last is not None else 0,
                   optional=False, default_variant_key="original")
    s.add(slot)
    await s.flush()
    s.add(DocVariant(doc_slot_id=slot.id, key="original", text=text, tags=[], derived=False))
    return slot


async def _delete_slot(s: AsyncSession, slot_id: int) -> None:
    from sqlalchemy import delete

    await s.execute(delete(DocVariant).where(DocVariant.doc_slot_id == slot_id))
    await s.execute(delete(DocSlot).where(DocSlot.id == slot_id))


@router.post("/documents/{master_id}/roles", response_class=HTMLResponse)
async def add_role(request: Request, master_id: int):
    """Add a role at the end of the master with its first bullet. Every field is the operator's
    own text; the bullet is required so a role never prints empty by accident."""
    form = await request.form()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        if not text:
            return await _tree_response(request, master_id,
                                        note="Not added: the role needs its first bullet.")
        last = (
            await s.execute(select(DocSection.sort_order)
                            .where(DocSection.doc_master_id == master_id)
                            .order_by(DocSection.sort_order.desc()).limit(1))
        ).scalar_one_or_none()
        values = {name: (str(form.get(name) or "").strip()[:_LABEL_MAX] or None)
                  for name in ("org_label", "title", "start_date", "end_date", "context_line")}
        sec = DocSection(doc_master_id=master_id, section_kind="role",
                         sort_order=(last + 1) if last is not None else 0, **values)
        s.add(sec)
        await s.flush()
        await _add_bullet(s, sec, text)
        await s.commit()
        new_id = sec.id
    return await _tree_response(request, master_id, note="Role added.", open_section=new_id)


@router.post("/documents/section/{section_id}/bullets", response_class=HTMLResponse)
async def add_bullet(request: Request, section_id: int):
    """Add one bullet, in the operator's own words, at the end of a role."""
    form = await request.form()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        sec = await s.get(DocSection, section_id)
        if sec is None:
            return HTMLResponse("not found", status_code=404)
        master_id = sec.doc_master_id
        if not text:
            return await _tree_response(request, master_id,
                                        note="Not added: type the bullet's text first.")
        await _add_bullet(s, sec, text)
        await s.commit()
    return await _tree_response(request, master_id, note="Bullet added.",
                                open_section=section_id)


@router.post("/documents/slot/{slot_id}/delete", response_class=HTMLResponse)
async def remove_bullet(request: Request, slot_id: int):
    """Remove one bullet and every wording of it. Renders already made keep their PDF; a
    stored config that names this slot is reported as unresolved on its review page."""
    async with sessionmaker()() as s:
        slot = await s.get(DocSlot, slot_id)
        if slot is None:
            return HTMLResponse("not found", status_code=404)
        sec = await s.get(DocSection, slot.doc_section_id)
        master_id = sec.doc_master_id if sec else None
        section_id = sec.id if sec else None
        await _delete_slot(s, slot_id)
        await s.commit()
    if master_id is None:
        return HTMLResponse("")
    return await _tree_response(request, master_id, note="Bullet removed.",
                                open_section=section_id)


@router.post("/documents/section/{section_id}/delete", response_class=HTMLResponse)
async def remove_role(request: Request, section_id: int):
    """Remove a role with all its bullets. An empty master is allowed."""
    from sqlalchemy import delete

    async with sessionmaker()() as s:
        sec = await s.get(DocSection, section_id)
        if sec is None:
            return HTMLResponse("not found", status_code=404)
        master_id = sec.doc_master_id
        slot_ids = (
            await s.execute(select(DocSlot.id).where(DocSlot.doc_section_id == section_id))
        ).scalars().all()
        for sid in slot_ids:
            await _delete_slot(s, sid)
        await s.execute(delete(DocSection).where(DocSection.id == section_id))
        await s.commit()
    return await _tree_response(request, master_id, note="Role removed.")


async def _slot_base(s: AsyncSession, slot_id: int):
    """(slot, its original variant, the role label) for the suggest flow, or None."""
    slot = await s.get(DocSlot, slot_id)
    if slot is None:
        return None
    variants = (
        await s.execute(select(DocVariant).where(DocVariant.doc_slot_id == slot_id)
                        .order_by(DocVariant.id))
    ).scalars().all()
    base = next((v for v in variants if v.key == slot.default_variant_key and not v.derived),
                None) or next((v for v in variants if not v.derived), None)
    section = await s.get(DocSection, slot.doc_section_id)
    label = " | ".join(x for x in (getattr(section, "org_label", ""),
                                   getattr(section, "title", "")) if x)
    return slot, base, variants, label


async def _slot_master(s: AsyncSession, slot: DocSlot) -> int | None:
    section = await s.get(DocSection, slot.doc_section_id)
    return section.doc_master_id if section is not None else None


@router.post("/documents/slot/{slot_id}/suggest", response_class=HTMLResponse)
async def suggest_slot_variants(request: Request, slot_id: int):
    """Ask for up to two alternate wordings of one slot's original bullet (P7c, D-IMPORT-3).
    Writes nothing; each suggestion is added only by an explicit click."""
    import httpx

    from kestrel.docs.imports import suggest_variants
    from kestrel.llm.client import LLMClient
    from kestrel.services.setting_overrides import load_settings_with_db

    ctx: dict = {"slot_id": slot_id, "suggestions": [], "error": None}
    async with sessionmaker()() as s:
        found = await _slot_base(s, slot_id)
        settings = await load_settings_with_db(s)
        if found is None or found[1] is None:
            ctx["error"] = "This slot has no original wording to work from."
        else:
            slot, base, _variants, label = found
            try:
                # The hosted path (D-PRIV-1): on an online tier the current employer goes as a
                # token and comes back as the real name; with nothing to hide it is refused.
                async with httpx.AsyncClient() as http:
                    ctx["suggestions"], ctx["error"] = await suggest_variants(
                        LLMClient(settings, http=http), role_label=label, text=base.text,
                        tags=list(base.tags or []), session=s,
                        master_id=await _slot_master(s, slot),
                    )
                await s.commit()  # a refusal or quarantine row the call staged
            except Exception as exc:  # a model that is not running must not 500 the page
                ctx["error"] = f"Could not reach the model: {type(exc).__name__}: {exc}"
    if (ctx["error"] or "").startswith("hosted send refused"):
        ctx["error"] = ("Not sent: confidential mode is on and no current employer is chosen, "
                        "so your employer's name would leave this machine. Choose it under "
                        "Settings, Core, then try again.")
    return templates().TemplateResponse(request, "partials/variant_suggestions.html", ctx)


def _refused(slot_id: int, message: str) -> HTMLResponse:
    """A refused Add shows its reason above the suggestions and leaves the suggestion in place
    (HX-Retarget), instead of landing among the slot's variants."""
    from html import escape

    return HTMLResponse(
        f'<div class="warnbox">{escape(message)}</div>',
        headers={"HX-Retarget": f"#suggest-msg-{slot_id}", "HX-Reswap": "innerHTML"},
    )


@router.post("/documents/slot/{slot_id}/variants", response_class=HTMLResponse)
async def add_slot_variant(request: Request, slot_id: int):
    """Add one reviewed suggestion as a derived variant. Re-checks the numbers against the
    original and the two-per-slot limit here, never trusting the form (hard rule 2)."""
    from kestrel.docs.imports import _MAX_DERIVED_PER_SLOT, facts_changed

    form = await request.form()
    text = str(form.get("text") or "").strip()
    tags = [t.strip() for t in str(form.get("tags") or "").split(",") if t.strip()]
    async with sessionmaker()() as s:
        found = await _slot_base(s, slot_id)
        if found is None or found[1] is None or not text:
            return _refused(slot_id, "Nothing to add.")
        slot, base, variants, _label = found
        from kestrel.privacy import contains_live_code

        if contains_live_code(text):
            # D-PRIV-1: a stand-in code from a model reply is never saved as the operator's text.
            return _refused(slot_id, "Not added: it contains a stand-in code instead of a name.")
        diff = facts_changed(base.text, text)
        if diff:
            return _refused(slot_id, f"Not added: it changes the numbers ({', '.join(diff)}).")
        if sum(1 for v in variants if v.derived) >= _MAX_DERIVED_PER_SLOT:
            return _refused(slot_id, f"Not added: this slot already has "
                            f"{_MAX_DERIVED_PER_SLOT} alternate wordings.")
        taken = {v.key for v in variants}
        n = 1
        while f"alt{n}" in taken:
            n += 1
        v = DocVariant(doc_slot_id=slot.id, key=f"alt{n}", text=text, tags=tags, derived=True)
        s.add(v)
        await s.commit()
        await s.refresh(v)
    return templates().TemplateResponse(request, "partials/doc_variant.html", {"v": v})


@router.post("/documents/{master_id}/preview", response_class=HTMLResponse)
async def preview(request: Request, master_id: int):
    """Live HTML preview using each slot's default variant. Byte-for-byte from doc_variant.text."""
    from kestrel.docs.catalog import load_catalog
    from kestrel.docs.render import render_html

    async with sessionmaker()() as s:
        catalog = await load_catalog(s, master_id)
    if catalog is None:
        return HTMLResponse("<p>No document.</p>")
    from kestrel.docs.catalog import resolve
    from kestrel.docs.styles import accent_from_params

    form = await request.form()
    config = {name: str(form.get(name)) for name in ("layout", "font", "header_align")
              if form.get(name)}
    accent, _err = accent_from_params(form)  # an invalid custom value previews the saved one
    if accent:
        config["accent"] = accent
    doc = resolve(catalog, config)  # slot defaults, the chosen layout's order, font and look
    try:
        html = render_html(doc)
        # The document carries its own stylesheet (paper colors, print fonts). Inside the page
        # those rules would be dropped and dark mode would paint it unreadable, so it goes in a
        # frame of its own (D-UI-22).
        doc_attr = escape(html, quote=True)
        return HTMLResponse(
            f'<iframe class="preview-doc" title="Preview" srcdoc="{doc_attr}"></iframe>'
        )
    except Exception as exc:  # a 500 here is invisible: htmx leaves the target untouched
        log.warning("preview_failed", master_id=master_id, error=f"{type(exc).__name__}: {exc}")
        return HTMLResponse(
            f'<div class="warnbox">Preview failed: {escape(f"{type(exc).__name__}: {exc}")}. '
            "Download diagnostics from About if this keeps happening.</div>"
        )


@router.post("/documents/{master_id}/accent", response_class=HTMLResponse)
async def save_accent(request: Request, master_id: int):
    """Save the document's accent color (D-DOC-4) on the master, so the preview, every test
    render and the pipeline's renders of it use the same color. A preset, a custom #rrggbb, or
    empty for the layout's own color. Anything else is refused and nothing is saved."""
    from kestrel.docs.styles import accent_from_params

    form = await request.form()
    accent, error = accent_from_params(form)
    if error:
        return HTMLResponse(f'<span class="warn" role="alert">{escape(error)} '
                            "Nothing was saved.</span>")
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        look = dict(master.look or {})
        if accent:
            look["accent"] = accent
        else:
            look.pop("accent", None)
        master.look = look
        await s.commit()
    msg = f"Saved {accent}." if accent else "Saved: the layout's own color."
    return HTMLResponse(f'<span class="saved" role="status">{escape(msg)}</span>')


# ------------------------------------- name, summary, skills and the optional flag (D-UI-52)
# Everything below is the operator's own typed text, saved as typed. None of it passes through
# the model or the import's verbatim check: that guards what a model proposes, and here nothing
# is proposed (hard rule 2 holds because the operator is the author).
_NAME_MAX = 200
_CONTACT_MAX = 300
_SUMMARY_MAX = 2000
_GROUP_LABEL_MAX = 100
_ITEM_MAX = 100
_ITEMS_MAX = 60
_GONE_WORDING = "That wording is no longer here; the list below is current."
_GONE_GROUP = "That group is no longer here; the list below is current."


async def _basics_response(request: Request, master_id: int, *, note: str | None = None,
                           error: str | None = None):
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
    ctx = {"selected": master, "basics_note": note, "basics_error": error}
    return templates().TemplateResponse(request, "partials/doc_basics.html", ctx)


def _split_items(raw: object) -> list[str]:
    items = [i.strip()[:_ITEM_MAX] for i in re.split(r"[,\n]", str(raw or "")) if i.strip()]
    return items[:_ITEMS_MAX]


@router.post("/documents/new", response_class=HTMLResponse)
async def new_document(request: Request):
    """Start an empty resume (or capability statement) with no import: a master with no roles,
    summary or skills, to be filled on this page. The operator's own document from the first
    keystroke."""
    form = await request.form()
    track = "consulting" if str(form.get("track")) == "consulting" else "employment"
    kind = "resume"
    if str(form.get("doc_kind")) == "capability_statement":
        kind = "capability_statement"
    name = str(form.get("name") or "").strip()[:_NAME_MAX] or (
        "Capability statement" if kind == "capability_statement" else "My resume"
    )
    async with sessionmaker()() as s:
        master = DocMaster(track=track, name=name, doc_kind=kind, header={},
                           summary_variants=[], skill_groups=[])
        s.add(master)
        await s.commit()
        new_id = master.id
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = f"/documents?master_id={new_id}"
    return resp


@router.post("/documents/{master_id}/header", response_class=HTMLResponse)
async def save_header(request: Request, master_id: int):
    """The name and the one contact line printed at the top of every render. Empty clears the
    field, and then the About you details are used for it (kestrel.docs.catalog.document_header)."""
    form = await request.form()
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        header = dict(master.header or {})
        for field, limit in (("name", _NAME_MAX), ("contact", _CONTACT_MAX)):
            value = " ".join(str(form.get(field) or "").split())[:limit]
            if value:
                header[field] = value
            else:
                header.pop(field, None)
        master.header = header  # a new dict, so the JSON change is persisted
        await s.commit()
    return await _basics_response(request, master_id, note="Saved. This prints at the top.")


@router.post("/documents/{master_id}/summary", response_class=HTMLResponse)
async def add_summary(request: Request, master_id: int):
    form = await request.form()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        if not text:
            return await _basics_response(request, master_id,
                                          error="Not added: type the summary wording first.")
        current = [dict(v) for v in (master.summary_variants or [])]
        taken = {str(v.get("key")) for v in current}
        n = len(current) + 1
        while f"summary_{n}" in taken:
            n += 1
        master.summary_variants = [*current, {"key": f"summary_{n}", "text": text[:_SUMMARY_MAX]}]
        await s.commit()
    return await _basics_response(request, master_id, note="Summary wording added.")


@router.post("/documents/{master_id}/summary/{key}", response_class=HTMLResponse)
async def edit_summary(request: Request, master_id: int, key: str):
    form = await request.form()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current = [dict(v) for v in (master.summary_variants or [])]
        row = next((v for v in current if v.get("key") == key), None)
        if row is None:
            return await _basics_response(request, master_id,
                                          error=_GONE_WORDING)
        if not text:
            return await _basics_response(
                request, master_id,
                error="Not saved: a wording cannot be empty. Use Remove to delete it.")
        row["text"] = text[:_SUMMARY_MAX]
        master.summary_variants = current
        await s.commit()
    return await _basics_response(request, master_id, note="Summary wording saved.")


@router.post("/documents/{master_id}/summary/{key}/delete", response_class=HTMLResponse)
async def remove_summary(request: Request, master_id: int, key: str):
    """Delete one wording. The last one is kept: a resume that has a summary does not lose it to
    one stray click. Editing it is always possible."""
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current = [dict(v) for v in (master.summary_variants or [])]
        if not any(v.get("key") == key for v in current):
            return await _basics_response(request, master_id,
                                          error=_GONE_WORDING)
        if len(current) <= 1:
            return await _basics_response(
                request, master_id,
                error="Kept: at least one summary wording stays. Edit it instead, or add the "
                      "new one first.")
        master.summary_variants = [v for v in current if v.get("key") != key]
        await s.commit()
    return await _basics_response(request, master_id, note="Summary wording removed.")


def _group_from_form(form) -> tuple[str, list[str]] | None:
    label = " ".join(str(form.get("label") or "").split())[:_GROUP_LABEL_MAX]
    items = _split_items(form.get("items"))
    return (label, items) if label and items else None


@router.post("/documents/{master_id}/skills", response_class=HTMLResponse)
async def add_skill_group(request: Request, master_id: int):
    from kestrel.docs.imports import _slug, _unique

    form = await request.form()
    group = _group_from_form(form)
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        if group is None:
            return await _basics_response(
                request, master_id, error="Not added: a skills group needs a label and its items.")
        current = [dict(g) for g in (master.skill_groups or [])]
        taken = {str(g.get("key")) for g in current}
        key = _unique(_slug(group[0], f"skills_{len(current) + 1}"), taken)
        master.skill_groups = [*current, {"key": key, "label": group[0], "items": group[1],
                                          "default_visible": True}]
        await s.commit()
    return await _basics_response(request, master_id, note="Skills group added.")


@router.post("/documents/{master_id}/skills/{key}", response_class=HTMLResponse)
async def edit_skill_group(request: Request, master_id: int, key: str):
    form = await request.form()
    group = _group_from_form(form)
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current = [dict(g) for g in (master.skill_groups or [])]
        row = next((g for g in current if g.get("key") == key), None)
        if row is None:
            return await _basics_response(request, master_id,
                                          error=_GONE_GROUP)
        if group is None:
            return await _basics_response(
                request, master_id,
                error="Not saved: a group needs a label and its items. Use Remove to delete it.")
        row["label"], row["items"] = group
        master.skill_groups = current
        await s.commit()
    return await _basics_response(request, master_id, note="Skills group saved.")


@router.post("/documents/{master_id}/skills/{key}/delete", response_class=HTMLResponse)
async def remove_skill_group(request: Request, master_id: int, key: str):
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current = [dict(g) for g in (master.skill_groups or [])]
        if not any(g.get("key") == key for g in current):
            return await _basics_response(request, master_id,
                                          error=_GONE_GROUP)
        master.skill_groups = [g for g in current if g.get("key") != key]
        await s.commit()
    return await _basics_response(request, master_id, note="Skills group removed.")


@router.post("/documents/slot/{slot_id}/optional", response_class=HTMLResponse)
async def set_slot_optional(request: Request, slot_id: int):
    """The per-bullet "Can be left out when it does not fit the role" box (DocSlot.optional). An
    optional bullet may be dropped by the model's selection for a role (it gains "hide" in the
    selection schema); the operator can hide any part on the Tailor page regardless."""
    form = await request.form()
    on = "1" in [str(v) for v in form.getlist("optional")]
    async with sessionmaker()() as s:
        slot = await s.get(DocSlot, slot_id)
        if slot is None:
            return HTMLResponse("not found", status_code=404)
        slot.optional = on
        await s.commit()
    return HTMLResponse('<span class="saved" role="status">Saved</span>')


# --------------------------------------------- optional sections (D-DOC-3): typed entries only
def _known_kind(kind: str) -> bool:
    from kestrel.docs.sections import KINDS

    return kind in KINDS


@router.post("/documents/{master_id}/sections/{kind}", response_class=HTMLResponse)
async def add_entry(request: Request, master_id: int, kind: str):
    """Add one entry (an education line, a certification, a license or a project) in the
    operator's own words, with a new stable id. The required field (institution or name) must
    be filled."""
    from kestrel.docs.sections import (
        ENTRIES_MAX,
        clean_entry,
        heading,
        new_entry_id,
        required_field,
    )

    if not _known_kind(kind):
        return HTMLResponse("unknown section", status_code=404)
    form = await request.form()
    entry = clean_entry(kind, form)
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        if entry is None:
            label = required_field(kind).replace("_", " ")
            return await _optional_response(
                request, master_id, open_kind=kind,
                error=f"Not added: {heading(kind)} needs the {label}.")
        current = list(getattr(master, kind) or [])
        if len(current) >= ENTRIES_MAX:
            return await _optional_response(
                request, master_id, open_kind=kind,
                error=f"Not added: {heading(kind)} holds at most {ENTRIES_MAX} entries.")
        # A new list, so the JSON change is persisted.
        setattr(master, kind, [*current, {**entry, "id": new_entry_id()}])
        await s.commit()
    return await _optional_response(request, master_id, open_kind=kind,
                                    note=f"Added to {heading(kind)}.")


def _entry_gone(kind: str) -> str:
    from kestrel.docs.sections import heading

    return (f"That entry is no longer in {heading(kind)}: it was changed or removed in another "
            "window. Nothing was saved; the list below is current.")


def _find_entry(current: list, entry_id: str) -> int | None:
    return next((i for i, e in enumerate(current)
                 if isinstance(e, dict) and e.get("id") == entry_id), None)


@router.post("/documents/{master_id}/sections/{kind}/{entry_id}", response_class=HTMLResponse)
async def edit_entry(request: Request, master_id: int, kind: str, entry_id: str):
    """Save one entry in place, addressed by its stable id (never its list position, which
    another window may have shifted). Clearing the required field is refused (remove it
    instead)."""
    from kestrel.docs.sections import clean_entry, heading, required_field, with_ids

    if not _known_kind(kind):
        return HTMLResponse("unknown section", status_code=404)
    form = await request.form()
    entry = clean_entry(kind, form)
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current, _ = with_ids(list(getattr(master, kind) or []))
        index = _find_entry(current, entry_id)
        if index is None:
            return await _optional_response(request, master_id, open_kind=kind,
                                            error=_entry_gone(kind))
        if entry is None:
            label = required_field(kind).replace("_", " ")
            return await _optional_response(
                request, master_id, open_kind=kind,
                error=f"Not saved: the {label} cannot be empty. Use Remove to delete it.")
        current[index] = {**entry, "id": entry_id}
        setattr(master, kind, current)
        await s.commit()
    return await _optional_response(request, master_id, open_kind=kind,
                                    note=f"Saved in {heading(kind)}.")


@router.post("/documents/{master_id}/sections/{kind}/{entry_id}/delete",
             response_class=HTMLResponse)
async def remove_entry(request: Request, master_id: int, kind: str, entry_id: str):
    from kestrel.docs.sections import heading, with_ids

    if not _known_kind(kind):
        return HTMLResponse("unknown section", status_code=404)
    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        if master is None:
            return HTMLResponse("not found", status_code=404)
        current, _ = with_ids(list(getattr(master, kind) or []))
        index = _find_entry(current, entry_id)
        if index is None:
            return await _optional_response(request, master_id, open_kind=kind,
                                            error=_entry_gone(kind))
        del current[index]
        setattr(master, kind, current)
        await s.commit()
    return await _optional_response(request, master_id, open_kind=kind,
                                    note=f"Removed from {heading(kind)}.")


# ------------------------------------------------------------- assess: choose the model
# The Assess popup (partials/assess_model.html) offers the current model or another one for
# this run only. A hosted pick is offered only while Hosted mode is on and acknowledged: its
# disclosure already covers the assessment ("only when you click Assess on Profile >
# Documents, your resume text"), and the same redaction plan applies. A local pick is one of
# Kestrel's derived `kestrel-` models, which carry their own num_ctx; a raw Ollama tag runs at
# Ollama's default context and would truncate the resume silently (CLAUDE.md, platform traps).
ASSESS_HOSTED_PREFIX = "hosted:"
_LOCAL_DERIVED_PREFIX = "kestrel-"


def _local_assess_models(settings) -> tuple[list[str], str | None]:
    """Kestrel's derived local models Ollama has right now, or ([], why). Sync; run it in a
    thread. Never raises."""
    from kestrel.doctor import hosted_only
    from kestrel.llm.client import OllamaClient

    backend = settings.llm.backends.get("local")
    if backend is None or backend.external:
        return [], None
    if hosted_only(settings):
        # Every tier is hosted: Ollama is not part of this setup, so it is never probed and its
        # absence is not news (D-SETUP-8); the popup offers the hosted models only.
        return [], None
    try:
        tags = OllamaClient(
            backend.base_url, settings.llm.ollama_native_url, backend.api_key or "ollama"
        ).list_models(timeout=5.0)
    except Exception as exc:  # noqa: BLE001 -- Ollama not running is a note in the popup
        return [], f"Ollama is not reachable ({type(exc).__name__})."
    return sorted(t for t in tags if t.startswith(_LOCAL_DERIVED_PREFIX)), None


async def _hosted_assess_models(settings) -> tuple[str, list, list, dict, str | None]:
    """(provider label, the provider's models, their picker rows, block reasons, note) for the
    popup's hosted group. Empty unless Hosted mode is on and a key is saved."""
    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models as hm

    found = hm.catalog_backend(settings)
    if found is None:
        return "", [], [], {}, None
    name, backend = found
    label = next((p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name)
    if not settings.llm.hosted_mode.active:
        return label, [], [], {}, (
            f"{label} models are offered here once Hosted mode is on under Settings > Hosted."
        )
    if not (backend.api_key or prov.secret_present(backend.api_key_env)):
        return label, [], [], {}, None
    try:
        raw = await hm.fetch_models(name, backend)
    except hm.ModelListError as exc:
        return label, [], [], {}, str(exc)
    from kestrel.docs.assess import VERSION
    from kestrel.llm.prompts import load_prompt

    # The assessment prompt's own tier sets the context budget a pick must meet.
    tier = load_prompt(VERSION).tier
    picker = hm.picker_models(raw)
    reasons = {m.id: why for m in picker if (why := hm.block_reason(m, tier, backend))}
    return label, raw, picker, reasons, None


@router.get("/documents/{master_id}/assess/choose", response_class=HTMLResponse)
async def assess_choose(request: Request, master_id: int):
    """The Assess popup: run with the current model, or pick another for this run only."""
    import asyncio

    from kestrel.docs.assess import VERSION
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        master = await s.get(DocMaster, master_id)
        settings = await load_settings_with_db(s)
    local, local_note = await asyncio.to_thread(_local_assess_models, settings)
    label, _raw, hosted, reasons, hosted_note = await _hosted_assess_models(settings)
    ctx = {
        "master_id": master_id,
        "missing": master is None,
        "doc_word": "resume" if master is None or master.doc_kind == "resume"
        else "capability statement",
        "current_label": await _import_model_label(VERSION),
        "local_models": local, "local_note": local_note,
        "hosted_label": label, "hosted_models": hosted, "hosted_reasons": reasons,
        "hosted_note": hosted_note, "hosted_prefix": ASSESS_HOSTED_PREFIX,
        # Kestrel runs on local models, so a hosted pick is the one external send here: the
        # popup says so before the operator confirms (Privacy).
        "hosted_while_local": settings.llm.mode != "hosted",
    }
    return templates().TemplateResponse(request, "partials/assess_model.html", ctx)


async def _assess_settings(settings, pick: str):
    """`settings` with the assessment's tier pointed at `pick` for this run only (a copy; no
    setting row is written), plus the label to show. Returns (settings, label, error)."""
    import asyncio

    from kestrel.config import HOSTED_MAX_TOKENS
    from kestrel.docs.assess import VERSION
    from kestrel.llm import providers as prov
    from kestrel.llm.prompts import load_prompt
    from kestrel.services import hosted_models as hm

    tier = load_prompt(VERSION).tier
    tcfg = settings.llm.tiers.get(tier)
    if tcfg is None:
        return None, "", f"No {tier} model is configured."
    run = settings.model_copy(deep=True)
    if pick.startswith(ASSESS_HOSTED_PREFIX):
        model_id = pick.removeprefix(ASSESS_HOSTED_PREFIX)
        label, raw, _picker, reasons, note = await _hosted_assess_models(settings)
        info = next((m for m in raw if m.id == model_id), None)
        found = hm.catalog_backend(settings)
        if info is None or found is None:
            return None, "", note or f"{model_id!r} is not in the hosted model list."
        if reasons.get(model_id):
            return None, "", f"{model_id} cannot run this: {reasons[model_id]}."
        run.llm.tiers[tier] = tcfg.model_copy(update={
            "backend": found[0], "model": model_id,
            "max_tokens": max(tcfg.max_tokens or 0, HOSTED_MAX_TOKENS.get(tier, 0)) or None,
        })
        run.llm.hosted_model_caps[model_id] = prov.model_caps(info)
        return run, f"{model_id} ({label}, this run only)", None
    local, note = await asyncio.to_thread(_local_assess_models, settings)
    if pick not in local:
        return None, "", note or f"{pick!r} is not one of the local Kestrel models Ollama has."
    run.llm.tiers[tier] = tcfg.model_copy(update={"backend": "local", "model": pick})
    return run, f"{pick} (local, this run only)", None


@router.post("/documents/{master_id}/assess", response_class=HTMLResponse)
async def assess(request: Request, master_id: int):
    """Resume assessment (P13, D-DOC-2): how this document reads for the operator's target
    titles. Advisory and displayed only; nothing is written to the document. A failed or
    quarantined reply is a message in the partial, never a 500. An optional `model` field (the
    Assess popup) runs this one assessment on another model; no setting changes."""
    import httpx

    from kestrel.docs.assess import VERSION, assess_master
    from kestrel.docs.catalog import load_catalog
    from kestrel.llm.client import LLMClient
    from kestrel.services.setting_overrides import load_settings_with_db

    form = await request.form()
    pick = str(form.get("model") or "").strip()[:240]
    ctx: dict = {"result": None, "error": None,
                 "model_label": await _import_model_label(VERSION)}
    if "model" in form and not pick:
        # The "other model" form was submitted with nothing picked (script off or bypassed): ask
        # for a pick rather than quietly running the current model.
        ctx.update(error="Pick a model first, or use the current model.", model_label="")
        return templates().TemplateResponse(request, "partials/doc_assessment.html", ctx)
    try:
        async with sessionmaker()() as s:
            catalog = await load_catalog(s, master_id)
            if catalog is None:
                ctx["error"] = "That document no longer exists."
            else:
                settings = await load_settings_with_db(s)
                if pick:
                    run, label, why = await _assess_settings(settings, pick)
                    if run is None:
                        ctx.update(error=f"Not assessed: {why}", model_label="")
                        return templates().TemplateResponse(
                            request, "partials/doc_assessment.html", ctx
                        )
                    settings = run
                    ctx["model_label"] = label
                async with httpx.AsyncClient() as http:
                    result = await assess_master(
                        LLMClient(settings, http=http), catalog, session=s,
                        redact=bool(settings.confidential_mode),
                    )
                await s.commit()  # a quarantine row the guard staged (hard rule 3)
                ctx.update(result=result, error=result.error)
    except Exception as exc:  # a model that is not running must not 500 the page
        log.warning("resume_assess_route_failed", master_id=master_id,
                    error=f"{type(exc).__name__}: {exc}")
        ctx["error"] = f"Could not reach the model: {type(exc).__name__}: {exc}"
    return templates().TemplateResponse(request, "partials/doc_assessment.html", ctx)


_UPLOAD_CHUNK = 1024 * 1024


async def _read_upload_within_limit(upload, filename: str) -> bytes:
    """Read an upload up to convert.MAX_BYTES and refuse past it (FileTooLarge, the same message
    the converter gives), so an oversize file is never fully buffered: the declared size is
    checked first when the multipart parser knows it, then the body is read in chunks."""
    limit = convert_mod.MAX_BYTES
    declared = getattr(upload, "size", None)
    if isinstance(declared, int) and declared > limit:
        raise convert_mod.FileTooLarge(convert_mod._too_large(filename))
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise convert_mod.FileTooLarge(convert_mod._too_large(filename))
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_source(form) -> dict:
    """The import source as markdown: an uploaded file (.md, .txt, .docx, .pdf, .doc) that
    kestrel.docs.convert turns into markdown, or pasted text. Returns the partial's context; a
    conversion failure is an `error` in it, never a 500."""
    import asyncio

    from kestrel.docs.convert import ConvertError

    track = str(form.get("track") or "employment")
    doc_kind = str(form.get("doc_kind") or "resume")
    ctx: dict = {"ob": form.get("ob") == "1",  # in the setup wizard (D-UI-65)
                 "proposal": None, "markdown": "", "track": track, "doc_kind": doc_kind,
                 "warnings": [], "source_format": None, "filename": None, "error": None}
    upload = form.get("file")
    filename = str(getattr(upload, "filename", "") or "")
    if upload is not None and filename:
        ctx["filename"] = filename
        try:
            data = await _read_upload_within_limit(upload, filename)
            # pypdf, python-docx and the LibreOffice subprocess (up to 120 s) are all
            # synchronous: off the event loop, or every other request waits behind them.
            converted = await asyncio.to_thread(
                convert_mod.convert_to_markdown, data, filename=filename
            )
        except ConvertError as exc:
            ctx["error"] = str(exc)
            return ctx
        except Exception as exc:  # a parser crash on a hostile file is a message, not a 500
            ctx["error"] = f"Could not read {filename!r}: {type(exc).__name__}: {exc}"
            return ctx
        ctx.update(markdown=converted.markdown.strip(), warnings=list(converted.warnings),
                   source_format=converted.source_format)
        if not ctx["markdown"]:
            ctx["error"] = f"{filename!r} contained no text to import."
        return ctx
    # Pasted text, or the second (processing) step re-posting what the first step read.
    ctx["markdown"] = str(form.get("text") or "").strip()
    ctx["source_format"] = str(form.get("source_format") or "") or None
    ctx["filename"] = str(form.get("filename") or "") or None
    if not ctx["markdown"]:
        ctx["error"] = "Paste some text or choose a file to import first."
    return ctx


async def _import_model_label(version: str = "resume_outline/v1") -> str:
    """The model a prompt will run on (its tier's model, local or hosted), for the processing
    message: the P7 prompts' by default, P13's for the assessment."""
    from kestrel.services.setting_overrides import load_settings_with_db

    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
        from kestrel.llm.prompts import load_prompt

        tier = settings.llm.tiers.get(load_prompt(version).tier)
        backend = settings.llm.backends.get(tier.backend) if tier else None
    except Exception as exc:  # the label is a courtesy; its absence must not fail the import
        log.warning("import_model_label_failed", error=str(exc))
        return ""
    if tier is None:
        return ""
    external = backend is not None and backend.external
    if not external:
        where = "local"
    elif settings.llm.mode == "hosted":
        where = "hosted"
    else:
        where = "external backend, but Hosted mode is off: the call will be refused"
    return f"{tier.model} ({where})"


@router.post("/api/documents/import/read", response_class=HTMLResponse)
async def import_read(request: Request):
    """Step one of the import (D-IMPORT-2): upload and convert, then show the text that was read
    with a processing panel that starts step two (/api/documents/import) on load. Splitting the
    two makes the status honest (the operator sees Uploading, then Reading, then Processing with
    the model named, not one silent minute) and lets a failed proposal retry on the same text
    without uploading again."""
    form = await request.form()
    ctx = await _read_source(form)
    if ctx["error"] is None:
        ctx["model_label"] = await _import_model_label()
        ctx["confidential_send"] = await _import_confidential_send(ctx["markdown"])
    return templates().TemplateResponse(request, "partials/import_read.html", ctx)


def _confirm_hash(markdown: str, provider: str, model: str) -> str:
    """What an unredacted-import confirmation is bound to (D-PRIV-1): this text, this provider,
    this model. A Try again with any of them changed needs a fresh confirmation."""
    import hashlib
    import json as _json

    blob = _json.dumps([markdown, provider, model], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _import_confidential_send(markdown: str = "") -> dict | None:
    """What the import would send online under confidential mode (D-PRIV-1), or None when it
    stays on this machine or confidential mode is off.

    {"provider", "mapping", "needs_confirm"}: with an employer marked (or inferred), the line
    text goes with that employer as a token; with none, the whole resume goes as written, which
    the operator must confirm (a server-side field, `confirm_unredacted`)."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import plan_hosted_send
    from kestrel.privacy import EmployerMap
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.pages.models import _provider_label

    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)
            if not settings.confidential_mode:
                return None
            tier = load_prompt("resume_outline/v1").tier
            plan = await plan_hosted_send(s, settings, tier)
        if not plan.external:
            return None
        tcfg = settings.llm.tiers[tier]
        provider = _provider_label(tcfg.backend)
        model = tcfg.model
    except Exception as exc:  # unknown means ask: privacy copy fails closed
        log.warning("import_confidential_check_failed", error=str(exc))
        return {"provider": "an online model", "mapping": EmployerMap(), "needs_confirm": True,
                "shown": "", "confirm": _confirm_hash(markdown, "?", "?")}
    shown = sorted({e.shown_as for e in plan.mapping.employers})
    # D-PRIV-1: ALWAYS confirmed under confidential mode on an online model: the resume may name
    # an employer nothing marks yet (the one being imported). Marked names still go coded.
    return {"provider": provider, "mapping": plan.mapping, "needs_confirm": True,
            "shown": ", ".join(f'"{x}"' for x in shown),
            "confirm": _confirm_hash(markdown, provider, model)}


@router.post("/api/documents/import", response_class=HTMLResponse)
async def import_markdown(request: Request):
    """Run the P7 import proposal and show it for review with verbatim flagging. Writes NOTHING: a
    variant whose text is not verbatim from the source is flagged (hard rule 2), and the operator
    accepts only after reviewing. A failed model call returns the error, never prose, with a
    retry on the same text.

    The source is either pasted markdown or an uploaded file (.md, .txt, .docx, .pdf, .doc) that
    kestrel.docs.convert turns into markdown. The extracted markdown is shown above the proposal so
    the operator sees exactly what was read, and it is the text the verbatim check runs against. A
    conversion failure is a message in the partial, never a 500. `extracted_shown` is set by the
    processing step of /api/documents/import/read, which already showed the extracted text."""
    import httpx

    from kestrel.docs.imports import propose_import
    from kestrel.llm.client import LLMClient
    from kestrel.services.setting_overrides import load_settings_with_db

    form = await request.form()
    ctx = await _read_source(form)
    ctx["extracted_shown"] = bool(form.get("extracted_shown"))
    if ctx["error"] is not None:
        return templates().TemplateResponse(request, "partials/import_proposal.html", ctx)
    import hmac

    send = await _import_confidential_send(ctx["markdown"])
    ctx["confidential_send"] = send
    posted = str(form.get("confirm_unredacted") or "")
    if send and send["needs_confirm"] and not hmac.compare_digest(posted, send["confirm"]):
        # D-PRIV-1: nothing is marked, so the whole resume would go as written. The confirmation
        # is bound to this text, provider and model; a missing or stale one is refused here, and
        # the reply carries a fresh one behind the same warning.
        ctx["error"] = (f"Not sent. {_UNREDACTED_WARNING.format(provider=send['provider'])} "
                        "Press Send and import to confirm.")
        ctx["retryable"] = True  # the form below carries a FRESH confirmation
        return templates().TemplateResponse(request, "partials/import_proposal.html", ctx)
    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)  # the setting-table layer: hosted rows
        async with httpx.AsyncClient() as http:
            client = LLMClient(settings, http=http)
            proposal = await propose_import(
                client, ctx["markdown"], source_format=ctx["source_format"],
                mapping=send["mapping"] if send else None,
            )
        ctx.update({"proposal": proposal, "error": proposal.error})
        ctx["current_offer"] = _current_offer(proposal, ctx["track"])
    except Exception as exc:  # a model that is not running must not 500 the page
        ctx["error"] = f"Import could not reach the model: {type(exc).__name__}: {exc}"
    ctx["retryable"] = ctx["error"] is not None
    return templates().TemplateResponse(request, "partials/import_proposal.html", ctx)


_UNREDACTED_WARNING = (
    "This sends your whole resume to {provider}, including your current employer's name. "
    "Confidential mode can hide it only after you mark your current employer."
)


def _current_offer(proposal, track: str) -> dict | None:
    """The proposal's opt-in "Mark <company> as my current employer" (D-PRIV-1), offered only
    when the newest role is open-ended. Unticked by default: the import sets nothing on its own."""
    from kestrel.docs.current_employer import suggested_placeholder
    from kestrel.docs.imports import open_ended_newest_role

    role = open_ended_newest_role(getattr(proposal, "value", None))
    if role is None:
        return None
    return {"company": role.company_label.strip(), "hint": suggested_placeholder(track)}


@router.post("/api/documents/import/accept", response_class=HTMLResponse)
async def import_accept(request: Request):
    """ACCEPT the proposal the operator REVIEWED, not a fresh model re-run. The reviewed proposal is
    round-tripped as JSON in the form, so what is written is exactly what was shown; accept_import
    still re-validates verbatim/defaults/dup keys and raises on any violation before writing (the
    enforcement boundary), so a hallucinated bullet can never be written even here."""
    from kestrel.docs.imports import ImportRejected, accept_import, place_unplaced
    from kestrel.llm.schemas import P7ResumeImport

    form = await request.form()
    if form.get("ob") == "1" and form.get("approved") != "1":
        # The wizard's approval is enforced here, not only by the disabled button (review
        # finding 11): nothing is written, and the hint shows in place under the proposal,
        # which stays on screen as it was.
        return HTMLResponse(
            '<p class="warnbox" id="ob-approve-error" data-testid="ob-approve-error" '
            'role="alert">Approve your resume text first: tick "I approve this resume text", '
            "then Save resume and continue.</p>",
            headers={"HX-Retarget": "#ob-approve-error", "HX-Reswap": "outerHTML"},
        )
    markdown = str(form.get("text") or "").strip()
    track = str(form.get("track") or "employment")
    doc_kind = str(form.get("doc_kind") or "resume")
    proposal_json = str(form.get("proposal_json") or "")
    try:
        value = P7ResumeImport.model_validate_json(proposal_json)
        choices = list(zip(form.getlist("unplaced_line"), form.getlist("unplaced_to"),
                           strict=False))
        if any(to for _line, to in choices):
            value = place_unplaced(value, markdown, [(str(a), str(b)) for a, b in choices])
        from kestrel.docs.current_employer import clean_placeholder, suggested_placeholder

        current = None
        if "on" in form.getlist("current_employer"):
            # Ticked with the field cleared: the suggestion that was prefilled, never nothing,
            # so a ticked box always takes effect.
            current = (clean_placeholder(form.get("current_employer_as"))
                       or suggested_placeholder(track))
        async with sessionmaker()() as s:
            master_id = await accept_import(
                s, value, track=track, doc_kind=doc_kind,
                name=f"Imported {doc_kind}", source_markdown=markdown,
                current_employer=current,
            )
        if form.get("ob") == "1":
            # The wizard's "Save resume and continue" (D-UI-65): saved, so the step moves on.
            from kestrel.web.pages.onboarding import advance_after

            return await advance_after(request, "resume")
        return HTMLResponse(
            '<div class="panel" style="padding:1rem"><p class="covered">Created.</p>'
            f'<a class="btn" href="/documents?master_id={master_id}">Open it</a></div>'
        )
    except ImportRejected as exc:
        return HTMLResponse(f'<div class="warnbox">Rejected at the write boundary: {exc}</div>')
    except Exception as exc:  # bad/absent proposal JSON, or a DB error
        return HTMLResponse(f'<div class="warnbox">Could not accept the import: {exc}</div>')
