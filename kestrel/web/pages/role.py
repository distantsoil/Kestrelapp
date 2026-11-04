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
import contextvars
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    DocMaster,
    DocRender,
    DocSection,
    DocSlot,
    DocVariant,
    Evaluation,
    Opportunity,
    Org,
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
from kestrel.urls import safe_web_url
from kestrel.web import calibrate_view as cv
from kestrel.web import pipeline_view as pv
from kestrel.web import role_view as rv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON, fit_word_for_verdict
from kestrel.web.pages.pipeline import _events, _toast
from kestrel.web.queries import (
    hidden_unscored,
    opportunity_history,
    opportunity_renders,
    org_context,
    scoring_task_exists,
)
from kestrel.web.redact import redact_json, redact_text
from kestrel.web.rescoring import EARLIER, rescoring_states
from kestrel.web.words import words

log = structlog.get_logger()
router = APIRouter()

TABS = rv._ORDER


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
        # Hidden as applied with nothing queued: Not scored, never Being scored (issue #59).
        # Score again queues a live task, and then it is Being scored after all.
        detail["hidden_why"] = (await hidden_unscored(s2, [opp])).get(opp.id)
        from kestrel.web.queries import scored_twins

        # Unscored means the role has no scored Tier 2 or Tier 3 evaluation (the same test
        # scored_twins applies to the copy): a Tier 1 screen, or a Tier 2 row without a score
        # (quarantined), still shows the scored copy's line.
        scored = (ev is not None and (getattr(ev, "tier", None) or 0) >= 2
                  and getattr(ev, "score", None) is not None)
        unscored = not scored
        detail["twin"] = (await scored_twins(s2, [opp])).get(opp.id) if unscored else None
        detail["empty_posting"] = (opp.stage == "t1_pass"
                                   and opp.stage_reason == EMPTY_POSTING_STAGE_REASON)
        detail["posting_url"] = safe_web_url(opp.url)
        # Any scoring task, held or not: the role is Being scored then (issue #90).
        detail["queued"] = bool((await s2.execute(
            select(scoring_task_exists()).where(Opportunity.id == opp_id))).scalar())
        # Queued or running to score again (issue #74); a task held for approval is not in line.
        detail["rescoring"] = (
            None if detail["scoring_held"]
            else (await rescoring_states(s2, [opp_id])).get(opp_id)
        )
        stats = None if track == "consulting" else await comp_intel_stats(s2, opp)
        n_seen = 0 if track == "consulting" else await comp_intel_count(s2, opp)
        from kestrel.services.profile_readiness import operator_profile_is_thin

        thin = await asyncio.to_thread(operator_profile_is_thin, track)
        reqs = rv.requirements(opp, ev_json, thin=thin)
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
        cover = await latest_letter(s2, opp_id)
        from kestrel.docs import letter_task

        ltask = letter_task.summary(await letter_task.latest_task(s2, opp_id))
    fit = detail["fit"]
    reqs_wheel = rv.wheel(reqs)
    w = words(track)
    detail.update(
        {
            "tab": tab,
            "earlier_label": EARLIER,
            "tab_labels": rv.TAB_LABELS,
            "tabs": TABS,
            "w": w,
            "applied_on_default": date.today().isoformat(),
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
            "drafting": ltask["state"] == "drafting",
            "letter_task": ltask,
            "nf_reasons": pv.reasons_for(track),
            "request": request,
        }
    )
    detail["first_status"] = _first_status(opp.track)
    detail["fresh"] = _is_fresh(opp, pursuit)
    detail["doc_rows"] = _doc_rows(detail, cover, ltask)
    detail["mini_rows"] = [d for d in detail["doc_rows"] if d["render_id"]]
    detail["has_docs"] = bool(detail["mini_rows"])
    return detail


