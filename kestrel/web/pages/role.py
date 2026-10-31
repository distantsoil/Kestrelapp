"""The Role page (D-UI-150, SPEC-INTERFACES 2.1 `/opportunities/{id}`): the full page for one role,
its five tabs, and the actions on it.

  GET  /opportunities/{id}[/{tab}]          the page (a partial for an HTMX request: Previous and
                                             Next slide the body, a tab slides the pane)
  GET  /opportunities/{id}/tab/{tab}        one tab as a partial (kept; the tabs use it)
  GET  /opportunities/{id}/role/side        the decision column, refreshed in place
  POST /opportunities/{id}/role/shortlist   Shortlist (Add to leads); Undo removes only that pursuit
  POST /opportunities/{id}/role/unshortlist the second click: off the shortlist, with Undo
  POST /opportunities/{id}/role/not-for-me  one of the four reasons; hides the role, Undo restores
  POST /opportunities/{id}/role/archive     Archive (hide); Undo / Put it back unhide
  POST /opportunities/{id}/role/view        Your view: Yes, or Adjust (a manual label); Undo
  POST /opportunities/{id}/role/story       Add a story; optionally queue a single-role re-score
  POST /opportunities/{id}/role/research    Research the company or Find contacts (asks first when
                                             the model is online)
  GET  /opportunities/{id}/role/deeper      the ask-first dialog for Deeper read
  POST /opportunities/{id}/role/deeper/send the confirmed send (the existing Tier 3 token flow)
  GET  /opportunities/{id}/role/preview/{n} a document as a scaled true page

Nothing here applies, sends or submits anything for the operator (hard rule 1): documents are
drafts the operator downloads, and the only online sends are the ones a dialog named first and a
confirmation released, each checked again on the server (`gate`)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Opportunity,
    PrepPack,
    Pursuit,
    Story,
    Task,
    VerdictOverride,
)
from kestrel.llm import credit
from kestrel.llm import providers as prov
from kestrel.research import jobs as research_jobs
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import calibrate_view as cv
from kestrel.web import pipeline_view as pv
from kestrel.web import role_view as rv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.fit import fit_word_for_verdict
from kestrel.web.pages.pipeline import _events, _toast
from kestrel.web.queries import (
    opportunity_history,
    opportunity_renders,
    org_context,
)
from kestrel.web.redact import redact_json, redact_text
from kestrel.web.words import words

log = structlog.get_logger()
router = APIRouter()

TABS = rv._ORDER
# The Prepare buttons Kestrel has no drafting back end for yet (D-UI-150): shown, never active.
_NO_DRAFTER = "Kestrel does not draft this yet. Nothing is sent or made without you."


# ------------------------------------------------------------------------------------- helpers
def _answer(events: dict[str, Any] | None, body: str = "", status: int = 200) -> HTMLResponse:
    """An action's answer: the OOB parts in `body`, no main swap, and the events."""
    headers = {"HX-Reswap": "none", **_events(events)}
    return HTMLResponse(body, headers=headers, status_code=status)


def _refresh(*urls: tuple[str, str, str]) -> list[dict[str, str]]:
    """What an Undo reloads: [(target, url, swap)] as the toast detail role.js reads."""
    return [{"target": t, "url": u, "swap": s} for t, u, s in urls]


def _side_refresh(opp_id: int) -> list[dict[str, str]]:
    return _refresh(("#rl-side", f"/opportunities/{opp_id}/role/side", "outerHTML"))


def _render(request: Request, name: str, ctx: dict[str, Any]) -> str:
    return templates().get_template(name).render(**{"request": request, **ctx})


async def _oob_counts(s) -> str:
    from kestrel.web.routes_api import oob_nav_counts

    return await oob_nav_counts(s)


# ------------------------------------------------------------------------- online consent (gate)
async def online_route(s, settings, tier: str) -> dict[str, Any] | None:
    """Shared consent facts, with the Role dialog's free-model disclosure."""
    from kestrel.services.online_consent import online_route as shared_route

    route = await shared_route(s, settings, tier)
    if not route["online"]:
        return None
    route["free"] = prov.is_free_variant(str(route["model"]))
    return route


def gate(route: dict[str, Any] | None, form) -> str | None:
    """Use the shared refusal rules; an unconfirmed Role action opens its dialog."""
    from kestrel.services.online_consent import NOT_CONFIRMED, consent_problem

    if route is None:
        return None
    problem = consent_problem({**route, "online": True}, form)
    return "ask" if problem == NOT_CONFIRMED else problem


def _ask(
    request: Request,
    route: dict[str, Any],
    *,
    action: str,
    kind: str,
    url: str,
    role: str,
    extra: dict[str, str] | None = None,
) -> str:
    """The ask-first dialog, as an out-of-band fill of #rl-dialog."""
    body = _render(
        request,
        "partials/role_online.html",
        {
            "route": route,
            "action": action,
            "kind": kind,
            "url": url,
            "role": role,
            "extra": extra or {},
        },
    )
    return f'<div id="rl-dialog" hx-swap-oob="innerHTML">{body}</div>'


# ---------------------------------------------------------------------------- page context
def _terms(profile, track: str) -> tuple[float | None, float | None]:
    terms = getattr(
        profile, "consulting_terms" if track == "consulting" else "employment_terms", None
    )
    keys = (
        ("day_rate_floor", "day_rate_target")
        if track == "consulting"
        else ("comp_floor_usd", "comp_target_usd")
    )

    def num(k: str) -> float | None:
        v = getattr(terms, k, None)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v else None

    return num(keys[0]), num(keys[1])


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


