"""Tailor resume (D-UI-120, SPEC-INTERFACES 2.1 `/renders/{id}`): the render review.

The page shows the operator's OWN wordings, picked per role, beside a live preview built the way
the PDF is (resolve() then render_html(), byte for byte from doc_variant.text, hard rule 2).
Every swap WRITES a `selection_override` row before it changes the config (the audit trail the
learning loop reads, SPEC-MODULES 5). A pick that fails the catalog check is never defaulted
away: its slot reads "Needs your pick" and Download and Use for this application ask first.

/renders/new is the entry point from Documents and from a role: it creates a DocRender through
kestrel.docs.service.render_document and redirects here. Render failures (no Edge on this machine,
e.g. a cloud agent) are caught and shown inline, never a 500 (CLAUDE.md environment split).

Updates are in place. A swap answers with out-of-band fragments (the card that changed, the
preview source, the page guard, the header and the coverage lists), and the preview frame is
patched by tailor.js without reloading. Pick again, the one slow action, may send the operator's
wordings to an online model; it asks first and the server checks the ask again."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    SelectionOverride,
    Setting,
    utcnow_iso,
)
from kestrel.docs.catalog import (
    MasterCatalog,
    load_catalog,
    operator_slot_keys,
    resolve,
)
from kestrel.docs.render import render_html
from kestrel.learning.capture import choice_texts
from kestrel.pipeline._shared import tier_is_hosted
from kestrel.services import pursuit_undo
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import tailor_view as tv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import fixed_reason, plain_error
from kestrel.web.pipeline_view import pay_text, place_text

log = structlog.get_logger()

router = APIRouter()

PICK_TIER = "docsel"
# What a pick sends, beyond the tier line of the Hosted mode disclosure (which comes from
# `disclosure_terms`, the text the operator acknowledged, never from words written here).
_PICK_SENDS = "your wordings for this resume and a short summary of the role"

# The printed page in the preview frame: white paper with its half-inch margin in every theme
# (the document paints its own sheet), and the highlight and flash for a hovered or swapped line.
# Preview only; the PDF never carries it. These are the document's own paper colors, not theme
# colors: a resume page is white in any theme.
PREVIEW_SHEET = (
    "<style>html{background:#fff}body{padding:.5in;min-height:11in}"
    'body[data-margins="narrow"]{padding:.35in}'
    "[data-slot]{border-radius:3px;transition:background .2s ease,box-shadow .2s ease}"
    "[data-hl]{background:#E4E8FF;box-shadow:0 0 0 2px #E4E8FF}"
    ".tl-flash{animation:tlflash .9s ease}"
    "@keyframes tlflash{0%{background:#C9F0DA;box-shadow:0 0 0 2px #C9F0DA}"
    "100%{background:transparent;box-shadow:none}}</style>"
)
PAGE_PX = (816, 1056)  # a Letter page at 96 dpi: what the preview is laid out at


# ------------------------------------------------------------------------------ small helpers
def _look_params(params) -> dict:
    """`font`, `header_align`, `accent`, `size` and `margins` from a query string or form, kept
    only when they name a listed value or a valid #rrggbb (kestrel.docs.styles), so nothing else
    can reach a render config's look."""
    from kestrel.docs.styles import ALIGNS, FONTS, MARGINS, SIZES, accent_from_params

    out: dict = {}
    font = str(params.get("font") or "")
    if font in FONTS:
        out["font"] = font
    align = str(params.get("header_align") or "")
    if align in ALIGNS:
        out["header_align"] = align
    size = str(params.get("size") or "")
    if size in SIZES and size != "default":
        out["size"] = size
    margins = str(params.get("margins") or "")
    if margins in MARGINS and margins != "normal":
        out["margins"] = margins
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


def _events(events: dict[str, Any] | None) -> dict[str, str]:
    """An HX-Trigger header. ensure_ascii keeps it latin-1 safe for the header."""
    return {"HX-Trigger": json.dumps(events, ensure_ascii=True)} if events else {}


def _toast(message: str, undo_url: str | None = None) -> dict:
    body: dict[str, Any] = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    return body


def _words(opp: Opportunity | None) -> dict[str, str]:
    """The operator's word for the thing a render is attached to."""
    con = opp is not None and opp.track == "consulting"
    return {"app": "lead" if con else "application", "apps": "Leads" if con else "Applications"}


# ------------------------------------------------------------------------------ the page's data
def _short(text: str, n: int = 44) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


async def _overrides(session: AsyncSession, render_id: int) -> dict[str, SelectionOverride]:
    """The latest operator override per part for this render (a later one replaces an earlier)."""
    rows = (
        await session.execute(
            select(SelectionOverride)
            .where(SelectionOverride.doc_render_id == render_id)
            .order_by(SelectionOverride.id)
        )
    ).scalars()
    return {o.slot_key: o for o in rows}


def _pick_state(render: DocRender) -> dict[str, Any]:
    """Who picked this render's wordings: Kestrel ("picked", with the model and the time when
    known), nobody yet ("none": the operator's first wording shows everywhere), or a pick that
    failed outright ("failed")."""
    config = render.config or {}
    meta = config.get("pick_meta") or {}
    if meta:
        return {
            "state": "picked", "model": meta.get("model") or "", "online": bool(meta.get("online")),
            "seconds": meta.get("seconds"), "at": meta.get("at") or "",
        }
    if render.rationale:
        return {"state": "picked", "model": "", "online": False, "seconds": None, "at": ""}
    if render.defaults_used:
        return {"state": "failed", "model": "", "online": False, "seconds": None, "at": ""}
    return {"state": "none", "model": "", "online": False, "seconds": None, "at": ""}


def _needs(config: dict, doc) -> dict[str, str | None]:
    """The parts that still need the operator's pick: the ones the model named a wording for that
    the operator never wrote (`config.needs_pick`, set by Pick again), and any part whose saved
    key the catalog no longer has (`doc.unresolved`). Keys are slot keys, or "summary"."""
    out: dict[str, str | None] = dict(config.get("needs_pick") or {})
    for what in doc.unresolved:
        if what != "layout":
            out.setdefault(what, doc.unresolved_keys.get(what))
    return out