def _doc_rows(
    detail: dict[str, Any], cover, ltask: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The two document rows (resume and cover letter; capability statement and engagement
    outline): the latest render of each that exists, else what is missing and why."""
    opp, renders = detail["opp"], detail["renders"]
    ltask = ltask or {"state": None}
    drafting = ltask["state"] == "drafting"
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
    unlocked = detail.get("pursuit") is not None or bool(renders)
    if cover is not None:
        from kestrel.date_display import format_display as _fd

        cfg = cover.config or {}
        words_n = int(cfg.get("words") or 0)
        sub = (f"Draft · {words_n} words · {'edited' if cfg.get('edited') else 'drafted'} "
               f"{_fd(cover.created_at)}. Kestrel never sends it.")
        # A redraft that is on hold or did not finish says so here, after the draft it would
        # replace (which stays until a new one is saved).
        newer = (ltask.get("at") or "") > (cover.created_at or "")
        if ltask["state"] == "held" and newer:
            sub += " A new draft is on hold: use Draft again to confirm it. Nothing was sent."
        elif ltask["state"] == "failed" and newer:
            sub += f" The last new draft did not finish: {ltask['reason']}"
        rows.append(
            {
                "title": second,
                "icon": "prep",
                "letter": True,
                "render_id": cover.id,
                "has_pdf": False,
                "short": "Draft",
                "sub": sub,
                "action": "Open draft",
                "enabled": True,
            }
        )
    else:
        held, failed = ltask["state"] == "held", ltask["state"] == "failed"
        base = (
            ("An outline for a conversation, not a priced proposal. " if consulting else "")
            + "Kestrel drafts it from your own proof points and resume lines, and never sends it."
        )
        if drafting:
            short, sub = "Drafting", (
                "Drafting now. You can leave this page; the draft will be here when it is done.")
        elif held:
            short, sub = "On hold", (
                "On hold. The model or online service changed since you confirmed, so nothing "
                "was sent. Open it to confirm again.")
        elif failed:
            short, sub = "Not drafted", (
                f"No draft was made. {ltask['reason']} Nothing was written to your documents.")
        else:
            short, sub = "No draft yet", "No draft yet. " + base
        rows.append(
            {
                "title": second,
                "icon": "prep",
                "letter": True,
                "render_id": None,
                "has_pdf": False,
                "short": short,
                "sub": sub,
                "action": ("Drafting" if drafting else "Review and confirm" if held
                           else "Try again" if failed else "Draft"),
                "enabled": unlocked and not drafting,
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


@router.get("/opportunities/{opp_id}/role/rescoring")
async def rescoring_status(opp_id: int):
    """Where a score again stands, for the role page's poll (issue #74): `state` is "queued",
    "running" or "none" (it landed, failed or was cancelled, so the page asks for the new card)."""
    async with sessionmaker()() as s:
        found = (await rescoring_states(s, [opp_id])).get(opp_id)
    if found is not None and found.awaiting_ok:
        found = None  # held for the person's OK: the page redraws and shows its confirm card
    return JSONResponse(found.as_dict() if found else {"state": "none"},
                        headers={"Cache-Control": "no-store"})


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
            res = await enqueue_rescore(
                s, [opp_id], online_approval=form.get("online_approval"), score_stubs=True
            )
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


# ------------------------------------------------------------ Cover letter and engagement outline
# D-UI-239. One row in Documents with one action (Draft / Open draft). Local mode drafts on this
# computer with no consent step; an online model first shows the ask-first panel and nothing is
# sent until the confirmed form is posted, checked again here (`consent_problem`), including the
# plain-writing re-ask, which goes to the same model the panel named. A draft is a `task` row
# (kestrel.docs.letter_task, hard rule 5): the request queues it and runs it here so the sheet
# answers when it is done, and if the app quits first the serve worker leases it again on the
# next start. An online task carries the model and destination that were confirmed and is held,
# not sent, when they no longer match. The draft is a DocRender row (template "cover_letter" /
# "engagement_outline"; its text and checks live in `config`), so it survives a reload, shows in
# Documents, and downloads through the same PDF plumbing. Nothing here sends, submits or emails
# anything (hard rule 1).
_LETTER_URL = "/opportunities/{}/role/cover-letter"
_NO_BASIS = (
    "Nothing was sent. Kestrel needs a resume (or a capability statement) to draft from, so it "
    "can use only your own lines. Add one under Documents first."
)
_EMPTY_LETTER = "A draft cannot be empty. Discard it instead."
_SAVED = "Saved."
_JOIN_REFUSED = (
    "Nothing was sent. A draft is already under way with a different confirmation. Wait for it, "
    "or close this and open the draft from Documents."
)


def _letter_kind(opp) -> str:
    from kestrel.docs.letters import COVER, OUTLINE

    return OUTLINE if opp.track == "consulting" else COVER


def _letter_noun(kind: str) -> str:
    from kestrel.docs.letters import OUTLINE

    return "engagement outline" if kind == OUTLINE else "cover letter"


async def _letter_check(task) -> str | None:
    """Why a queued draft may not send now (None: it may run). A local route needs nothing. An
    online one must still match the model and destination the person confirmed, with Hosted mode
    on and, in confidential mode, an employer chosen; else the task is held and nothing is sent
    (on the first run, a retry and a resume after a restart alike)."""
    from kestrel.services.online_consent import CONSENT_DEST_KEY, CONSENT_KEY, consent_problem

    payload = task.payload or {}
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, payload.get("opportunity_id"))
        if opp is None:
            return None  # the run reports that the role is gone; nothing is sent
        route = await _letter_route(s, await load_settings_with_db(s), opp)
    confirmed = {
        "confirmed": "1" if payload.get(CONSENT_KEY) else "",
        "model": payload.get(CONSENT_KEY) or "", "dest": payload.get(CONSENT_DEST_KEY) or "",
    }
    return consent_problem(route, confirmed)


# What the running task's person confirmed ({} for a local draft). `_make_draft` checks it against
# the settings it is about to send with, so a change between the lease and the send holds the
# task instead of sending. None outside a task: nothing to check against.
_TASK_CONSENT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "letter_task_consent", default=None
)


async def _run_letter_task(task) -> dict[str, Any]:
    """Make the draft a task asks for; a redraft then replaces the draft it names."""
    from kestrel.services.online_consent import CONSENT_DEST_KEY, CONSENT_KEY

    payload = task.payload or {}
    opp_id = int(payload["opportunity_id"])
    seen = {k: payload[k] for k in (CONSENT_KEY, CONSENT_DEST_KEY) if payload.get(k)}
    marker = _TASK_CONSENT.set(seen)
    try:
        outcome = await _make_draft(opp_id)
    finally:
        _TASK_CONSENT.reset(marker)
    replace_id = payload.get("replace_id")
    if outcome.get("ok") and replace_id and int(replace_id) != outcome.get("render_id"):
        async with sessionmaker()() as s:
            await _drop_letter(s, opp_id, int(replace_id))  # the new draft is in; the old goes
            await s.commit()
    return outcome


async def run_letter_drafts(
    *, stop=None, task_id: int | None = None, worker_id: str = "pipeline-letters"
) -> int:
    """Run queued cover letter and outline drafts (all, or one task). Only the serve worker calls
    this (after the tiers have drained, and never while paused), every cycle, so a draft
    interrupted by a quit or crash resumes after the restart. A request never runs one."""
    from kestrel.docs import letter_task
    from kestrel.web.app import is_paused

    return await letter_task.drain(
        sessionmaker(), _run_letter_task, _letter_check, stop=stop, task_id=task_id,
        worker_id=worker_id, paused=is_paused,
    )


async def latest_letter(s, opp_id: int) -> DocRender | None:
    """The role's saved cover letter or engagement outline, or None."""
    from kestrel.docs.letters import LETTER_TEMPLATES

    return (
        await s.execute(
            select(DocRender)
            .where(DocRender.opportunity_id == opp_id, DocRender.template.in_(LETTER_TEMPLATES))
            .order_by(DocRender.id.desc())
            .limit(1)
        )
    ).scalars().first()


def _name_sources(doc) -> list[str]:
    """The person's own resume text (roles, companies, titles, bullets), matched locally against
    names in a draft so a real employer is never marked unverified. Never sent anywhere."""
    out: list[str] = [str(v) for v in (doc.header or {}).values() if isinstance(v, str)]
    for block in doc.blocks:
        out += [t for t in (block.text, block.heading) if t]
        for role in block.roles:
            out += [t for t in (role.org, role.title, role.context) if t]
            out += [b.text for b in role.bullets]
        for e in block.entries:
            out += [t for t in (e.lead, e.sub, e.detail, e.extra) if t]
    return out


async def _letter_basis(s, settings, opp, basis: dict | None = None):
    """The person's document the letter draws its lines from: (master, config, resolved doc) or
    None. A new draft uses the latest tailored resume for the role, else the track's active one;
    a saved draft reads back the pick it was made from (`basis`)."""
    from kestrel.docs.catalog import load_catalog, resolve
    from kestrel.web.routes_pages import _pick_master

    master_id, cfg = None, {}
    if basis and basis.get("master_id"):
        master_id, cfg = int(basis["master_id"]), dict(basis.get("config") or {})
    else:
        want = "capability_statement" if opp.track == "consulting" else "resume"
        for row in await opportunity_renders(s, opp.id):
            if row["master"] is not None and row["master"].doc_kind == want:
                master_id, cfg = row["render"].doc_master_id, dict(row["render"].config or {})
                break
        if master_id is None:
            masters = list((await s.execute(select(DocMaster).where(
                DocMaster.track == opp.track, DocMaster.is_active.is_(True)
            ))).scalars())
            picked = _pick_master(masters, opp.track)
            master_id = picked.id if picked is not None else None
    if master_id is None:
        return None
    catalog = await load_catalog(s, master_id)
    if catalog is None:
        return None
    cfg["redact_orgs"] = bool(cfg.get("redact_orgs") or settings.confidential_mode)
    return catalog.master, cfg, resolve(catalog, cfg)


def _variants(doc) -> list[tuple[str, str]]:
    return [(b.key, b.text) for blk in doc.blocks for role in blk.roles for b in role.bullets]


def _evaluation_lines(ev, kind: str) -> dict[str, str]:
    """What Kestrel already worked out about the role, as short labeled lines for the prompt."""
    blocks = ev.blocks if ev is not None and isinstance(ev.blocks, dict) else {}
    out: dict[str, str] = {}
    if kind == "engagement_outline":
        sit, thesis = blocks.get("a_situation") or {}, blocks.get("b_thesis") or {}
        eco, app = blocks.get("d_economics") or {}, blocks.get("e_approach") or {}
        out["Situation (stated)"] = str(sit.get("stated") or "")
        out["Situation (inferred)"] = str(sit.get("inferred") or "")
        out["Engagement thesis"] = str(thesis.get("engagement") or "")
        out["Proposed phases"] = "; ".join(
            f"{p.get('name')} ({p.get('days')} days)" for p in thesis.get("phases") or []
            if isinstance(p, dict)
        )
        out["Economics"] = f"{eco.get('rate_low')}-{eco.get('rate_high')}" if eco else ""
        out["Opening move"] = str(app.get("opening_move") or "")
        return out
    angle = blocks.get("e_angle") or {}
    out["What to lead with"] = str(angle.get("lead_with") or "")
    out["Phrases from the posting worth echoing"] = "; ".join(
        str(c) for c in angle.get("jd_citations") or [])
    gaps = [g for g in ((blocks.get("b_fit") or {}).get("gaps") or []) if isinstance(g, dict)]
    rank = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda g: rank.get(str(g.get("severity")), 3))
    out["Gap to acknowledge in one sentence, in your own voice"] = (
        str(gaps[0].get("requirement") or "") if gaps else "")
    out["How senior the role reads"] = str((blocks.get("c_level") or {}).get("scope_read") or "")
    return out