async def _evidence_sources(
    s, reqs: list[dict[str, Any]], track: str, conf: bool, mapping
) -> dict[int, dict[str, str]]:
    """{requirement k: {label, href}} for each met or partly met row whose evidence quotes one of
    the operator's stories or resume bullets, found by comparing the model's evidence text with
    the stored text. A row with no match gets no link (the page says "Source not recorded"), never
    a nearest guess (hard rule 2)."""
    wanted = [r for r in reqs if r["state"] != rv.GAP and r.get("evidence")]
    if not wanted:
        return {}
    stories = list((await s.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars())
    variants = (
        await s.execute(
            select(DocVariant.text, DocMaster.id, DocMaster.name, DocSection.title)
            .join(DocSlot, DocSlot.id == DocVariant.doc_slot_id)
            .join(DocSection, DocSection.id == DocSlot.doc_section_id)
            .join(DocMaster, DocMaster.id == DocSection.doc_master_id)
            .where(DocMaster.track == track, DocMaster.is_active.is_(True))
        )
    ).all()
    out: dict[int, dict[str, str]] = {}
    for r in wanted:
        ev = _norm(r["evidence"])
        hit = None
        for st in stories:
            for field in (st.title, st.result, st.action):
                n = _norm(field)
                if len(n) >= 18 and (n in ev or ev in n):
                    hit = {"label": f"Story · {st.title}", "href": f"/stories#story-{st.id}"}
                    break
            if hit:
                break
        if hit is None:
            for text, master_id, master_name, title in variants:
                n = _norm(text)
                if len(n) >= 25 and (n in ev or ev in n):
                    where = f" · {title}" if title else ""
                    hit = {
                        "label": f"Resume · {master_name}{where}",
                        "href": f"/documents?master_id={master_id}",
                    }
                    break
        if hit:
            hit["label"] = redact_text(hit["label"], mapping, enabled=conf) or hit["label"]
            out[r["k"]] = hit
    return out


async def _sequence(s, opp: Opportunity) -> dict[str, Any] | None:
    """Where this role sits in the default Pipeline view of its track: {pos, total, prev, next}
    for Previous and Next ("3 of 11"), or None when it is not in that list (a Not a fit role,
    a hidden one, one reached from elsewhere)."""
    from kestrel.services import saved_views
    from kestrel.web.queries import list_opportunities
    from kestrel.web.routes_pages import page_filters

    views = await saved_views.list_views(s, opp.track)
    default = next((v for v in views if v.is_default), None)
    f = page_filters(saved_views.params_for(default) if default else {}, None)
    f.track = opp.track
    f.kind = "opportunities"
    f.grouped = True
    f.page_size = 200
    f.limits = {k: 200 for k in ("worth", "maybe", "applied", "new", "failed")}
    page = await list_opportunities(s, f)
    ids = [r.opp.id for r in page.rows]
    if opp.id not in ids:
        return None
    i = ids.index(opp.id)
    return {
        "pos": i + 1,
        "total": len(ids),
        "prev": ids[i - 1] if i > 0 else None,
        "next": ids[i + 1] if i + 1 < len(ids) else None,
    }


def _label_state(ev, overrides: list[VerdictOverride]) -> dict[str, Any] | None:
    """The operator's latest word on the fit (a `verdict_override` row, the record Pipeline's
    "you said" pill reads) made against this evaluation or later, or None. `overrides` is
    newest first."""
    if ev is None:
        return None
    for ov in overrides:
        if ov.operator_score is not None and (ov.created_at or "") >= (ev.created_at or ""):
            agreed = (ov.reason or "").startswith("Agreed with Kestrel")
            return {
                "id": ov.id,
                "agreed": agreed,
                "score": ov.operator_score,
                "word": cv.word_for(ov.operator_score),
            }
    return None


async def page_context(request: Request, opp_id: int, tab: str) -> dict[str, Any] | None:
    """Everything the Role page's templates print for one role and one tab. Prose comes from the
    redacted copies `_detail_context` builds (confidential mode), never the raw ORM fields. The
    extra reads use their own session: `_detail_context` edits ORM rows for display only, and a
    later query on the same session would flush those edits."""
    from kestrel.pipeline.context import comp_intel_count, comp_intel_stats
    from kestrel.pipeline.profile import load_profile
    from kestrel.web.routes_pages import _detail_context

    async with sessionmaker()() as s:
        detail = await _detail_context(s, opp_id)
        if detail is None:
            return None
        detail["renders"] = await opportunity_renders(s, opp_id)
    opp, ev, ev_json, org = detail["opp"], detail["ev"], detail["ev_json"], detail["org"]
    conf, mapping = detail["confidential"], detail["mapping"]
    track = opp.track
    profile = await asyncio.to_thread(load_profile, track)
    floor, target = _terms(profile, track)
    async with sessionmaker()() as s2:
        from kestrel.services.online_consent import APPROVAL_HOLD

        detail["scoring_held"] = (await s2.execute(select(Task.id).where(
            Task.type.in_(("t1", "t2")), Task.status == "pending",
            Task.last_error == APPROVAL_HOLD,
            func.json_extract(Task.payload, "$.opportunity_id") == opp_id,
        ).limit(1))).first() is not None
        stats = None if track == "consulting" else await comp_intel_stats(s2, opp)
        n_seen = 0 if track == "consulting" else await comp_intel_count(s2, opp)
        reqs = rv.requirements(opp, ev_json)
        sources = await _evidence_sources(s2, reqs, track, conf, mapping)
        labels = list(
            (
                await s2.execute(
                    select(VerdictOverride)
                    .where(VerdictOverride.opportunity_id == opp_id)
                    .order_by(VerdictOverride.id.desc())
                )
            ).scalars()
        )
        seq = await _sequence(s2, opp)
        octx = await org_context(s2, org.id if org is not None else None)
        research = redact_json(octx["research"], mapping, enabled=conf)
        contacts = redact_json(octx["contacts"], mapping, enabled=conf)
        jobs = await research_jobs.states(s2, opp_id)
        history_items = []
        if tab == "history":
            history_items = redact_json(
                await opportunity_history(s2, opp_id), mapping, enabled=conf
            )
        cover = None
        pursuit = detail.get("pursuit")
        qview = None
        if pursuit is not None:
            from kestrel.web.routes_pages import _questions_view

            qview = await _questions_view(s2, opp_id, detail)
        prep_ready = (
            await s2.execute(select(PrepPack.id).where(PrepPack.opportunity_id == opp_id).limit(1))
        ).first() is not None
        if pursuit is not None and pursuit.cover_letter_id:
            cover = await s2.get(DocRender, pursuit.cover_letter_id)
    fit = detail["fit"]
    reqs_wheel = rv.wheel(reqs)
    w = words(track)
    detail.update(
        {
            "tab": tab,
            "tab_labels": rv.TAB_LABELS,
            "tabs": TABS,
            "w": w,
            "consulting": track == "consulting",
            "facts_row": rv.facts_row(opp, detail["org_name"]),
            "liveness": pv.liveness_fact(opp),
            "dims": rv.dimension_rows(ev_json, track),
            "ring_pct": round((fit.score or 0) / 5 * 100) if fit.score is not None else 0,
            "ring_tone": "good" if fit.word in ("strong fit", "good fit") else "warn",
            "read": rv.read_card(opp, ev_json, fit.word) if ev is not None else None,
            "pay": rv.pay_band(opp, ev_json, stats, n_seen, floor, target),
            "level": rv.level_box(opp, ev_json),
            "lead": pv.lead_with(opp, ev_json),
            "reqs": reqs,
            "wheel": reqs_wheel,
            "favor": [r for r in reqs if r["state"] != rv.GAP],
            "gaps": [r for r in reqs if r["state"] == rv.GAP],
            "evidence": rv.evidence_rows(reqs, sources),
            "summary": rv.summary_card(opp, ev_json, org),
            "seq": seq,
            "labels_state": _label_state(ev, labels),
            "research": research,
            "contacts": contacts,
            "research_jobs": jobs,
            "org_signals": redact_json(
                list(org.signals or []) if org is not None else [], mapping, enabled=conf
            ),
            "cover": cover,
            "n_docs": len(detail["renders"]) + (1 if cover is not None else 0),
            "history": rv.history_timeline(
                history_items,
                opp,
                detail["source_name"],
                len(detail["dedup_rows"]),
                detail["rule_why"].sentence if detail.get("rule_why") else None,
            )
            if tab == "history"
            else [],
            "unlocked": pursuit is not None,
        "qview": qview,
        "prep_ready": prep_ready,
            "cv_words": cv.SCORE_WORDS,
            "next_id": None,
            "no_drafter": _NO_DRAFTER,
            "nf_reasons": pv.reasons_for(track),
            "request": request,
        }
    )
    detail["first_status"] = _first_status(opp.track)
    detail["fresh"] = _is_fresh(opp, pursuit)
    detail["doc_rows"] = _doc_rows(detail, cover)
    detail["mini_rows"] = [d for d in detail["doc_rows"] if d["render_id"]]
    detail["has_docs"] = bool(detail["mini_rows"])
    return detail


def _doc_rows(detail: dict[str, Any], cover) -> list[dict[str, Any]]:
    """The two document rows (resume and cover letter; capability statement and engagement
    outline): the latest render of each that exists, else what is missing and why."""
    opp, renders = detail["opp"], detail["renders"]
    consulting = opp.track == "consulting"
    want = "capability_statement" if consulting else "resume"
    versions: dict[int, int] = {}
    seen: dict[int, int] = {}
    for row in reversed(renders):
        mid = row["render"].doc_master_id
        seen[mid] = seen.get(mid, 0) + 1
        versions[row["render"].id] = seen[mid]
    first = next(
        (r for r in renders if r["master"] is not None and r["master"].doc_kind == want),
        renders[0] if renders else None,
    )
    rows: list[dict[str, Any]] = []
    title = "Capability statement" if consulting else "Tailored resume"
    if first is not None:
        r, m = first["render"], first["master"]
        from kestrel.date_display import format_display

        when = format_display(r.created_at)
        rows.append(
            {
                "title": title,
                "icon": "profile",
                "render_id": r.id,
                "has_pdf": bool(r.pdf_path),
                "short": f"Version {versions[r.id]} · {when}",
                "sub": f"Version {versions[r.id]} · built from {m.name if m else 'your master'} · "
                f"{when}" + (f" · {r.filename}" if r.filename else ""),
                "href": None,
                "action": "",
            }
        )
    else:
        rows.append(
            {
                "title": title,
                "icon": "profile",
                "render_id": None,
                "has_pdf": False,
                "short": "Not tailored yet",
                "sub": "Not tailored yet. Every line will be your own "
                "text, chosen from your variants.",
                "href": detail.get("tailor_href"),
                "action": detail["tailor_label"],
            }
        )
    second = "Engagement outline" if consulting else "Cover letter"
    if cover is not None:
        rows.append(
            {
                "title": second,
                "icon": "prep",
                "render_id": cover.id,
                "has_pdf": bool(cover.pdf_path),
                "short": "Draft",
                "sub": f"Draft #{cover.id} · attached to your "
                f"{'lead' if consulting else 'application'}. Kestrel never sends it.",
                "href": None,
                "action": "",
            }
        )
    else:
        rows.append(
            {
                "title": second,
                "icon": "prep",
                "render_id": None,
                "has_pdf": False,
                "short": "No draft yet",
                "sub": (
                    "No draft yet. An outline for a conversation, not a priced proposal. "
                    if consulting
                    else "No draft yet. "
                )
                + _NO_DRAFTER,
                "href": None,
                "action": "New engagement outline" if consulting else "New cover letter",
            }
        )
    return rows


def _first_status(track: str) -> str:
    from kestrel.db.enums import PURSUIT_STATUS

    return PURSUIT_STATUS[track][0]


def _is_fresh(opp, pursuit) -> bool:
    """A shortlist the click just made: still the first column, never applied, not closed. Only
    such a pursuit comes off with the second click; later ones carry real history."""
    return bool(
        pursuit is not None
        and not pursuit.closed_at
        and not pursuit.applied_at
        and pursuit.status == _first_status(opp.track)
    )


async def render_page(request: Request, opp_id: int, tab: str) -> Response:
    """The page, or (HX-Request) the part of it that changed: a tab's pane, or the whole body
    for Previous and Next. A history restore (Back) always gets the whole page."""
    ctx = await page_context(request, opp_id, tab)
    if ctx is None:
        return HTMLResponse("Not found", status_code=404)
    htmx = request.headers.get("HX-Request") == "true"
    restore = request.headers.get("HX-History-Restore-Request") == "true"
    if htmx and not restore:
        return templates().TemplateResponse(request, "partials/role_body.html", ctx)
    return templates().TemplateResponse(request, "opportunity_detail.html", ctx)


async def render_tab(request: Request, opp_id: int, name: str) -> Response:
    if name not in TABS:
        return HTMLResponse("No such tab", status_code=404)
    ctx = await page_context(request, opp_id, name)
    if ctx is None:
        return HTMLResponse("Not found", status_code=404)
    return templates().TemplateResponse(request, f"partials/role_tab_{name}.html", ctx)


def _tab_route(name: str):
    async def handler(request: Request, opp_id: int) -> Response:
        return await render_page(request, opp_id, name)

    return handler


for _t in TABS[1:]:
    router.add_api_route(
        f"/opportunities/{{opp_id}}/{_t}",
        _tab_route(_t),
        methods=["GET"],
        response_class=HTMLResponse,
        name=f"role_{_t}",
    )


# ----------------------------------------------------------------------------- side and cards
async def _side_html(request: Request, opp_id: int, **extra: Any) -> str:
    ctx = await page_context(request, opp_id, "evaluation")
    if ctx is None:
        return ""
    ctx.update(extra)
    return _render(request, "partials/role_side.html", ctx)


@router.get("/opportunities/{opp_id}/role/side", response_class=HTMLResponse)
async def side(request: Request, opp_id: int):
    html = await _side_html(request, opp_id)
    return HTMLResponse(html or "", status_code=200 if html else 404)


async def _cards_oob(
    request: Request,
    opp_id: int,
    *,
    view: bool = False,
    company: bool = False,
    orgtab: bool = False,
    doctab: bool = False,
) -> str:
    """Out-of-band replacements for the cards an action changed (only those on the page)."""
    ctx = await page_context(request, opp_id, "evaluation")
    if ctx is None:
        return ""
    out = []

    def oob(name: str) -> str:
        return _render(request, name, ctx).replace("<section", '<section hx-swap-oob="true"', 1)

    if view:
        out.append(oob("partials/role_view_card.html"))
    if company:
        out.append(oob("partials/role_company_card.html"))
    if orgtab:
        ctx["tab"] = "organization"
        out.append(
            _render(request, "partials/role_tab_organization.html", ctx).replace(
                "<div", '<div hx-swap-oob="true"', 1
            )
        )
    if doctab:
        ctx["tab"] = "documents"
        out.append(
            _render(request, "partials/role_tab_documents.html", ctx).replace(
                "<div", '<div hx-swap-oob="true"', 1
            )
        )
    return "".join(out)


# ------------------------------------------------------------------------------------ Shortlist
@router.post("/opportunities/{opp_id}/role/shortlist", response_class=HTMLResponse)
async def shortlist(request: Request, opp_id: int):
    """Shortlist (Add to leads in Consulting). The Undo carries the pursuit this click made and
    removes only that, only while it is still the fresh one (a later change is never undone)."""
    from kestrel.web.pages.opportunity_actions import add_to_list

    toast: dict[str, Any]
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
            existed = (
                await s.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp_id))
            ).first() is not None
            pursuit, text = await add_to_list(s, opp)
            if pursuit is None:
                toast = _toast(text, tone="warn")
            elif existed:
                toast = _toast(text)
            else:
                toast = _toast(
                    "Shortlisted. Tailor resume, cover letter and prep pack are switched on."
                    if opp.track != "consulting"
                    else "Added to your leads. Preparation is switched on.",
                    f"/opportunities/{opp_id}/role/shortlist/undo?pursuit={pursuit.id}"
                    f"&token={s.info['added_undo'][opp_id]}",
                )
                toast["refresh"] = _side_refresh(opp_id)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning("role_shortlist_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            toast = _toast(f"Could not add it: {plain_error(exc)}", tone="warn")
        oob = await _oob_counts(s)
    html = await _side_html(request, opp_id, unlocked_now=True)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


@router.post("/opportunities/{opp_id}/role/shortlist/undo", response_class=HTMLResponse)
async def shortlist_undo(request: Request, opp_id: int, pursuit: int = 0):
    from kestrel.services.pursuit_undo import undo_added

    async with sessionmaker()() as s:
        done = await undo_added(s, request.query_params.get("token", ""), opp_id)
        await s.commit()
        oob = await _oob_counts(s)
    # A refused Undo says so: the toast reads the marker (kestrel-ui.js).
    return _answer(
        {"pipelineRefresh": True},
        oob
        if done
        else '<span data-undo-msg data-ok="0">This can no longer be undone.</span>' + oob,
    )


@router.post("/opportunities/{opp_id}/role/unshortlist", response_class=HTMLResponse)
async def unshortlist(request: Request, opp_id: int):
    """The second click on Shortlist: off the shortlist, with Undo. Drafts stay in Documents.
    Only a fresh shortlist comes off this way; one that moved on to Applications carries history
    and is removed from More with the warning it needs."""
    from kestrel.db.models import EngagementOffer, Offer
    from kestrel.services import pursuit_undo
    from kestrel.web.pages.opportunity_actions import remove_from_list

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        p = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        ).scalar_one_or_none()
        has_offer = False
        if p is not None:
            for model in (Offer, EngagementOffer):
                has_offer = has_offer or (await s.execute(
                    select(model.id).where(model.pursuit_id == p.id).limit(1))).first() is not None
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        if p is None:
            toast = _toast("It is not on your shortlist.")
        elif has_offer or not _is_fresh(opp, p):
            toast = _toast(
                "This role has moved on in Applications, so it stays there. "
                "Remove it from More if you really want it gone.",
                tone="warn",
            )
        else:
            snap = await pursuit_undo.remove_snapshot(s, p, opp)
            await remove_from_list(
                s, opp, commit=False, label_ids=[v["id"] for v in snap["labels"]]
            )
            snap["opp_after"] = {k: getattr(opp, k) for k in pursuit_undo.OPP_FIELDS}
            token = await pursuit_undo.save(s, snap)
            await s.commit()
            where = "your leads" if opp.track == "consulting" else "your shortlist"
            toast = _toast(
                f"Removed from {where}. Anything you drafted stays in Documents.",
                f"/pursuits/undo/{token}",
            )
            toast["refresh"] = _side_refresh(opp_id)
        oob = await _oob_counts(s)
    html = await _side_html(request, opp_id)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