def _section_head(sec: DocSection, show: Callable[[str], str]) -> str:
    bits = [sec.org_label, sec.title]
    when = " to ".join(x for x in (sec.start_date, sec.end_date) if x)
    head = " · ".join(show(b) for b in bits if b)
    return f"{head} · {when}" if head and when else head or when


async def _evaluation_blocks(session: AsyncSession, opportunity_id: int | None) -> dict | None:
    """The newest evaluation's blocks that carry a fit read (the same source the pick used)."""
    if not opportunity_id:
        return None
    rows = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opportunity_id)
            .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
        )
    ).scalars()
    for ev in rows:
        if (ev.blocks or {}).get("b_fit"):
            return ev.blocks
    return None


async def _fit_score(session: AsyncSession, opportunity_id: int | None) -> float | None:
    if not opportunity_id:
        return None
    rows = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opportunity_id)
            .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
        )
    ).scalars()
    for ev in rows:
        v = ev.weighted_score if ev.weighted_score is not None else ev.score
        if v is not None:
            return round(max(0.0, min(5.0, float(v))), 1)
    return None


def _preview_srcdoc(doc) -> str:
    page = render_html(doc)
    return page.replace("<head>", "<head>" + PREVIEW_SHEET, 1) if "<head>" in page else (
        PREVIEW_SHEET + page
    )


def _chosen_label(hidden: bool, index: int | None, n: int) -> str:
    """ "Left out", "Wording 2 of 3" or "Your only wording": which of the operator's wordings
    shows, for the What Kestrel chose tab."""
    if hidden:
        return "Left out"
    if index is None or n < 2:
        return "Your only wording"
    return f"Wording {index + 1} of {n}"


def _layout_label(key: str) -> str:
    return (key or "").replace("_", " ").replace("-", " ").strip().capitalize() or "Standard"


async def _employer_for(session: AsyncSession, settings: Settings, master_id: int | None) -> str:
    """What happens to the current employer in a send about THIS document, as
    `plan_hosted_send` will decide it: "hidden", "held" (confidential mode and none marked in
    this document) or "off"."""
    if not settings.confidential_mode or not settings.llm.hosted_mode.redact_confidential:
        return "off"
    from kestrel.docs.current_employer import load_employer_map

    return "hidden" if await load_employer_map(session, master_id=master_id) else "held"