def _failure_sentence(draft) -> str:
    """What the person is told when no draft came back: one of Kestrel's own sentences, picked by
    kind. Nothing of the model's reply, an exception or a provider message is ever shown."""
    from kestrel.errors import classify_model_failure

    if draft.out_of_credit and draft.error:
        return draft.error  # authored by kestrel.llm.credit
    if draft.refused:
        return _refusal_sentence(draft.error)
    if getattr(draft, "third_person", False):
        return ("The draft was written about you instead of as you, even after one more try, so "
                "none was kept. Draft again to try once more.")
    if draft.quarantined:
        return ("The model's reply could not be read as a draft, so none was made. Kestrel kept "
                "the reply so you can see what came back.")
    return classify_model_failure(draft.error)[2]


def _refusal_sentence(reason: str | None) -> str:
    """Why Kestrel did not send a hosted draft, as one of its own sentences (the plan's reason
    is a log line, not copy). A refusal has no reply, so there is nothing to look at."""
    from kestrel.pipeline._shared import HOSTED_REFUSAL
    from kestrel.services.online_consent import HELD

    if reason == HOSTED_REFUSAL:
        return HELD
    if "turned off" in (reason or ""):
        return "Nothing was sent. Online models need Hosted mode, which is off."
    return "Nothing was sent. Kestrel refused this online send, so no draft was made."