@router.post("/opportunities/{opp_id}/role/remove", response_class=HTMLResponse)
async def remove_anyway(request: Request, opp_id: int):
    """Remove a shortlist that has moved on (More). The dialog names what goes with it; the
    server refuses unless the post says `confirmed=1`, whatever the page showed."""
    from kestrel.web.pages.opportunity_actions import remove_from_list

    form = await request.form()
    if str(form.get("confirmed") or "") != "1":
        return _answer({"k-toast": _toast("Nothing was removed. Confirm first.", tone="warn")})
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        text = await remove_from_list(s, opp)
        oob = await _oob_counts(s)
    html = await _side_html(request, opp_id)
    return HTMLResponse(
        html + oob, headers=_events({"pipelineRefresh": True, "k-toast": _toast(text)})
    )


# ---------------------------------------------------------------------------------- Not for me
@router.post("/opportunities/{opp_id}/role/not-for-me", response_class=HTMLResponse)
async def not_for_me(request: Request, opp_id: int, reason: str = ""):
    """One of the four reasons hides the role and records why (the Pipeline rule). Not offered
    for a role on the shortlist: take it off first."""
    from kestrel.web.pages.pipeline import hide_with_reason

    next_id = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
            has_pursuit = (
                await s.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp_id))
            ).first() is not None
            if has_pursuit:
                return _answer(
                    {
                        "k-toast": _toast(
                            "Take it off your shortlist first, then say not for me.", tone="warn"
                        )
                    }
                )
            if opp.archived_at:
                label_id, stamp = None, opp.archived_at
            else:
                seq = await _sequence(s, opp)
                next_id = (seq["next"] or seq["prev"]) if seq else None
                label_id = await hide_with_reason(s, opp, reason)
                stamp = opp.archived_at or ""
                await s.commit()
            oob = await _oob_counts(s)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning("role_not_for_me_failed", opp_id=opp_id, error=str(exc))
            await s.rollback()
            return _answer(
                {"k-toast": _toast(f"Could not hide it: {plain_error(exc)}", tone="warn")}
            )
    undo = f"/opportunities/{opp_id}/decide/undo-hide?at={quote(stamp)}" + (
        f"&label={label_id}" if label_id else ""
    )
    toast = _toast("Got it. Kestrel will learn from this.", undo)
    toast["refresh"] = _side_refresh(opp_id)
    html = await _side_html(request, opp_id, next_id=next_id)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