async def _pick_route(
    session: AsyncSession, settings: Settings, master_id: int | None = None
) -> dict[str, Any]:
    """Where a pick runs, as the call will actually be routed: on this computer, or by an online
    model (Hosted mode on and the document-selection tier on an external backend). An online route
    carries what is sent (the tier's line of the Hosted mode disclosure, the text the operator
    acknowledged) and what happens to the current employer. The server checks it again."""
    if not tier_is_hosted(settings, PICK_TIER):
        return {"online": False}
    from kestrel.services.online_consent import destination_id
    from kestrel.web.pages.needs_try import _hosted_models

    tcfg = settings.llm.tiers[PICK_TIER]
    label = (await _hosted_models(settings))[0]
    return {
        "online": True,
        "model": tcfg.model,
        "dest": destination_id(settings, PICK_TIER),
        "provider": label or tcfg.backend or "the online service",
        "sends": settings.llm.hosted_mode.disclosure_terms()["sends"][PICK_TIER],
        "pick_sends": _PICK_SENDS,
        "employer": await _employer_for(session, settings, master_id),
    }


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
        "groups": [],
        "summary_card": None,
        "skills_card": None,
        "preview_html": None,
        "catalog_missing": False,
        "max_pages": settings.docs.max_pages,
        "is_con": False,
        "kind_word": "resume",
        "w": _words(None),
        "needs": {},
        "needs_n": 0,
        "attached": False,
        "facts": "",
        "fit": None,
        "reqs": [],
        "kw": [],
        "has_posting": False,
        "posting": None,
        "has_eval": False,
        "changes": [],
        "guard": {"limit": settings.docs.max_pages, "pages": None, "stale": False,
                  "optional": [], "over": False},
        "pick": {"state": "none", "model": "", "online": False, "seconds": None, "at": ""},
        "privacy": {"on": False, "forced": False, "marked": False, "label": ""},
        "look": None,
        "route": {"online": False},
        "dl_name": "resume.pdf",
    }
    if render is None:
        return ctx
    master = await session.get(DocMaster, render.doc_master_id)
    ctx["master"] = master
    ctx["is_con"] = bool(master and master.doc_kind != "resume")
    ctx["kind_word"] = "capability statement" if ctx["is_con"] else "resume"
    ctx["title"] = "Tailor " + ctx["kind_word"]
    opp = await session.get(Opportunity, render.opportunity_id) if render.opportunity_id else None
    ctx["opportunity"] = opp
    ctx["w"] = _words(opp)
    if opp is not None:
        org = await session.get(Org, opp.org_id) if opp.org_id else None
        ctx["org_name"] = (org.name if org else None) or opp.org_name
        pursuit = (
            await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
        ).scalar_one_or_none()
        ctx["pursuit"] = pursuit
        ctx["attached"] = bool(pursuit and pursuit.document_id == render.id)
        ctx["facts"] = " · ".join(x for x in (place_text(opp), pay_text(opp)) if x)
        ctx["fit"] = await _fit_score(session, opp.id)
    catalog = await load_catalog(session, render.doc_master_id)
    if catalog is None:
        ctx["catalog_missing"] = True
        return ctx
    config = dict(render.config or {})
    # Confidential mode redacts the current employer everywhere the preview shows it. The cards
    # are the operator's editor: real text (D-PRIV-1), so only the preview and the PDF redact.
    view_config = dict(config)
    view_config["redact_orgs"] = bool(config.get("redact_orgs") or settings.confidential_mode)
    doc = resolve(catalog, view_config)
    ctx["preview_html"] = _preview_srcdoc(doc)
    ctx["needs"] = _needs(config, doc)
    overrides = await _overrides(session, render.id)
    pick = _pick_state(render)
    ctx["pick"] = pick
    rationale = render.rationale or {}
    chosen = config.get("slots") or {}

    def card_state(key: str, hidden: bool, current: str | None) -> str:
        if key in ctx["needs"]:
            return "needs"
        ov = overrides.get(key)
        if ov is not None and ov.operator_choice == current:
            return "you"
        if hidden:
            return "out"
        return "picked" if pick["state"] == "picked" else "first"

    lines: list[tv.Line] = []
    shown_text: list[str] = []
    vocab: list[str] = []
    order = 0
    for sec in catalog.sections:
        cards = []
        n = 0
        for sl in sec.slots:
            n += 1
            order += 1
            key = chosen.get(sl.key, sl.default_variant_key)
            variant = next((v for v in sl.variants if v.key == key), None)
            hidden = key == "hide"
            shown = variant
            if variant is None and not hidden:  # unresolved: the page shows the default
                shown = next((v for v in sl.variants if v.key == sl.default_variant_key), None) or (
                    sl.variants[0] if sl.variants else None
                )
            state = card_state(sl.key, hidden, key)
            if shown is not None and not hidden:
                lines.append(tv.Line(sl.key, shown.text, tuple(shown.tags or [])))
                shown_text.append(shown.text)
                vocab.extend(shown.tags or [])
            cards.append(
                {
                    "key": sl.key, "label": f"Line {n}", "optional": sl.optional, "state": state,
                    "text": shown.text if shown is not None else "", "hidden": hidden,
                    "current": key, "reason": rationale.get(sl.key),
                    "wordings": [{"key": v.key, "text": v.text} for v in sl.variants],
                    "n": len(sl.variants), "bad": ctx["needs"].get(sl.key),
                    "bad_model": sl.key in (config.get("needs_pick") or {}),
                    "short": _short(shown.text if shown is not None else ""),
                    "mine": state == "you",
                    "chosen_label": _chosen_label(
                        hidden,
                        [v.key for v in sl.variants].index(shown.key) if shown else None,
                        len(sl.variants),
                    ),
                    "order": order,
                }
            )
        if cards:
            ctx["groups"].append({"title": _section_head(sec.section, lambda t: t), "cards": cards})
    # The summary, listed first: the operator's summary wordings, picked by key.
    svars = [{"key": str(v.get("key")), "text": str(v.get("text") or "")}
             for v in catalog.summary_variants if v.get("key")]
    if svars:
        skey = config.get("summary")
        hidden = bool(config.get("hide_summary"))
        cur = next((v for v in svars if v["key"] == skey), None)
        shown = cur or (svars[0] if "summary" in ctx["needs"] else None)
        if shown:
            lines.append(tv.Line("summary", shown["text"]))
            shown_text.append(shown["text"])
        state = card_state("summary", hidden, skey)
        ctx["summary_card"] = {
            "key": "summary", "label": "What I do" if ctx["is_con"] else "Summary",
            "optional": False, "state": state, "text": (shown or {}).get("text", ""),
            "bad_model": "summary" in (config.get("needs_pick") or {}),
            "hidden": hidden, "current": skey, "reason": rationale.get("summary"),
            "wordings": svars, "n": len(svars), "bad": ctx["needs"].get("summary"),
            "short": _short((shown or {}).get("text", "")), "mine": state == "you",
            "chosen_label": _chosen_label(
                hidden, [v["key"] for v in svars].index(shown["key"]) if shown else None,
                len(svars),
            ),
        }
    # Skills: one switch per group; what prints is the visible groups.
    visible = config.get("skill_groups") or {}
    groups = list(catalog.skill_groups)
    if groups:
        hide_all = bool(config.get("hide_skills"))
        sk = []
        for g in groups:
            on = bool(visible.get(g.get("key"), g.get("default_visible", True))) and not hide_all
            label = str(g.get("label") or g.get("key"))
            items = ", ".join(g.get("items") or [])
            sk.append({"key": str(g.get("key")), "label": label, "list": items, "on": on})
            vocab.extend(g.get("items") or [])
            if on:
                shown_text.append(f"{label}: {items}")
        ctx["skills_card"] = {
            "key": "skills", "label": "How I work" if ctx["is_con"] else "Skills", "groups": sk,
            "state": "you" if any(k.startswith("skills:") for k in overrides) else (
                "picked" if pick["state"] == "picked" else "first"
            ),
        }
    ctx["needs_n"] = len(ctx["needs"])
    names = {c["key"]: c["label"] for g in ctx["groups"] for c in g["cards"]}
    names["summary"] = (ctx["summary_card"] or {}).get("label") or "Summary"
    ctx["need_rows"] = [
        {"slot": k, "label": names.get(k, k.replace("_", " ")), "bad": v}
        for k, v in ctx["needs"].items()
    ]
    # Page guard: the saved page count is the PDF's; the page estimates the live one from the
    # preview and says which optional lines are showing, in catalog order.
    optional = [
        {"key": c["key"], "label": c["label"], "short": c["short"], "group": g["title"]}
        for g in ctx["groups"] for c in g["cards"] if c["optional"] and not c["hidden"]
    ]
    ctx["guard"] = {
        "limit": settings.docs.max_pages, "pages": render.page_count,
        "stale": bool(config.get("stale")), "optional": optional,
        "over": bool(render.page_count and render.page_count > settings.docs.max_pages
                     and not config.get("stale")),
    }
    # Coverage from the role's evaluation, and the posting's words against the printed text.
    blocks = await _evaluation_blocks(session, render.opportunity_id)
    reqs = tv.requirements(blocks, lines)
    ctx["reqs"] = reqs
    ctx["has_eval"] = blocks is not None
    gaps = [g.get("requirement", "") for g in ((blocks or {}).get("b_fit") or {}).get("gaps") or []]
    ctx["has_posting"] = bool(opp and (opp.description_md or "").strip())
    ctx["kw"] = tv.posting_words(
        opp.description_md if opp else None, vocabulary=vocab,
        shown_text="\n".join(shown_text), gap_phrases=gaps,
    )
    ctx["posting"] = {
        "rows": tv.posting_blocks(opp.description_md if opp else None, reqs),
        "url": (opp.url if opp and str(opp.url or "").lower().startswith(("http://", "https://"))
                else ""),
        "place": place_text(opp) if opp else "", "pay": pay_text(opp) if opp else "",
        "posted": (opp.date_posted if opp else None) or "",
    } if opp is not None else None
    # Privacy: the employer toggle. Confidential mode forces it on; with no employer marked in
    # Documents there is nothing to hide, and the page says so.
    marked = [s.section.org_label_redacted for s in catalog.sections
              if getattr(s.section, "org_label_redacted", None)]
    ctx["privacy"] = {
        "on": bool(config.get("redact_orgs") or settings.confidential_mode),
        "forced": bool(settings.confidential_mode),
        "marked": bool(marked), "label": marked[0] if marked else "",
    }
    # Look: this application's layout, size and margins against the document's own saved look.
    saved = dict((master.look if master else None) or {})
    first_layout = catalog.layouts[0].key if catalog.layouts else ""
    profile = {
        "layout": saved.get("layout") or first_layout,
        "size": saved.get("size") or "default", "margins": saved.get("margins") or "normal",
    }
    here = {
        "layout": config.get("layout") or profile["layout"],
        "size": config.get("size") or profile["size"],
        "margins": config.get("margins") or profile["margins"],
    }
    ctx["look"] = {
        "layouts": [{"key": lay.key, "label": _layout_label(lay.key)} for lay in catalog.layouts],
        "here": here, "profile": profile, "same": here == profile,
        "profile_label": _layout_label(profile["layout"]),
    }
    ctx["changes"] = [
        {"label": f"{g['title'] + ' · ' if g['title'] else ''}{c['label']}", "card": c}
        for g in ctx["groups"] for c in g["cards"]
    ]
    ctx["route"] = await _pick_route(session, settings, render.doc_master_id)
    ctx["dl_name"] = render.filename or "resume.pdf"
    return ctx