async def _make_draft(opp_id: int) -> dict[str, Any]:
    """Draft and save the letter. Runs detached from the request, so leaving the page never loses
    it. Returns {"ok": True, "render_id"} or {"ok": False, "reason", "quarantined"}; the reason
    is a fixed sentence."""
    from kestrel.docs.letters import (
        build_letter_prompt,
        generate_cover,
        generate_engagement,
        proof_rows,
        source_map,
        word_count,
    )
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline._shared import jd_budget, make_quarantine_hook
    from kestrel.pipeline.context import normalize_description
    from kestrel.pipeline.profile import load_profile
    from kestrel.web.errors import fixed_reason

    try:
        async with sessionmaker()() as s:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return {"ok": False, "reason": "That role is gone.", "quarantined": False}
            kind = _letter_kind(opp)
            settings = await load_settings_with_db(s)
            found = await _letter_basis(s, settings, opp)
            if found is None:
                return {"ok": False, "reason": _NO_BASIS, "quarantined": False}
            master, cfg, doc = found
            confirmed = _TASK_CONSENT.get()
            if confirmed is not None:  # a queued draft: still the model and place that were OK'd
                from kestrel.services.online_consent import (
                    CONSENT_DEST_KEY,
                    CONSENT_KEY,
                    consent_problem,
                )

                held = consent_problem(await _letter_route(s, settings, opp), {
                    "confirmed": "1" if confirmed.get(CONSENT_KEY) else "",
                    "model": confirmed.get(CONSENT_KEY) or "",
                    "dest": confirmed.get(CONSENT_DEST_KEY) or "",
                })
                if held:  # nothing has been sent; the task waits for a new confirmation
                    return {"ok": False, "held": True, "reason": held, "quarantined": False}
            profile = await asyncio.to_thread(load_profile, opp.track)
            proofs = proof_rows(profile)
            variants = _variants(doc)
            sources = source_map(proofs, variants)
            ev = (await s.execute(
                select(Evaluation).where(Evaluation.opportunity_id == opp.id, Evaluation.tier == 2)
                .order_by(Evaluation.created_at.desc(), Evaluation.id.desc()).limit(1)
            )).scalars().first()
            org = await s.get(Org, opp.org_id) if opp.org_id else None
            from kestrel.ingest.tokens import truncate_middle
            from kestrel.interview.prep import _latest_research

            research = await _latest_research(s, org.id if org else None)
            desc = truncate_middle(
                normalize_description(opp.description_md),
                jd_budget(settings, "tier3", settings.pipeline.max_jd_tokens),
            )
            company = (org.name if org else None) or opp.org_name or ""
            prompt = build_letter_prompt(
                kind, proofs=proofs, variants=variants,
                headline=str(getattr(profile, "headline", "") or ""),
                narrative=str(getattr(profile, "narrative", "") or ""),
                evaluation=_evaluation_lines(ev, kind),
                research=(research.summary_md or "") if research else "",
                title=opp.title or "", company=company, description=desc,
                org_line=" | ".join(x for x in (
                    company, f"sector {org.sector}" if org and org.sector else "",
                    f"size {org.size_band}" if org and org.size_band else "",
                    f"ownership {org.ownership}" if org and org.ownership else "") if x),
            )
            hook = make_quarantine_hook(s, tier=4, opportunity_id=opp.id)
            # Hard rule 2: only the person's own proof points and resume lines ground a number
            # or a name. The posting is the model's input, never the person's claim.
            grounding = [t["text"] for t in sources.values()]
            generate = generate_engagement if kind == "engagement_outline" else generate_cover
            async with httpx.AsyncClient() as http:
                draft = await generate(
                    LLMClient(settings, http=http), user_prompt=prompt, valid_keys=set(sources),
                    on_quarantine=hook, session=s, grounding=grounding,
                    name_sources=_name_sources(doc), opportunity_id=opp.id,
                    posting=normalize_description(opp.description_md),
                )
            if draft.value is None:
                await s.commit()  # the quarantine row, with the raw reply kept
                return {"ok": False, "reason": _failure_sentence(draft),
                        "quarantined": draft.quarantined and not draft.refused}
            v = draft.value
            extras: dict[str, Any] = {}
            if kind == "engagement_outline":
                extras = {
                    "assumptions": list(v.assumptions), "exclusions": list(v.exclusions),
                    "commercial": v.commercial_shape.model_dump(mode="json"),
                    "too_few_exclusions": draft.too_few_exclusions,
                }
            row = DocRender(
                opportunity_id=opp.id, doc_master_id=master.id, template=kind,
                config={
                    "kind": kind, "body_md": v.body_md, "generated_md": v.body_md,
                    "claims_used": list(v.claims_used), "flags": list(draft.style_flags),
                    "basis": {"master_id": master.id, "config": {
                        k: val for k, val in cfg.items() if k != "redact_orgs"}},
                    "edited": False, "words": word_count(v.body_md), **extras,
                },
                rationale={}, defaults_used=False,
            )
            s.add(row)
            await s.flush()
            pursuit = (await s.execute(
                select(Pursuit).where(Pursuit.opportunity_id == opp.id)
            )).scalars().first()
            if pursuit is not None:
                pursuit.cover_letter_id = row.id
            await s.commit()
            return {"ok": True, "render_id": row.id}
    except Exception as exc:  # noqa: BLE001 (the person is told a fixed sentence; details go to the log)
        log.warning("letter_draft_failed", opportunity_id=opp_id, error_type=type(exc).__name__)
        return {"ok": False, "reason": f"No draft was made: {fixed_reason(exc)}.",
                "quarantined": False}