@router.post("/opportunities/{opp_id}/role/put-back", response_class=HTMLResponse)
async def put_back(request: Request, opp_id: int):
    """Put a hidden role back in Pipeline. The label stays: it was the operator's word then."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is not None and opp.archived_at:
            opp.archived_at = None
            opp.archive_reason = None
            await s.commit()
        oob = await _oob_counts(s)
    html = await _side_html(request, opp_id)
    return HTMLResponse(
        html + oob,
        headers=_events({"pipelineRefresh": True, "k-toast": _toast("Back in Pipeline.")}),
    )


@router.post("/opportunities/{opp_id}/role/archive", response_class=HTMLResponse)
async def archive(request: Request, opp_id: int):
    """Archive: hide the role without saying why (the reason "other"). Undo clears exactly this
    archive, matched on the stamp it wrote."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        stamp = opp.archived_at
        if not stamp:
            opp.archived_at = stamp = utcnow_iso()
            opp.archive_reason = "other"
            await s.commit()
        oob = await _oob_counts(s)
    toast = _toast(
        "Archived. Find it under Hidden in Pipeline.",
        f"/opportunities/{opp_id}/role/unarchive?at={quote(stamp)}",
    )
    toast["refresh"] = _side_refresh(opp_id)
    html = await _side_html(request, opp_id)
    return HTMLResponse(html + oob, headers=_events({"pipelineRefresh": True, "k-toast": toast}))