# ------------------------------------------------------------------------------ the page
@router.get("/renders/new", response_class=HTMLResponse)
async def new_render(request: Request):
    """Create a DocRender for a master (optionally for a specific opportunity) using the master's
    default selection, and redirect to its review page. Any failure, including a missing Edge,
    since render_document calls Playwright, is shown inline instead of raising, so this route
    never 500s even where Playwright is not installed (a cloud agent, a fresh checkout)."""
    from kestrel.docs.catalog import default_config
    from kestrel.docs.service import render_document

    master_id_raw = request.query_params.get("master_id")
    opportunity_id_raw = request.query_params.get("opportunity_id")
    if not master_id_raw:
        ctx = {
            "title": "Tailor resume",
            "query": dict(request.query_params),
            "back": back_target(request.query_params),
            "render": None,
        }
        # A bare visit (a typed address, an old bookmark) shows the page's calm empty state
        # (how to make a version), never an error page.
        return templates().TemplateResponse(request, "renders.html", ctx)
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
    except Exception as exc:  # Playwright/Edge missing, bad id, or any render failure: never 500
        log.warning("test_render_failed", error_type=type(exc).__name__, error=str(exc))
        ctx = {
            "title": "Tailor resume",
            "query": dict(request.query_params),
            "back": back_target(request.query_params),
            "render": None,
            "new_render_error": f"Could not create a test render: {plain_error(exc)}",
        }
        return templates().TemplateResponse(request, "renders.html", ctx, status_code=200)
    if result is None:
        ctx = {
            "title": "Tailor resume",
            "query": dict(request.query_params),
            "back": back_target(request.query_params),
            "render": None,
            "new_render_error": f"No such document master: {master_id}",
        }
        return templates().TemplateResponse(request, "renders.html", ctx, status_code=404)
    back = back_target(request.query_params)
    suffix = f"?from={back}" if back else ""
    return RedirectResponse(f"/renders/{result.doc_render_id}{suffix}", status_code=303)


async def _page_response(
    request: Request, session: AsyncSession, render: DocRender | None, **extra: Any
) -> Any:
    settings = await load_settings_with_db(session)
    ctx = await _build_ctx(request, session, render, settings)
    ctx.update(extra)
    ctx["ctx"] = ctx  # the partials' macros take the page's data as one argument
    return ctx


@router.get("/renders/{render_id}", response_class=HTMLResponse)
async def page(request: Request, render_id: int):
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        ctx = await _page_response(request, s, render)
    ctx["render_id_requested"] = render_id
    tmpl = "partials/render_review.html" if request.headers.get("HX-Request") else "renders.html"
    return templates().TemplateResponse(request, tmpl, ctx)


def _fragments(request: Request, ctx: dict, *, events: dict | None = None,
               headers: dict | None = None) -> Any:
    """The in-place update after a change: only the parts that changed come back, each out of
    band, and the page keeps its scroll, its open card and its preview frame."""
    return templates().TemplateResponse(
        request, "partials/tailor_update.html", ctx,
        headers={**_events(events), **(headers or {})},
    )


# ------------------------------------------------------------------------------ swapping
def _choice_for(
    catalog: MasterCatalog, config: dict, slot_key: str
) -> tuple[set[str] | None, str | None]:
    """(the keys the operator may set this part to, the config's CURRENT value for it). The part
    is a bullet slot, "summary", "skills" or "skills:<group>". The current value is what the
    override row records as the model's choice."""
    if slot_key == "summary":
        keys = {str(v.get("key")) for v in catalog.summary_variants if v.get("key")}
        return (keys | {"hide"} if keys else None), (
            "hide" if config.get("hide_summary") else config.get("summary")
        )
    if slot_key == "skills":
        return {"show", "hide"}, ("hide" if config.get("hide_skills") else "show")
    if slot_key.startswith("skills:"):
        group = next(
            (g for g in catalog.skill_groups if f"skills:{g.get('key')}" == slot_key), None
        )
        if group is None:
            return None, None
        on = (config.get("skill_groups") or {}).get(group.get("key"),
                                                    group.get("default_visible", True))
        return {"show", "hide"}, "show" if on else "hide"
    return operator_slot_keys(catalog, slot_key), (config.get("slots") or {}).get(slot_key)