async def _letter_ctx(s, settings, opp, row: DocRender, *, tab: str = "check", saved: bool = False):
    """Everything the draft view prints, recomputed from the saved text and the person's current
    proof points and resume lines (so a claim that now resolves stops being flagged)."""
    from kestrel.docs.letters import (
        WORD_RANGE,
        annotate,
        banned_hits,
        letter_html,
        proof_rows,
        source_map,
        unverified_details,
        verify_claims,
        word_count,
    )
    from kestrel.pipeline.context import normalize_description
    from kestrel.pipeline.profile import load_profile
    from kestrel.web.redact import confidential

    cfg = dict(row.config or {})
    kind, stored = cfg.get("kind") or row.template, str(cfg.get("body_md") or "")
    found = await _letter_basis(s, settings, opp, cfg.get("basis"))
    profile = await asyncio.to_thread(load_profile, opp.track)
    variants = _variants(found[2]) if found else []
    sources = source_map(proof_rows(profile), variants)
    claims = [str(c) for c in cfg.get("claims_used") or []]
    unresolved = verify_claims(claims, set(sources))
    # Confidential mode: the current employer is hidden in everything the person is shown (the
    # check view, the edit text, the preview, the sources) and in the PDF. The checks below run on
    # the stored text, so a hidden name is never mistaken for an unverified one.
    conf, mapping = await confidential(s)
    body = redact_text(stored, mapping, enabled=conf) or ""
    shown = {k: {**v, "text": redact_text(v["text"], mapping, enabled=conf),
                 "parts": [redact_text(x, mapping, enabled=conf) for x in v.get("parts") or []]}
             for k, v in sources.items()}
    flags = [f for f in cfg.get("flags") or [] if isinstance(f, dict)]
    # Hard rule 2: the unverified-detail check runs again on the text as it is now, against the
    # person's own proof points and resume lines only (never the posting), so a number or name
    # added by hand is marked in the check view, the preview warning and before a download, and
    # one that now matches a source stops being marked.
    fresh = unverified_details(
        stored, [t["text"] for t in sources.values()],
        _name_sources(found[2]) if found else None,
        normalize_description(opp.description_md),
    )
    if cfg.get("edited"):
        # The style marks are on Kestrel's wording, so they go once the person has changed it
        # (the stock-phrase check below still runs on their text).
        flags = []
    kept = [f for f in flags if f.get("kind") != "ungrounded"] + fresh
    located = list(fresh)
    banned = banned_hits(stored)
    lo, hi = WORD_RANGE[kind]
    n = word_count(stored)
    header = found[2].header if found else {}
    used = [{"key": k, **shown[k]} for k in dict.fromkeys(claims) if k in shown]
    return {
        "opp": opp, "opp_id": opp.id, "render": row, "kind": kind, "noun": _letter_noun(kind),
        "outline": kind == "engagement_outline", "body_md": body, "n_words": n,
        "lo": lo, "hi": hi, "paragraphs": annotate(body, shown, claims, kept, banned),
        "used": used, "unresolved": unresolved, "located": located,
        "unverified": bool(unresolved or located), "banned": banned,
        "style_flags": [f for f in kept if f.get("kind") == "style"],
        "preview_html": letter_html(header, body, kind, preview=True), "tab": tab, "saved": saved,
        "extras": {k: cfg.get(k) for k in ("assumptions", "exclusions", "commercial",
                                           "too_few_exclusions")} if kind == "engagement_outline"
        else {},
        "edited": bool(cfg.get("edited")),
    }