@router.post("/opportunities/{opp_id}/role/unarchive", response_class=HTMLResponse)
async def unarchive(request: Request, opp_id: int, at: str = ""):
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is not None and at and opp.archived_at == at:
            opp.archived_at = None
            opp.archive_reason = None
            await s.commit()
        oob = await _oob_counts(s)
    return _answer({"pipelineRefresh": True}, oob)


# ----------------------------------------------------------------------------------- Your view
@router.post("/opportunities/{opp_id}/role/view", response_class=HTMLResponse)
async def save_view(request: Request, opp_id: int):
    """Your view: Yes (agree with Kestrel's score) or Adjust (the 1 to 5 picker, optional nudges
    per dimension, an optional note). Saved through `record_override`, the same path as
    /api/opportunities/{id}/override, so Pipeline's "you said" pill, Calibrate and the learning
    loop read one record. The Undo deletes exactly that row. The note is the reason; without one
    the reason says what was picked, so a rating is never saved without a why."""
    from kestrel.web.queries import latest_evaluation, record_override
    from kestrel.web.routes_learning import verdict_from_score

    form = await request.form()
    mode = "agree" if str(form.get("mode")) == "agree" else "adjust"
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        opp = await s.get(Opportunity, opp_id)
        ev = await latest_evaluation(s, opp_id) if opp is not None else None
        if opp is None or ev is None or ev.score is None:
            return _answer(
                {"k-toast": _toast("Kestrel has not scored this role yet.", tone="warn")}
            )
        if mode == "agree":
            row = await record_override(
                s, opp_id=opp_id, mode="agree", reason="Agreed with Kestrel on the Role page."
            )
            msg = "Thanks. Saved as a training example."
        else:
            try:
                score = int(form.get("score"))
            except (TypeError, ValueError):
                score = 0
            if score not in cv.SCORE_WORDS:
                return _answer({"k-toast": _toast("Pick a number from 1 to 5.", tone="warn")})
            nudged: dict[str, Any] = {}
            for d in cv.dim_scores(ev, opp.track):
                raw = form.get(f"dim_{d['key']}")
                if raw in (None, ""):
                    continue
                try:
                    v = max(1.0, min(5.0, round(float(raw) * 2) / 2))
                except ValueError:
                    continue
                stored = (ev.dimensions or {}).get(d["key"])
                orig = stored.get("score") if isinstance(stored, dict) else stored
                try:
                    orig = float(orig)
                except (TypeError, ValueError):
                    orig = float(d["score"])
                if abs(v - orig) >= 0.25:
                    nudged[d["key"]] = v
            note = " ".join(str(form.get("note") or "").split())[:500]
            reason = note or f"Rated {score} of 5 ({cv.SCORE_WORDS[score]}) on the Role page."
            if nudged and not note:
                reason += " Nudged: " + ", ".join(sorted(nudged)) + "."
            row = await record_override(
                s,
                opp_id=opp_id,
                mode="override",
                operator_verdict=verdict_from_score(float(score), opp.track, settings),
                operator_score=float(score),
                reason=reason,
                dimension_corrections={"scores": nudged} if nudged else None,
            )
            msg = "Your rating is saved. Kestrel will learn from it."
        if row is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        oob = await _oob_counts(s)
    toast = _toast(msg, f"/opportunities/{opp_id}/role/view/undo/{row.id}")
    toast["refresh"] = _refresh(("#rl-view", f"/opportunities/{opp_id}/role/view", "outerHTML"))
    cards = await _cards_oob(request, opp_id, view=True)
    return _answer({"k-toast": toast, "pipelineRefresh": True}, cards + oob)