async def _model_choice(
    session: AsyncSession, render: DocRender, slot_key: str, current: str | None
) -> str | None:
    """What the MODEL chose for this part of this render: the pick recorded when Kestrel last
    picked (`config.model_picks`), else the model_choice of the render's first override row for
    the part (written before the config was first changed, so it holds the render's stored
    selection), else the config's current value, which nothing has changed yet. A later operator
    swap is never reported to the learning loop as the model's pick."""
    picks = (render.config or {}).get("model_picks") or {}
    if slot_key in picks:
        return picks[slot_key]
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
    honors; choosing a summary wording (or "show") clears it. Any change makes the stored PDF
    stale, and a part the operator has now chosen no longer needs their pick."""
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
    elif slot_key.startswith("skills:"):
        groups = dict(config.get("skill_groups") or {})
        groups[slot_key.split(":", 1)[1]] = variant_key == "show"
        config["skill_groups"] = groups
    else:
        slots = dict(config.get("slots") or {})
        slots[slot_key] = variant_key
        config["slots"] = slots
    pending = dict(config.get("needs_pick") or {})
    pending.pop(slot_key, None)
    if pending:
        config["needs_pick"] = pending
    else:
        config.pop("needs_pick", None)
    config["stale"] = True
    return config


def _undo_swap_url(render_id: int, override_id: int, slot_key: str, prev: str | None,
                   now: str, extra: dict | None = None) -> str:
    from urllib.parse import urlencode

    q = {"slot_key": slot_key, "prev": "" if prev is None else prev, "now": now, **(extra or {})}
    return f"/renders/{render_id}/swap/undo/{override_id}?{urlencode(q)}"


@router.post("/renders/{render_id}/swap", response_class=HTMLResponse)
async def swap(request: Request, render_id: int):
    """THE GATE ITEM. Write the selection_override BEFORE mutating the config, with the model's
    own selection for this render as model_choice, so the row is a true record of what the model
    picked versus what the operator picked instead, even after an earlier swap. Then update the
    config and answer with the parts that changed, so the page updates in place. The part is a
    bullet slot, "summary", "skills" or "skills:<group>". Any part can be hidden for this render
    ("hide"), optional or not; a hide is never a deletion. An unknown key is refused with nothing
    written (hard rule 2)."""
    form = await request.form()
    return await _do_swap(
        request, render_id,
        str(form.get("slot_key") or "").strip(), str(form.get("variant_key") or "").strip(),
    )


async def _do_swap(request: Request, render_id: int, slot_key: str, variant_key: str):
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        if not slot_key or not variant_key:
            return PlainTextResponse("slot_key and variant_key are required", status_code=400)
        # Validate against the LIVE catalog before anything is written: a key that is not one of
        # this part's wordings (or "hide") is refused outright. Writing it would make resolve()
        # silently substitute the default while the config claimed otherwise (hard rule 2).
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return PlainTextResponse(
                "the document master for this render no longer exists", status_code=404
            )
        config = dict(render.config or {})
        allowed, current = _choice_for(catalog, config, slot_key)
        if allowed is None:
            return PlainTextResponse(f"unknown slot key: {repr(slot_key)}", status_code=400)
        if variant_key not in allowed:
            return PlainTextResponse(
                f"variant key {repr(variant_key)} is not one of slot "
                f"{repr(slot_key)}'s variants "
                f"({html.escape(', '.join(sorted(allowed)))}); nothing was changed",
                status_code=400,
            )
        was_needed = slot_key in _needs(config, resolve(catalog, config))
        # What the swap clears besides the value, so Undo can put it back exactly.
        extra: dict = {}
        if slot_key in (config.get("needs_pick") or {}):
            extra["npk"] = "1"
            extra["np"] = (config.get("needs_pick") or {}).get(slot_key) or ""
        if slot_key == "summary":
            extra["ps"] = config.get("summary") or ""
        model_choice = await _model_choice(s, render, slot_key, current)
        row = SelectionOverride(
            doc_render_id=render.id,
            opportunity_id=render.opportunity_id,
            slot_key=slot_key,
            model_choice=model_choice,
            operator_choice=variant_key,
            **await choice_texts(s, render.doc_master_id, slot_key, model_choice, variant_key),
        )
        s.add(row)
        await s.flush()
        render.config = _apply_choice(config, slot_key, variant_key)
        await s.commit()
        await s.refresh(render)
        ctx = await _page_response(request, s, render)
        override_id = row.id
    ctx["flash"] = slot_key
    note = (
        "Thanks. That line now uses the wording you chose."
        if was_needed else "Swapped. Saved as your choice so Kestrel learns from it."
    )
    if slot_key.startswith("skills:"):
        note = "Updated. Saved as your choice so Kestrel learns from it."
    return _fragments(
        request, ctx,
        events={"tl-toast": _toast(
            note, _undo_swap_url(render_id, override_id, slot_key, current, variant_key, extra)
        )},
    )


@router.post("/renders/{render_id}/swap/undo/{override_id}", response_class=HTMLResponse)
async def undo_swap(request: Request, render_id: int, override_id: int):
    """Undo of one swap. The token carries exactly what the swap changed: the override row it
    wrote, the part, the value before and the value it set. It reverts only that: if the part has
    been changed again since (a later override row, or a value other than the one the swap set),
    nothing is touched and the page says so."""
    q = request.query_params
    slot_key, prev, now = q.get("slot_key") or "", q.get("prev"), q.get("now") or ""

    def gone() -> HTMLResponse:
        return HTMLResponse('<span data-undo-msg data-ok="0">This can no longer be undone.</span>')

    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        row = await s.get(SelectionOverride, override_id)
        if (
            render is None or row is None or row.doc_render_id != render_id
            or row.slot_key != slot_key or row.operator_choice != now
        ):
            return gone()
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return gone()
        config = dict(render.config or {})
        allowed, current = _choice_for(catalog, config, slot_key)
        later = (
            await s.execute(
                select(SelectionOverride.id).where(
                    SelectionOverride.doc_render_id == render_id,
                    SelectionOverride.slot_key == slot_key,
                    SelectionOverride.id > override_id,
                ).limit(1)
            )
        ).first()
        if allowed is None or current != now or later is not None:
            return gone()
        back_to = prev if prev else None
        if back_to is None:
            # The part had no stored value before the swap: it goes back to the standard wording.
            if slot_key.startswith("skills:") or slot_key in ("summary", "skills"):
                return gone()
            slots = dict(config.get("slots") or {})
            slots.pop(slot_key, None)
            config["slots"] = slots
            config["stale"] = True
            render.config = config
        elif back_to in allowed:
            render.config = _apply_choice(config, slot_key, back_to)
        else:
            return gone()
        config = dict(render.config or {})
        if q.get("npk") == "1":  # the swap cleared a failed pick: it is a failed pick again
            pending = dict(config.get("needs_pick") or {})
            pending[slot_key] = q.get("np") or None
            config["needs_pick"] = pending
        if slot_key == "summary" and "ps" in q:
            if q.get("ps"):
                config["summary"] = q["ps"]
            else:
                config.pop("summary", None)
        render.config = config
        await s.delete(row)  # the swap is undone, so its audit row goes with it
        await s.commit()
    return HTMLResponse('<span data-undo-msg data-ok="1">Undone.</span>')


@router.post("/renders/{render_id}/skills", response_class=HTMLResponse)
async def skills(request: Request, render_id: int):
    """Show or hide one skills group for this render: the same override path as a swap, with the
    part named `skills:<group>`."""
    form = await request.form()
    group = str(form.get("group") or "").strip()
    on = str(form.get("on") or "") == "1"
    return await _do_swap(request, render_id, f"skills:{group}", "show" if on else "hide")


_WORDING_MAX = 2000


def _next_key(taken: set[str]) -> str:
    n = 1
    while f"mine{n}" in taken:
        n += 1
    return f"mine{n}"


@router.post("/renders/{render_id}/wording", response_class=HTMLResponse)
async def edit_wording(request: Request, render_id: int):
    """Save the operator's own typed text for one part as a NEW wording of theirs, chosen for this
    render through the same override path as a swap; the original stays available. The Tailor
    page no longer offers free editing (edits happen in Profile, Documents), but the route keeps
    answering for any older caller. The operator wrote it, so it is the source of truth: the
    verbatim check that guards model output and imports does not apply here, the same as the
    Documents page's own "edit wording" route. The model is not involved."""
    form = await request.form()
    slot_key = str(form.get("slot_key") or "").strip()
    text = str(form.get("text") or "").strip()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return PlainTextResponse(
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
                **await choice_texts(s, render.doc_master_id, "summary", model_choice, key),
            ))
            render.config = _apply_choice(config, "summary", key)
            notice = "Saved as a new summary wording. The original is still available."
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
                return PlainTextResponse(
                    f"unknown slot key: {repr(slot_key)}", status_code=400
                )
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
            model_pick = await _model_choice(s, render, slot_key, chosen)
            s.add(SelectionOverride(
                doc_render_id=render.id, opportunity_id=render.opportunity_id,
                slot_key=slot_key, model_choice=model_pick, operator_choice=key,
                **await choice_texts(s, render.doc_master_id, slot_key, model_pick, key),
            ))
            render.config = _apply_choice(config, slot_key, key)
            notice = "Saved as a new wording of your own. The original is still available."
        await s.commit()
        await s.refresh(render)
        ctx = await _page_response(request, s, render)
    ctx["wording_notice"] = notice
    return templates().TemplateResponse(request, "partials/render_review.html", ctx)