async def _letter_route(s, settings, opp) -> dict[str, Any]:
    from kestrel.services.online_consent import online_route as shared_route

    noun = _letter_noun(_letter_kind(opp))
    route = await shared_route(
        s, settings, "tier3", reask=True,
        this_send=f"the posting, your proof points and the resume lines chosen for this {noun}",
    )
    if route.get("online"):
        route["free"] = prov.is_free_variant(str(route["model"]))
    return route


def _fragment(request: Request, name: str, ctx: dict[str, Any], events=None, status=200):
    html = templates().get_template(f"partials/{name}.html").render(**{"request": request, **ctx})
    return HTMLResponse(html, headers=_events(events), status_code=status)


async def _entry(
    request: Request, s, opp, *, shell: bool, events=None, show_failed: bool = False
):
    """What opening the row shows: the saved draft; a draft on its way (the drafting card, which
    only watches it); a draft held because the model or service changed (the ask-first panel
    again); the reason the last try failed; else (online) the ask-first panel, else (on this
    computer) the drafting card, which starts the draft with no consent step."""
    from kestrel.docs import letter_task

    settings = await load_settings_with_db(s)
    ctx: dict[str, Any] = {"opp": opp, "opp_id": opp.id, "noun": _letter_noun(_letter_kind(opp)),
                           "kind": _letter_kind(opp)}
    row = await latest_letter(s, opp.id)
    live = await letter_task.live_task(s, opp.id)
    replacing = live is not None and bool((live.payload or {}).get("replace_id"))
    if row is not None and not replacing:  # a draft in hand, unless a redraft is on its way
        ctx.update(await _letter_ctx(s, settings, opp, row))
        part = "role_letter_draft"
    else:
        route = await _letter_route(s, settings, opp)
        ctx["route"] = route
        latest = live or await letter_task.latest_task(s, opp.id)
        state = letter_task.summary(latest)
        if live is not None and letter_task.is_held(live) and route.get("online"):
            ctx.update(held=True, replace_id=(live.payload or {}).get("replace_id"))
            part = "role_letter_ask"
        elif live is not None:
            if letter_task.is_held(live):  # it runs on this computer now: nothing to confirm
                await letter_task.release_holds(sessionmaker(), _letter_check)
            ctx["watching"] = True
            part = "role_letter_drafting"
        elif show_failed and state["state"] == "failed":
            ctx.update(reason=state["reason"], quarantined=bool(state.get("quarantined")),
                       online=bool(route.get("online")))
            part = "role_letter_failed"
        else:
            part = "role_letter_ask" if route.get("online") else "role_letter_drafting"
    ctx["part"] = part
    if shell:
        return templates().TemplateResponse(request, "partials/role_letter.html", ctx)
    return _fragment(request, part, ctx, events)


@router.get("/opportunities/{opp_id}/role/cover-letter", response_class=HTMLResponse)
async def cover_letter_dialog(request: Request, opp_id: int):
    """The row's one action. Opens the sheet on the draft, the ask-first panel or the drafting
    card; nothing is sent by opening it."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        return await _entry(request, s, opp, shell=True, show_failed=True)


@router.get("/opportunities/{opp_id}/role/cover-letter/status", response_class=HTMLResponse)
async def cover_letter_status(request: Request, opp_id: int):
    """The drafting card asks this every few seconds: still drafting, the finished draft, the
    ask-first panel again (held), or why it failed. It starts nothing and sends nothing."""
    from kestrel.docs import letter_task

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        done = (await latest_letter(s, opp_id) is not None
                and await letter_task.live_task(s, opp_id) is None)  # a redraft is not done yet
        return await _entry(
            request, s, opp, shell=False, show_failed=True,
            events={"rl-refresh": True} if done else None,
        )


@router.get("/opportunities/{opp_id}/role/cover-letter/start", response_class=HTMLResponse)
async def cover_letter_start(request: Request, opp_id: int):
    """The same entry as a fragment, for Try again: an online model asks first again."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        return await _entry(request, s, opp, shell=False)