@router.post("/opportunities/{opp_id}/role/view/undo/{override_id}")
async def undo_view(opp_id: int, override_id: int):
    """Undo of Your view: deletes that one row and only if it belongs to this role."""
    from kestrel.learning.ledger import delete_label

    async with sessionmaker()() as s:
        row = await s.get(VerdictOverride, override_id)
        if row is None or row.opportunity_id != opp_id:
            return JSONResponse({"undone": False}, status_code=404)
        ok = await delete_label(s, override_id, kind="override")
    return JSONResponse({"undone": ok}, status_code=200 if ok else 404)


@router.get("/opportunities/{opp_id}/role/view", response_class=HTMLResponse)
async def view_card(request: Request, opp_id: int):
    ctx = await page_context(request, opp_id, "evaluation")
    if ctx is None:
        return HTMLResponse("", status_code=404)
    return templates().TemplateResponse(request, "partials/role_view_card.html", ctx)


# ------------------------------------------------------------------------------------ the story
@router.post("/opportunities/{opp_id}/role/story", response_class=HTMLResponse)
async def add_story(request: Request, opp_id: int):
    """Add a story from a gap or from Evidence. The three boxes are the operator's own words and
    are stored as written (situation, action, result); Kestrel rewrites nothing. Save and re-score
    also queues one re-score task for this role: a resumable task row, on the configured model.
    When that model is online the dialog's confirmation is required and checked here."""
    from kestrel.queue.rescore import enqueue_rescore

    form = await request.form()
    situation = str(form.get("situation") or "").strip()
    action = str(form.get("action") or "").strip()
    result = str(form.get("result") or "").strip()
    rescore = str(form.get("rescore") or "") == "1"
    if not (situation or action or result):
        return _answer(
            {"k-toast": _toast("Write at least one of the three boxes first.", tone="warn")}
        )
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        settings = await load_settings_with_db(s)
        route = await online_route(s, settings, "tier2") if rescore else None
        if route is not None:
            from kestrel.services.online_consent import approval_for

            route["approval"] = json.dumps(
                approval_for(settings, ("tier1", "tier2")), separators=(",", ":")
            )
            route["scoring_routes"] = [
                await online_route(s, settings, tier) for tier in ("tier1", "tier2")
            ]
        verdict = gate(route, form)
        if verdict is None and route is not None:
            from kestrel.services.online_consent import approval_covers, approval_for

            expected = approval_for(settings, ("tier1", "tier2"))
            if expected and not approval_covers(expected, form.get("online_approval")):
                verdict = (
                    "Nothing was sent. The model or online service changed since the dialog "
                    "opened. Press the button again to see what would be sent."
                )
        if verdict == "ask":
            return _answer(
                None,
                _ask(
                    request,
                    route,
                    action="re-score this role",
                    kind="story",
                    url=f"/opportunities/{opp_id}/role/story",
                    role=opp.title or "this role",
                    extra={
                        "situation": situation,
                        "action": action,
                        "result": result,
                        "rescore": "1",
                        "for_text": str(form.get("for_text") or ""),
                    },
                ),
            )
        if verdict:
            return _answer({"k-toast": _toast(verdict, tone="warn")})
        first = next((t for t in (situation, action, result) if t), "")
        title = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0][:60].rstrip(" .") or "My story"
        s.add(
            Story(
                title=title,
                situation=situation or None,
                action=action or None,
                result=result or None,
                themes=[],
                is_canonical=True,
                operator_edited=True,
            )
        )
        await s.flush()
        queued = None
        if rescore:
            credit.clear()
            res = await enqueue_rescore(s, [opp_id], online_approval=form.get("online_approval"))
            queued = bool(res.queued)
        await s.commit()
    if rescore:
        msg = (
            "Story saved. Kestrel will score this role again. Stories are not part of scoring yet."
            if queued
            else "Story saved. This role is already waiting to be scored."
        )
    else:
        msg = "Story saved. It is yours; Kestrel does not rewrite it."
    return _answer({"k-toast": _toast(msg), "rl-story-saved": {"title": title}})