# ------------------------------------------------------------------------------ look and privacy
@router.post("/renders/{render_id}/look", response_class=HTMLResponse)
async def change_look(request: Request, render_id: int):
    """Change this application's layout, font size or margins (and, from an older caller, font,
    header alignment and accent). Presentation only: no variant changes, so no selection_override
    row. A layout the catalog does not have is refused. The preview updates in place; the PDF is
    made again from the new look when it is downloaded."""
    form = await request.form()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return PlainTextResponse(
                "the document master for this render no longer exists", status_code=404
            )
        config = dict(render.config or {})
        layout = str(form.get("layout") or "")
        if layout:
            if not any(lay.key == layout for lay in catalog.layouts):
                return HTMLResponse(f"Unknown layout {html.escape(layout)}; nothing was changed.")
            config["layout"] = layout
        if any(k in form for k in ("font", "header_align", "accent")):
            for k in ("font", "header_align", "accent"):
                config.pop(k, None)
        if "size" in form:
            config.pop("size", None)
        if "margins" in form:
            config.pop("margins", None)
        config.update(_look_params(form))
        config["stale"] = True
        render.config = config
        await s.commit()
        await s.refresh(render)
        ctx = await _page_response(request, s, render)
    ctx["look_changed"] = True
    return _fragments(request, ctx)


@router.post("/renders/{render_id}/look/default", response_class=HTMLResponse)
async def make_look_default(request: Request, render_id: int):
    """Make this application's layout, size and margins the document's own Look (Profile,
    Documents). The toast's Undo carries what it replaced and what it wrote, and reverts only
    that."""
    from urllib.parse import urlencode

    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        master = await s.get(DocMaster, render.doc_master_id)
        if master is None:
            return PlainTextResponse("not found", status_code=404)
        config = dict(render.config or {})
        look = dict(master.look or {})
        before = {k: look.get(k) or "" for k in ("layout", "size", "margins")}
        for k in ("layout", "size", "margins"):
            if config.get(k):
                look[k] = config[k]
        master.look = look
        after = {k: look.get(k) or "" for k in ("layout", "size", "margins")}
        await s.commit()
        ctx = await _page_response(request, s, render)
    q = {**{f"was_{k}": v for k, v in before.items()}, **{f"now_{k}": v for k, v in after.items()}}
    return _fragments(
        request, ctx,
        events={"tl-toast": _toast(
            "Saved as your Profile look.",
            f"/renders/{render_id}/look/default/undo?{urlencode(q)}",
        )},
    )


@router.post("/renders/{render_id}/look/default/undo", response_class=HTMLResponse)
async def undo_look_default(request: Request, render_id: int):
    q = request.query_params
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        master = await s.get(DocMaster, render.doc_master_id) if render else None
        if master is None:
            return HTMLResponse(
                '<span data-undo-msg data-ok="0">This can no longer be undone.</span>')
        look = dict(master.look or {})
        keys = ("layout", "size", "margins")
        if any((look.get(k) or "") != (q.get(f"now_{k}") or "") for k in keys):
            return HTMLResponse(
                '<span data-undo-msg data-ok="0">This can no longer be undone.</span>')
        for k in keys:
            was = q.get(f"was_{k}") or ""
            if was:
                look[k] = was
            else:
                look.pop(k, None)
        master.look = look
        await s.commit()
    return HTMLResponse('<span data-undo-msg data-ok="1">Undone.</span>')