@router.post("/opportunities/{opp_id}/role/cover-letter/generate", response_class=HTMLResponse)
async def cover_letter_generate(request: Request, opp_id: int):
    """Draft the letter. An online model needs the confirmed ask-first form (confirmed, model and
    destination), checked again here against the settings in effect now; without it nothing is
    sent and the panel is shown again. The draft is a queued task that runs detached, so closing
    the sheet or leaving the page does not lose it. A second request for the same role joins the
    task in flight (it never queues another, nor asks the model again); online, it must carry the
    same confirmation the task holds, or it is refused. A held task takes the new confirmation."""
    from kestrel.docs import letter_task
    from kestrel.services.online_consent import consent_payload, consent_problem

    form = await request.form()
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("", status_code=404)
        settings = await load_settings_with_db(s)
        replace_id = await _replace_target(s, opp_id, form)
        live = await letter_task.live_task(s, opp_id)
        if live is None and replace_id is None and await latest_letter(s, opp_id) is not None:
            return await _entry(request, s, opp, shell=False)  # already drafted: nothing is sent
        route = await _letter_route(s, settings, opp)
        problem = consent_problem(route, form)
        if problem:
            if replace_id is not None and live is None:  # the panel again, still replacing
                return await _redraft_entry(
                    request, s, opp, replace_id, events={"k-toast": _toast(problem, tone="warn")}
                )
            return await _entry(
                request, s, opp, shell=False,
                events={"k-toast": _toast(problem, tone="warn")},
            )
        consent = consent_payload(route)
        kind = _letter_kind(opp)
        if live is not None and letter_task.is_held(live):
            await letter_task.reconfirm(s, live, consent)  # the new, checked confirmation
            await s.refresh(live)  # the bulk update did not touch this loaded copy
            task = live
        else:
            task = live
            if task is None:
                task, _created = await letter_task.enqueue_or_join(
                    s, opp_id, kind=kind, consent=consent, replace_id=replace_id)
            held_consent = {k: (task.payload or {}).get(k) for k in consent}
            if consent and held_consent != consent:
                # A draft is already under way on a different confirmation: this request neither
                # changes it nor rides on it.
                return await _entry(
                    request, s, opp, shell=False,
                    events={"k-toast": _toast(_JOIN_REFUSED, tone="warn")},
                )
        # The worker loop drains the task (after the tiers, never while paused); this answer is
        # the drafting card, which asks the status route until the draft is there.
        return await _entry(request, s, opp, shell=False)


async def _replace_target(s, opp_id: int, form) -> int | None:
    """The draft a redraft replaces (its id), when the form names one that is this role's own
    latest letter; else None."""
    raw = str(form.get("replace") or "")
    if not raw.isdigit():
        return None
    row = await _owned_letter(s, opp_id, int(raw))
    latest = await latest_letter(s, opp_id)
    return row.id if row is not None and latest is not None and latest.id == row.id else None


async def _drop_letter(s, opp_id: int, render_id: int) -> None:
    """Delete one letter row and its PDF; the pursuit lets go of it first (foreign key)."""
    row = await _owned_letter(s, opp_id, render_id)
    if row is None:
        return
    for pursuit in (await s.execute(
        select(Pursuit).where(Pursuit.cover_letter_id == row.id)
    )).scalars():
        pursuit.cover_letter_id = None
    await s.flush()
    pdf = paths.stored_pdf(row.pdf_path)  # a stored path outside the documents folder is ignored
    await s.delete(row)
    _remove_pdf(pdf)


def _remove_pdf(pdf) -> None:
    """Delete a draft's saved PDF (already confined to the documents folder by the caller)."""
    if pdf is None:
        return
    try:
        pdf.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("letter_pdf_remove_failed", error_type=type(exc).__name__)


async def _redraft_entry(request: Request, s, opp, replace_id: int, events=None):
    """The first-draft path for a redraft: the ask-first panel (online) or the drafting card
    (this computer), both carrying the id of the draft to replace. Nothing is sent from here."""
    settings = await load_settings_with_db(s)
    route = await _letter_route(s, settings, opp)
    ctx = {"opp": opp, "opp_id": opp.id, "noun": _letter_noun(_letter_kind(opp)),
           "kind": _letter_kind(opp), "route": route, "replace_id": replace_id}
    part = "role_letter_ask" if route.get("online") else "role_letter_drafting"
    return _fragment(request, part, ctx, events)


@router.post("/opportunities/{opp_id}/role/cover-letter/{render_id}/redraft",
             response_class=HTMLResponse)
async def cover_letter_redraft(request: Request, opp_id: int, render_id: int):
    """Draft again, after the sheet's question. The old draft stays until a new one is saved.
    Online, this shows the ask-first panel again and sends nothing; the draft call itself checks
    the ticked confirmation."""
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        row = await _owned_letter(s, opp_id, render_id)
        if opp is None or row is None:
            return HTMLResponse("", status_code=404)
        return await _redraft_entry(request, s, opp, row.id)


async def _owned_letter(s, opp_id: int, render_id: int) -> DocRender | None:
    from kestrel.docs.letters import LETTER_TEMPLATES

    row = await s.get(DocRender, render_id)
    if row is None or row.opportunity_id != opp_id or row.template not in LETTER_TEMPLATES:
        return None
    return row


@router.post("/opportunities/{opp_id}/role/cover-letter/{render_id}/save",
             response_class=HTMLResponse)