# ------------------------------------------------------------------------ Research and Contacts
@router.post("/opportunities/{opp_id}/role/research", response_class=HTMLResponse)
async def research(request: Request, opp_id: int):
    """Research the company, or Find contacts (kind=company|contacts). Both read public pages
    only and draft nothing that is ever sent. Their model is the Tier 3 one: when it is online the
    click first opens the dialog that names what is sent, and the confirmed form is checked here."""
    form = await request.form()
    kind = "contacts" if str(form.get("kind")) == "contacts" else "company"
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        settings = await load_settings_with_db(s)
        route = await online_route(s, settings, "tier3")
    verdict = gate(route, form)
    if verdict == "ask":
        return _answer(
            None,
            _ask(
                request,
                route,
                action="research the company" if kind == "company" else "find contacts",
                kind=kind,
                url=f"/opportunities/{opp_id}/role/research",
                role=opp.title or "this role",
            ),
        )
    if verdict:
        return _answer({"k-toast": _toast(verdict, tone="warn")})
    credit.clear()
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        settings = await load_settings_with_db(s)
        latest_route = await online_route(s, settings, "tier3")
        refusal = gate(latest_route, form)
        if refusal:
            return _answer({"k-toast": _toast("Settings changed. Start again to confirm the send.",
                                            tone="warn")})
        if opp is None:
            return _answer({"k-toast": _toast("That role is gone.", tone="warn")})
        try:
            added = await research_jobs.enqueue(s, opp, kind, settings,
                                               confirmed_online=latest_route is not None)
        except ValueError as exc:
            return _answer({"k-toast": _toast(str(exc), tone="warn")})
    cards = await _cards_oob(request, opp_id, company=True, orgtab=True)
    return _answer(
        {"k-toast": _toast("Queued. Progress is on the Organization tab." if added
                           else "Already queued. Progress is on the Organization tab.")}, cards
    )