@router.post("/renders/{render_id}/privacy", response_class=HTMLResponse)
async def change_privacy(request: Request, render_id: int):
    """Hide (or show) the current employer in this render's preview and PDF. Confidential mode
    keeps it hidden regardless; this switch is the per-application choice."""
    form = await request.form()
    on = str(form.get("on") or "") == "1"
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        config = dict(render.config or {})
        config["redact_orgs"] = on
        config["stale"] = True
        render.config = config
        await s.commit()
        await s.refresh(render)
        ctx = await _page_response(request, s, render)
    return _fragments(request, ctx)


# ------------------------------------------------------------------------------ picking
async def _run_selection(session: AsyncSession, settings: Settings, catalog: MasterCatalog,
                         opportunity_id: int | None):
    """The model call behind Pick again (P3, kestrel.docs.select). A seam: tests replace it."""
    from kestrel.docs.select import select_config
    from kestrel.llm.client import LLMClient

    async with httpx.AsyncClient() as http:
        client = LLMClient(settings, http=http)
        return await select_config(session, client, catalog, opportunity_id=opportunity_id)


@router.get("/renders/{render_id}/pick", response_class=HTMLResponse)
async def pick_dialog(request: Request, render_id: int):
    """The ask-first dialog for an online pick: the provider, the model, what is sent (from the
    Hosted mode disclosure), and what happens to the current employer. Nothing leaves this
    computer until the operator confirms."""
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        ctx = await _page_response(request, s, render)
    return templates().TemplateResponse(request, "partials/tailor_pick_dialog.html", ctx)


def _pick_error(request: Request, ctx: dict, message: str) -> Any:
    ctx["pick_error"] = message
    return templates().TemplateResponse(request, "partials/render_review.html", ctx)


@router.post("/renders/{render_id}/pick", response_class=HTMLResponse)
async def pick_again(request: Request, render_id: int):
    """Ask Kestrel to choose the wordings again. The operator's own swaps stay as they set them.
    On a local model it runs at once; on an online model it needs the operator's confirmation of
    THIS send, for the configured model, checked here and not trusted from the page. A failed
    pick changes nothing and says why. Every pick Kestrel keeps is a real wording of the
    operator's (the catalog check ran); a failed slot is marked "Needs your pick", never filled."""
    form = await request.form()
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        settings = await load_settings_with_db(s)
        route = await _pick_route(s, settings, render.doc_master_id)
        ctx = await _page_response(request, s, render)
        if route["online"]:
            confirmed = str(form.get("confirmed") or "") == "1"
            from kestrel.services.online_consent import confirmation_matches

            if not confirmed or not confirmation_matches(route, form):
                return _pick_error(
                    request, ctx,
                    "Nothing was sent. Confirm the online pick first: the box says what will be "
                    "sent.",
                )
            if route["employer"] == "held":
                return _pick_error(
                    request, ctx,
                    "Nothing was sent. Confidential mode is on and no current employer is "
                    "chosen, so Kestrel will not send this. Choose your employer in Profile, "
                    "Documents.",
                )
        if render.opportunity_id is None:
            return _pick_error(
                request, ctx, "This version is not for a role, so there is nothing to pick for."
            )
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is None:
            return _pick_error(request, ctx, "The document this came from no longer exists.")
        started = time.monotonic()
        try:
            sel = await _run_selection(s, settings, catalog, render.opportunity_id)
        except Exception as exc:  # a model that is not running is a message, never a 500
            log.warning("tailor_pick_failed", render_id=render_id, error_type=type(exc).__name__)
            await s.rollback()
            await s.refresh(render)
            ctx = await _page_response(request, s, render)
            return _pick_error(
                request, ctx,
                f"Kestrel could not pick this time: {plain_error(exc)} Your picks are unchanged.",
            )
        seconds = max(1, round(time.monotonic() - started))
        if sel.defaults_used:
            return _pick_error(
                request, ctx,
                "Kestrel could not make a valid pick this time, so nothing was changed.",
            )
        overrides = await _overrides(s, render.id)
        config = dict(render.config or {})
        new_slots = dict(sel.config.get("slots") or {})
        model_picks = {k: v for k, v in new_slots.items()}
        model_picks["summary"] = sel.config.get("summary")
        for bad_slot, bad_key in (sel.needs_pick or {}).items():
            model_picks[bad_slot] = bad_key  # what the model named, never the default we show
        skill_groups = dict(sel.config.get("skill_groups") or {})
        needs = {k: v for k, v in (sel.needs_pick or {}).items()}
        # The operator's own swaps stay as they set them.
        for slot_key, ov in overrides.items():
            choice = ov.operator_choice
            if slot_key == "summary":
                if choice == "hide":
                    config["hide_summary"] = True
                else:
                    sel.config["summary"] = choice
            elif slot_key == "skills" or slot_key.startswith("skills:"):
                if slot_key.startswith("skills:"):
                    skill_groups[slot_key.split(":", 1)[1]] = choice == "show"
            elif choice:
                new_slots[slot_key] = choice
            needs.pop(slot_key, None)
        config["slots"] = new_slots
        config["summary"] = sel.config.get("summary")
        config["skill_groups"] = skill_groups
        config["model_picks"] = model_picks
        config["pick_meta"] = {
            "model": route.get("model") or settings.llm.tiers[PICK_TIER].model,
            "online": bool(route["online"]), "seconds": seconds, "at": utcnow_iso(),
        }
        if needs:
            config["needs_pick"] = needs
        else:
            config.pop("needs_pick", None)
        config["stale"] = True
        render.config = config
        render.rationale = dict(sel.rationale or {})
        render.defaults_used = False
        await s.commit()
        await s.refresh(render)
        ctx = await _page_response(request, s, render)
    return templates().TemplateResponse(
        request, "partials/render_review.html", ctx,
        headers=_events({"tl-toast": _toast(
            "Picked again. Your own swaps stayed as you set them."
        )}),
    )


# ------------------------------------------------------------------------------ attach, download
def _unresolved_message(n: int) -> str:
    return (
        f"{n} line still needs your pick." if n == 1 else f"{n} lines still need your pick."
    )