async def cover_letter_save(request: Request, opp_id: int, render_id: int):
    """Save the edited text. The checks are recomputed from it; a short confirmation shows."""
    form = await request.form()
    text = str(form.get("body_md") or "").replace("\r\n", "\n").strip()[:20000]
    tab = "edit" if str(form.get("tab") or "") == "edit" else "check"
    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        row = await _owned_letter(s, opp_id, render_id)
        if opp is None or row is None:
            return HTMLResponse("", status_code=404)
        settings = await load_settings_with_db(s)
        if not text:
            ctx = await _letter_ctx(s, settings, opp, row, tab="edit")
            return _fragment(request, "role_letter_draft", ctx,
                             {"k-toast": _toast(_EMPTY_LETTER, tone="warn")})
        from kestrel.docs.letters import word_count
        from kestrel.web.redact import confidential

        cfg = dict(row.config or {})
        # The text on screen has the current employer hidden in confidential mode, so it is
        # compared with the stored text as shown; saving it keeps the employer hidden.
        conf, mapping = await confidential(s)
        stored_now = redact_text(str(cfg.get("body_md") or ""), mapping, enabled=conf)
        generated = redact_text(str(cfg.get("generated_md") or ""), mapping, enabled=conf)
        if text != stored_now:
            cfg.update({"body_md": text, "words": word_count(text),
                        "edited": text != generated, "pdf_digest": None})
            row.config = cfg  # a new dict, so the JSON column notices the change
            stale_pdf = paths.stored_pdf(row.pdf_path)  # confined to the documents folder
            row.pdf_path = None
            await s.commit()
            _remove_pdf(stale_pdf)  # the old file no longer matches the text; it is not kept
        ctx = await _letter_ctx(s, settings, opp, row, tab=tab, saved=True)
    return _fragment(request, "role_letter_draft", ctx,
                     {"k-toast": _toast(_SAVED), "rl-refresh": True})


@router.post("/opportunities/{opp_id}/role/cover-letter/{render_id}/discard",
             response_class=HTMLResponse)
async def cover_letter_discard(opp_id: int, render_id: int):
    """Delete the draft (the sheet asks first). The saved PDF, if any, goes with it."""
    async with sessionmaker()() as s:
        row = await _owned_letter(s, opp_id, render_id)
        if row is None:
            return _answer({"k-toast": _toast("That draft is already gone.", tone="warn"),
                            "rl-refresh": True})
        pdf = paths.stored_pdf(row.pdf_path)  # never a file outside the documents folder
        for pursuit in (await s.execute(
            select(Pursuit).where(Pursuit.cover_letter_id == row.id)
        )).scalars():
            pursuit.cover_letter_id = None
        await s.flush()  # the pursuit lets go of the row before the row goes (foreign key)
        await s.delete(row)
        await s.commit()
    _remove_pdf(pdf)
    return _answer({"k-toast": _toast("Draft discarded."), "rl-letter-close": True,
                    "rl-refresh": True})


@router.get("/opportunities/{opp_id}/role/cover-letter/{render_id}/download")
async def cover_letter_download(opp_id: int, render_id: int):
    """The PDF of the saved text, made from the same HTML the preview shows. It is made again
    when the text changed since the last one. Download goes through the computer's Save dialog
    (D-UI-12)."""
    import hashlib

    from fastapi.responses import FileResponse, PlainTextResponse

    from kestrel.docs import render as docrender
    from kestrel.docs.letters import letter_html
    from kestrel.web.errors import fixed_reason
    from kestrel.web.redact import confidential

    async with sessionmaker()() as s:
        opp = await s.get(Opportunity, opp_id)
        row = await _owned_letter(s, opp_id, render_id)
        if opp is None or row is None:
            return PlainTextResponse("No such draft.", status_code=404)
        settings = await load_settings_with_db(s)
        cfg = dict(row.config or {})
        kind = cfg.get("kind") or row.template
        conf, mapping = await confidential(s)  # the PDF hides the current employer too
        body = redact_text(str(cfg.get("body_md") or ""), mapping, enabled=conf) or ""
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        path = paths.stored_pdf(row.pdf_path)  # a path outside the documents folder is missing
        if path is None or not path.is_file() or cfg.get("pdf_digest") != digest:
            found = await _letter_basis(s, settings, opp, cfg.get("basis"))
            header = found[2].header if found else {}
            try:
                made = await docrender.render_pdf(letter_html(header, body, kind))
            except Exception as exc:  # noqa: BLE001 (no browser on this machine: say so, never a 500)
                log.warning("letter_pdf_failed", render_id=render_id,
                            error_type=type(exc).__name__)
                return PlainTextResponse(
                    f"Could not make the PDF: {fixed_reason(exc)}", status_code=503)
            org = opp.org_name or None
            row.filename = docrender.filename_for(
                header, org, "EngagementOutline" if kind == "engagement_outline" else "CoverLetter")
            docs_dir = paths.documents_dir()
            docs_dir.mkdir(parents=True, exist_ok=True)
            path = docs_dir / f"{row.id}_{row.filename}"
            path.write_bytes(made.pdf)
            row.pdf_path, row.page_count = str(path), made.page_count
            row.config = {**cfg, "pdf_digest": digest}
            await s.commit()
        name = row.filename or path.name
    return FileResponse(path, media_type="application/pdf", filename=name)