@router.get("/opportunities/{opp_id}/role/research", response_class=HTMLResponse)
async def research_progress(request: Request, opp_id: int):
    ctx = await page_context(request, opp_id, "organization")
    if ctx is None:
        return HTMLResponse("Not found", status_code=404)
    return templates().TemplateResponse(request, "partials/role_tab_organization.html", ctx)


@router.post("/opportunities/{opp_id}/role/research/{task_id}/cancel")
async def cancel_research(request: Request, opp_id: int, task_id: int):
    async with sessionmaker()() as s:
        stopped = await research_jobs.cancel(s, opp_id, task_id)
    return _answer(
        {"k-toast": _toast("Canceled." if stopped else "That task has already stopped.")},
        await _cards_oob(request, opp_id, orgtab=True),
    )


# ------------------------------------------------------------------------------ Deeper read
@router.get("/opportunities/{opp_id}/role/deeper", response_class=HTMLResponse)
async def deeper_dialog(request: Request, opp_id: int):
    """Ask first: the provider, the model, exactly what would be sent (the real payload, behind
    Show details), the cost, and whether the employer is hidden. The confirm form holds the token
    that binds the send to this payload byte for byte."""
    from kestrel.llm.service_name import service_name
    from kestrel.pipeline.tier3 import build_escalation_preview
    from kestrel.web.routes_pages import _cost_line

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        settings = await load_settings_with_db(s)
        preview = await build_escalation_preview(s, settings, opp_id)
    ctx: dict[str, Any] = {"opp": opp, "preview": preview, "error": preview.error}
    if not preview.error:
        ctx.update(
            {
                "service": service_name(preview.base_url),
                "cost": _cost_line(preview.model, preview.prompt_tokens_estimate),
                "payload": "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in preview.messages),
                "sends": settings.llm.hosted_mode.disclosure_terms()["sends"]["frontier"],
            }
        )
    return templates().TemplateResponse(request, "partials/role_deeper.html", ctx)


@router.post("/opportunities/{opp_id}/role/deeper/send", response_class=HTMLResponse)
async def deeper_send(request: Request, opp_id: int):
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline.tier3 import transmit_escalation

    form = await request.form()
    if str(form.get("confirmed") or "") != "1":
        return _answer(
            {"k-toast": _toast("Nothing was sent. Confirm in the dialog first.", tone="warn")}
        )
    token = str(form.get("confirmation_token") or "")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        async with httpx.AsyncClient() as http:
            client = LLMClient(settings, http=http)
            res = await transmit_escalation(s, client, settings, opp_id, confirmation_token=token)
    if res.sent and res.evaluation_id:
        word = fit_word_for_verdict(res.verdict, res.score) or res.verdict
        return _answer(
            {"k-toast": _toast(f"Sent. The stronger model says {word}."), "rl-refresh": True}
        )
    if res.sent:
        return _answer(
            {
                "k-toast": _toast(
                    f"Sent, but no usable answer came back: {res.error}", tone="warn"
                ),
                "rl-refresh": True,
            }
        )
    return _answer({"k-toast": _toast(f"Nothing was sent: {res.error}", tone="warn")})


# ------------------------------------------------------------------------------ Document preview
@router.get("/opportunities/{opp_id}/role/preview/{render_id}", response_class=HTMLResponse)
async def preview(request: Request, opp_id: int, render_id: int):
    """A document as a scaled true page: the same HTML the PDF is made from (the operator's own
    bullets, byte for byte), in a frame the page scales and never reflows."""
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.docs.render import render_html

    async with sessionmaker()() as s:
        render = await s.get(DocRender, render_id)
        if render is None or render.opportunity_id != opp_id:
            return HTMLResponse("", status_code=404)
        settings = await load_settings_with_db(s)
        master = await s.get(DocMaster, render.doc_master_id)
        catalog = await load_catalog(s, render.doc_master_id)
        doc_html = None
        if catalog is not None:
            cfg = dict(render.config or {})
            cfg["redact_orgs"] = bool(cfg.get("redact_orgs") or settings.confidential_mode)
            doc_html = render_html(resolve(catalog, cfg))
    ctx = {
        "render": render,
        "master": master,
        "opp_id": opp_id,
        "doc_html": doc_html,
        "has_pdf": bool(render.pdf_path),
        "label": words(master.track if master else None)["doc_title"]
        if master and master.doc_kind == "resume"
        else (master.name if master else "Document"),
    }
    return templates().TemplateResponse(request, "partials/role_preview.html", ctx)