@router.post("/renders/{render_id}/attach", response_class=HTMLResponse)
async def attach_to_pursuit(request: Request, render_id: int):
    """Attach this render as the pursuit's document, or detach it on a second press (sets or
    clears pursuit.document_id). A clear message when the render has no opportunity or the
    opportunity has no pursuit yet. While any line still needs the operator's pick, attaching
    needs `anyway=1`, and the choice is recorded on the render. The toast's Undo restores exactly
    what this press changed."""
    form = await request.form()
    anyway = str(form.get("anyway") or "") == "1"
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("not found", status_code=404)
        if render.opportunity_id is None:
            return HTMLResponse('<span class="muted">This version is not tied to a role.</span>')
        pursuit = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == render.opportunity_id))
        ).scalar_one_or_none()
        opp = await s.get(Opportunity, render.opportunity_id)
        word = _words(opp)["app"]
        if pursuit is None:
            return HTMLResponse(
                f'<span class="muted">Add this role to your list first; there is no {word} '
                "to attach it to yet.</span>"
            )
        config_before = dict(render.config or {})
        previous = pursuit.document_id
        detach = previous == render.id
        if not detach:
            catalog = await load_catalog(s, render.doc_master_id)
            if catalog is not None:
                doc = resolve(catalog, dict(render.config or {}))
                pending = _needs(dict(render.config or {}), doc)
                if pending and not anyway:
                    return PlainTextResponse(
                        _unresolved_message(len(pending))
                        + " Choose now, or attach anyway from the page.",
                        status_code=409,
                    )
                if pending:
                    config = dict(render.config or {})
                    config["left_unresolved"] = sorted(pending)
                    render.config = config
        pursuit.document_id = None if detach else render.id
        changed = config_before.get("left_unresolved") != (render.config or {}).get(
            "left_unresolved"
        )
        token = await pursuit_undo.save(
            s,
            {
                "kind": "attach",
                "render_id": render_id,
                "pursuit_id": pursuit.id,
                "last_activity_at": pursuit.last_activity_at,
                "before": previous,
                "after": pursuit.document_id,
                "config_changed": changed,
                "config_before": config_before.get("left_unresolved"),
                "config_after": (render.config or {}).get("left_unresolved"),
            },
        )
        await s.commit()
        ctx = await _page_response(request, s, render)
    org = ctx.get("org_name") or "this"
    undo = f"/renders/{render_id}/attach/undo?token={token}"
    message = (
        f"Removed from your {org} {word}."
        if detach
        else f"Attached to your {org} {word}. Kestrel never sends it."
    )
    ctx["attach_note"] = f"Removed from this {word}." if detach else f"Attached to this {word}."
    return _fragments(request, ctx, events={"tl-toast": _toast(message, undo)})


@router.post("/renders/{render_id}/attach/undo", response_class=HTMLResponse)
async def undo_attach(request: Request, render_id: int):
    """Undo of an attach or detach: restore the document the pursuit had, but only if it still
    holds what that press set."""
    from datetime import UTC, datetime

    refuse = '<span data-undo-msg data-ok="0">This can no longer be undone.</span>'
    async with sessionmaker()() as s:
        row = await s.get(Setting, pursuit_undo.PREFIX + request.query_params.get("token", ""))
        if row is None or row.updated_at < (datetime.now(UTC) - pursuit_undo.KEEP).isoformat():
            return HTMLResponse(refuse)
        data = row.value
        if data.get("kind") != "attach" or data["render_id"] != render_id:
            return HTMLResponse(refuse)
        render = await s.get(DocRender, render_id)
        pursuit = await s.get(Pursuit, data["pursuit_id"])
        if (
            render is None
            or pursuit is None
            or pursuit.last_activity_at != data["last_activity_at"]
            or pursuit.document_id != data["after"]
            or (
                data["config_changed"]
                and (render.config or {}).get("left_unresolved") != data["config_after"]
            )
        ):
            return HTMLResponse(refuse)
        pursuit.document_id = data["before"]
        if data["config_changed"]:
            config = dict(render.config or {})
            if data["config_before"] is None:
                config.pop("left_unresolved", None)
            else:
                config["left_unresolved"] = data["config_before"]
            render.config = config
        await s.delete(row)
        await s.commit()
    return HTMLResponse('<span data-undo-msg data-ok="1">Undone.</span>')


@router.get("/renders/{render_id}/download")
async def download(request: Request, render_id: int):
    """The PDF for what the page shows now. The file is made again from the current picks when
    they changed since it was written (or when none was written), so the download is never an
    older version than the preview. While any line still needs the operator's pick the server
    asks first (`anyway=1`) and records the choice on the render, so a document with an unresolved
    pick never leaves silently; the wording used is still the operator's own text (hard rule 2)."""
    anyway = request.query_params.get("anyway") == "1"
    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None:
            return PlainTextResponse("No PDF on record for this render.", status_code=404)
        catalog = await load_catalog(s, render.doc_master_id)
        if catalog is not None:
            config = dict(render.config or {})
            pending = _needs(config, resolve(catalog, config))
            if pending and not anyway:
                return PlainTextResponse(
                    _unresolved_message(len(pending)) + " Go back and choose, or download "
                    "anyway from the page.", status_code=409)
            if pending:
                config["left_unresolved"] = sorted(pending)
                render.config = config
                await s.commit()
        config = dict(render.config or {})
        path = Path(render.pdf_path) if render.pdf_path else None
        if config.get("stale") or path is None or not path.is_file():
            try:
                from kestrel.docs.service import refresh_pdf

                settings = await load_settings_with_db(s)
                fresh = await refresh_pdf(s, settings, render)
            except Exception as exc:  # no browser on this machine: say so, never a 500
                log.warning("tailor_pdf_failed", render_id=render_id,
                            error_type=type(exc).__name__)
                return PlainTextResponse(
                    f"Could not make the PDF: {fixed_reason(exc)}", status_code=503)
            if fresh is None:
                return PlainTextResponse("The document this came from no longer exists.",
                                    status_code=404)
            render = fresh
        path = Path(render.pdf_path) if render.pdf_path else None
        if path is None or not path.is_file():
            return PlainTextResponse("The PDF file is missing from disk.", status_code=404)
        name = render.filename or path.name
    return FileResponse(path, media_type="application/pdf", filename=name)
