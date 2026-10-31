"""Server-rendered pages (SPEC-INTERFACES 2.1). The pipeline triage list (docs/UI_PROPOSAL.html
Screen 2: chips, striped rows, a side drawer), the opportunity detail, the always-visible override
control (a partial shared by both), and the runs progress view. HTMX swaps the list on
chip/search/sort/paginate and loads the drawer beside it; full navigation is only for the detail
page."""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm.exc import StaleDataError

from kestrel.date_display import format_display
from kestrel.db.enums import ARCHIVE_REASONS
from kestrel.pipeline import lane
from kestrel.services import tier0_override
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.urls import safe_web_url
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.fit import (
    FAILED_NOTE,
    FIT_WORDS,
    GROUP_FAILED,
    GROUP_LABELS,
    GROUP_NEW,
    GROUP_NOT_FIT,
    RuleWhy,
    explain_rule,
    fit_word_for_verdict,
    forced_words,
)
from kestrel.web.pipeline_view import row_view, signal_view
from kestrel.web.queries import (
    GROUP_ORDER,
    GROUP_ROWS,
    OppFilters,
    OppPage,
    any_opportunities,
    first_sentence,
    get_detail,
    list_opportunities,
    live_activity,
    row_fit,
)
from kestrel.web.redact import redact_name, redact_text, redaction_map
from kestrel.web.routes_api import filters_from_query
from kestrel.web.words import words

router = APIRouter()
log = structlog.get_logger()

_STAGES = ["new", "t0_pass", "t0_reject", "t1_pass", "t1_reject", "t2_done", "quarantined"]
_VERDICTS = ["apply", "pursue", "consider", "pass", "keep", "kill"]
_TRUE = ("1", "true", "on", "True")
# The sort toggle on the triage list, inside each fit group: Best fit and Newest, and the menu
# beside it (D-UI-96). Older sort keys (pre_score, ghost) still work from a URL.
_SORTS = [("score", "Best fit"), ("first_seen", "Newest")]
_SORT_MENU = [
    ("date", "Date posted"), ("first_seen", "First seen"), ("oldest", "Oldest first"),
    ("company", "Company A to Z"), ("pre_score", "Quick-screen score"),
    ("ghost", "Ghost posting risk"),
]
_SORT_NOTE = {
    "score": "best fit first", "first_seen": "newest first", "date": "newest posted first",
    "oldest": "oldest first", "company": "company A to Z", "pre_score": "quick-screen score first",
    "ghost": "highest ghost risk first",
}
# Query parameters that the chips, the search box, and the sort menu do NOT own but must carry
# along, so an older bookmarked URL keeps filtering while the operator clicks chips.
_CARRIED = (
    "score_min",
    "score_max",
    "org",
    "source",
    "ghost_min",
    "verdict",
    "page_size",
    "archived",
    "pay",
    "work",
    "posted",
    "warn",
    "live",
    "legit",
    "tab",
    "type",
)
# The parameters that mean "the operator chose a filter": with none of them (and no `view`), a
# track's default saved view applies.
_FILTER_PARAMS = frozenset(
    {"q", "stage", "needs_review", "shortlisted", "new_today", "hidden", "saved", "sort", "type"}
    | set(_CARRIED) - {"tab", "page_size"}
)
_GROUP_LIMIT_PARAM = {
    key: f"lim_{key}" for key in ("worth", "maybe", "applied", "new", "unscored", "failed")
}
_WORK = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def page_filters(params, request=None) -> OppFilters:
    """The list filters plus the chip-only and More filters ones, which the JSON API does not
    expose and so `filters_from_query` does not parse. With `request`, the track is the active
    tab's (D-UI-57): the page never lists both tracks at once."""
    f = filters_from_query(params)
    f.track = active_track(request) if request is not None else f.track
    f.shortlisted = params.get("shortlisted") in _TRUE
    f.new_today = params.get("new_today") in _TRUE
    f.saved = params.get("saved") in _TRUE
    f.pay = params.get("pay") if params.get("pay") in ("floor", "target") else None
    f.work_type = _WORK.get(params.get("work") or "")
    try:
        f.posted_days = int(params.get("posted")) if params.get("posted") else None
    except ValueError:
        f.posted_days = None
    f.warnings = params.get("warn") if params.get("warn") in ("no_ghost", "review") else None
    f.liveness = params.get("live") if params.get("live") in ("live", "closed") else None
    legit = params.get("legit")
    f.legitimacy = legit if legit in ("clean", "suspect", "reject") else None
    f.limits = {}
    for key, name in _GROUP_LIMIT_PARAM.items():
        try:
            n = int(params.get(name) or 0)
        except ValueError:
            n = 0
        if n > 0:
            f.limits[key] = min(n, 1000)
    # Consulting reads two tabs; Employment has no trigger events to separate.
    if f.track == "consulting":
        f.kind = "signals" if params.get("tab") == "signals" else "opportunities"
    f.grouped = True
    if not params.get("sort"):
        f.sort = "score"
    return f


def _join_or(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


def not_fit_note(page) -> str:
    """The Not a fit section's note over the WHOLE filter set (D-UI-62): "49 set aside by your
    rules before any model ran (location 31, title 18); 0 scored low by the model.". Counted
    in SQL across every page, never from this page's rows."""
    n = page.group_counts.get(GROUP_NOT_FIT, 0)
    if not n:
        return ""
    rules = page.rule_rejects
    split = ", ".join(f"{w} {c}" for w, c in page.not_fit_rules)
    by_rules = f"{rules} set aside by your rules before any model ran"
    if rules and split:
        by_rules += f" ({split})"
    return f"{by_rules}; {n - rules} scored low by the model."


def _groups(page, f: OppFilters) -> list[dict]:
    """The Pipeline's visible fit groups in order (Not a fit is its own section, D-UI-62), each
    with its label, its count over the WHOLE filter set and the rows shown so far (25 a group,
    D-UI-96). `next_limit` is what Show 25 more asks for; `more` is how many that reveals."""
    out = []
    for key in GROUP_ORDER:
        if key == GROUP_NOT_FIT:
            continue
        count = page.group_counts.get(key, 0)
        rows = [r for r in page.rows if r.fit and r.fit.group == key]
        if count or rows:
            shown = len(rows)
            out.append(
                {
                    "key": key,
                    "label": GROUP_LABELS[key],
                    "count": max(count, shown),
                    "rows": rows,
                    "explain": FAILED_NOTE if key == GROUP_FAILED else "",
                    "ids": page.failed_ids if key == GROUP_FAILED else [],
                    "more": min(GROUP_ROWS, max(0, count - shown)),
                    "limit_param": _GROUP_LIMIT_PARAM.get(key),
                    "next_limit": shown + GROUP_ROWS,
                }
            )
    return out


def _filters_active(f: OppFilters) -> bool:
    return any(
        (
            f.stage,
            f.verdict,
            f.q,
            f.org,
            f.source,
            f.needs_review,
            f.include_archived,
            f.hidden_only,
            f.shortlisted,
            f.new_today,
            f.saved,
            f.pay,
            f.work_type,
            f.posted_days,
            f.warnings,
            f.liveness,
            f.legitimacy,
            f.score_min is not None,
            f.score_max is not None,
            f.ghost_min is not None,
        )
    )


# "/" is the dashboard (kestrel.web.pages.dashboard); it is no longer a redirect to /opportunities.


# A tiny indigo square favicon (data-free SVG) so the browser's automatic request is not a 404.
_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="3" fill="#4F5BD5"/>'
    b'<circle cx="8" cy="8" r="4" fill="#fff"/></svg>'
)


@router.get("/favicon.ico")
async def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


async def _redact_rows(s, settings, rows) -> None:
    """Confidential mode: redact the operator's current employer in the list, not only toggle.
    The summary line is the model's own prose and names the employer routinely, so it is
    redacted with the same map as the org name and the title. Then each row gets the parts a
    template prints (`row.view`), computed from what the operator will actually read."""
    if settings.confidential_mode:
        mapping = await redaction_map(s)
        for row in rows:
            row.org_name = redact_name(row.org_name, mapping, enabled=True)
            row.opp.org_name = redact_name(row.opp.org_name, mapping, enabled=True)
            row.opp.title = redact_text(row.opp.title, mapping, enabled=True)
            row.summary = redact_text(row.summary, mapping, enabled=True)
    for row in rows:
        row.view = row_view(row)


async def _redact_signals(s, settings, rows) -> None:
    """The same for Consulting signal rows: the title and company, then what the row prints."""
    if settings.confidential_mode:
        mapping = await redaction_map(s)
        for row in rows:
            row.org_name = redact_name(row.org_name, mapping, enabled=True)
            row.opp.title = redact_text(row.opp.title, mapping, enabled=True)
    for row in rows:
        row.view = signal_view(row)


# Not a fit's rows come twelve at a time inside their own scroll box (D-UI-96).
NF_PAGE = 12


async def _pay_value(track: str | None, which: str | None) -> float | None:
    """The operator's pay floor or target for `track` (a year; a day for Consulting), read off
    the event loop (profile.yml is a file read and a parse). None when it is not set."""
    import asyncio

    from kestrel.pipeline.profile import load_profile

    if which not in ("floor", "target"):
        return None
    try:
        profile = await asyncio.to_thread(load_profile, track or "employment")
    except Exception as exc:  # a filter value never breaks the page
        log.warning("pay_filter_profile_failed", error=str(exc))
        return None
    if track == "consulting":
        terms, key = getattr(profile, "consulting_terms", None), f"day_rate_{which}"
    else:
        terms, key = getattr(profile, "employment_terms", None), f"comp_{which}_usd"
    value = getattr(terms, key, None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def _effective_params(s, request: Request, track: str) -> tuple[dict, list, dict | None]:
    """(the parameters the page filters by, the track's saved views, the view in use). A saved
    view's filters sit under whatever the operator then changed; with no filter in the URL at
    all, the track's default view applies (`view=none` turns that off, `view=<id>` picks one)."""
    from kestrel.services import saved_views

    params = dict(request.query_params)
    views = await saved_views.list_views(s, track)
    chosen = params.pop("view", None)
    view = None
    if chosen == "none":
        view = None
    elif chosen and chosen.isdigit():
        view = next((v for v in views if v.id == int(chosen)), None)
    elif not (set(params) & _FILTER_PARAMS):
        view = next((v for v in views if v.is_default), None)
    eff = {**saved_views.params_for(view), **params} if view else params
    return eff, views, ({"id": view.id, "name": view.name} if view else None)


@router.get("/opportunities", response_class=HTMLResponse)
async def opportunities(request: Request):
    from sqlalchemy import func, select

    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Source, Task
    from kestrel.queue.queue import in_flight
    from kestrel.services.tracks import switched_off_tracks
    from kestrel.web.queries import chip_counts, list_signals
    from kestrel.web.routes_control import run_active

    track = active_track(request)
    async with sessionmaker()() as s:
        eff, views, active_view = await _effective_params(s, request, track)
    f = page_filters(eff, request)
    f.pay_min = await _pay_value(f.track, f.pay)
    signals_tab = f.kind == "signals"
    async with sessionmaker()() as s:
        count_filters = replace(f, kind="opportunities") if f.track == "consulting" else f
        counts = await chip_counts(s, count_filters)
        n_signals = 0
        signal_rows: list = []
        if f.track == "consulting":
            n_signals = len(await list_signals(s, replace(f, hidden_only=False, q=None)))
        if signals_tab:
            signal_rows = await list_signals(s, f)
            page = OppPage()
        else:
            page = await list_opportunities(s, f)
        settings = await load_settings_with_db(s)
        await _redact_rows(s, settings, page.rows)
        await _redact_signals(s, settings, signal_rows)
        n_not_fit = page.group_counts.get(GROUP_NOT_FIT, 0)
        # Only Not a fit to show: its first page opens in place (D-UI-62).
        nf_page = None
        if n_not_fit and not page.total:
            nf_page = await list_opportunities(
                s, replace(f, section="not_fit", page=1, page_size=NF_PAGE)
            )
            await _redact_rows(s, settings, nf_page.rows)
        # Only when the list is empty: is that "nothing matches" or "nothing ingested yet"?
        has_any = True
        if not (page.total or n_not_fit or signal_rows):
            has_any = await any_opportunities(s, f.track)
        running = await run_active(s)
        # Roles waiting in the background lane explain themselves under Being scored (#34): how
        # many, and whether local scoring is available for the legacy backlog.
        being_scored = page.group_counts.get(GROUP_NEW, 0)
        background = await lane.waiting_count(s, f.track) if being_scored else 0
        if f.track and f.track in set(switched_off_tracks()):
            background = 0  # a switched-off track's rows say "held"; the lane note would mislead
        # Refresh while ANY live work exists (queue.in_flight), not only a scrape or t1/t2 run:
        # embed, Tier 3 and Score again rows also change state (#34 review).
        polling = running or bool(
            (await s.execute(select(Task.id).where(in_flight(utcnow_iso())).limit(1))).first()
        )
        background_hosted = bool(background) and lane.scores_on_hosted(settings)
        source_kinds = list(
            (
                await s.execute(
                    select(Source.kind)
                    .where(Source.track == f.track)
                    .group_by(Source.kind)
                    .order_by(func.lower(Source.kind))
                )
            ).scalars()
        )
    query = {k: v for k, v in eff.items() if k != "open"}
    try:
        open_id = int(request.query_params.get("open") or 0) or None
    except ValueError:
        open_id = None
    ctx = {
        "page": page,
        "groups": _groups(page, f),
        "not_fit": {"count": n_not_fit, "note": not_fit_note(page), "page": nf_page},
        "running": running,
        "polling": polling,
        "f": f,
        "track": f.track,
        "signals_tab": signals_tab,
        "signals": signal_rows,
        "n_signals": n_signals,
        "counts": counts,
        "sorts": _SORTS,
        "sort_menu": _SORT_MENU,
        "sort_note": _SORT_NOTE.get(f.sort, "newest first"),
        "views": views,
        "active_view": active_view,
        "source_kinds": source_kinds,
        "query": query,
        "confidential": settings.confidential_mode,
        "has_any": has_any,
        "filters_active": _filters_active(f),
        "carried": {k: query[k] for k in _CARRIED if query.get(k)},
        "archive_reasons": ARCHIVE_REASONS,
        # The header's "N open · M still being scored" (D-UI-51) and the Score again selection.
        "being_scored": being_scored,
        "background": background,
        "background_hosted": background_hosted,
        "scoring_failed": counts["failed"],
        "total_open": sum(page.group_counts.values()),
        "page_ids": [r.opp.id for r in page.rows],
        "open_id": open_id,
        "panel_html": "",
    }
    if open_id:
        from kestrel.web.pages.pipeline import render_panel

        ctx["panel_html"] = await render_panel(request, open_id)
    # HTMX chip/search/sort/paginate requests swap only the list; a full load renders the page.
    tmpl = "partials/opp_rows.html" if request.headers.get("HX-Request") else "opportunities.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.get("/opportunities/not-fit", response_class=HTMLResponse)
async def opportunities_not_fit(request: Request):
    """The Not a fit section's rows (D-UI-62), loaded by its Show button, twelve at a time. Load
    more asks for the next page and the rows are appended inside the scroll box (`nf_page`),
    under the same filters as the list above."""
    track = active_track(request)
    async with sessionmaker()() as s:
        eff, _views, _active = await _effective_params(s, request, track)
    f = page_filters(eff, request)
    f.pay_min = await _pay_value(f.track, f.pay)
    try:
        nf = max(1, int(request.query_params.get("nf_page") or 1))
    except ValueError:
        nf = 1
    async with sessionmaker()() as s:
        page = await list_opportunities(
            s, replace(f, section="not_fit", page=nf, page_size=NF_PAGE)
        )
        settings = await load_settings_with_db(s)
        await _redact_rows(s, settings, page.rows)
    ctx = {
        "nf": page, "notfit_only": True, "append": nf > 1, "f": f,
        "query": {k: v for k, v in eff.items() if k not in ("open", "nf_page")},
        "archive_reasons": ARCHIVE_REASONS,
    }
    return templates().TemplateResponse(request, "partials/opp_rows.html", ctx)


# Legitimacy signals (PROMPTS.md P1 and P2 enums) as the operator would say them.
_WATCH_WORDS = {
    "no_company_detail": "Says little about the company.",
    "no_comp_no_location_no_team": "No pay, location or team named.",
    "generic_boilerplate": "Mostly boilerplate.",
    "staffing_agency_unnamed_client": "An agency posting; the client is not named.",
    "agency_posting": "An agency posting.",
    "unlimited_earning_language": "Promises unlimited earnings.",
    "personal_email_contact": "The contact is a personal email address.",
    "upfront_cost_or_purchase": "Asks you to pay or buy something up front.",
    "mlm_pattern": "Reads like multi-level marketing.",
    "scam_signals": "Shows signs of a scam.",
    "reposted_repeatedly": "Reposted many times.",
    "unrealistic_requirements_for_comp": "Asks a lot for the pay offered.",
    "commission_only": "Commission only.",
    "pipeline_building_not_real_opening": "May be collecting resumes, not a real opening.",
    "description_too_vague": "The description is vague.",
}


# Under every model-written summary and every "may not be a real opening" flag (D-UI-53).
AI_DISCLAIMER = (
    "AI overviews are the model's reading of the posting and can be wrong, including about the "
    "company. Open the posting and research the company before you act on it."
)


def contradicted(key: str, opp: Any) -> bool:
    """A model's warning sign that a fact Kestrel holds about the posting rules out (owner round
    14: "No pay, location or team named" on a posting located in North Carolina). "Agency posting"
    is left alone: staffing agencies run their own Greenhouse, Lever and Workday boards, so the
    board says nothing (round 14 review). The stored evaluation keeps the model's words."""
    if opp is None:
        return False
    if key == "no_comp_no_location_no_team":
        loc = str(getattr(opp, "location_raw", "") or "").strip()
        has_comp = getattr(opp, "comp_min", None) is not None or getattr(
            opp, "comp_max", None) is not None
        return bool(loc) or has_comp
    return False


def watch_lines(
    legitimacy: dict | None, ghost_score: float | None = None, opp: Any = None
) -> list[str]:
    """The drawer's Watch line: the legitimacy flags in plain words, plus a likely ghost. A flag
    that a known fact about the posting rules out is left out and logged (contradicted)."""
    out: list[str] = []
    for sig in (legitimacy or {}).get("signals") or []:
        if isinstance(sig, dict):
            key = str(sig.get("kind") or "")
            text = sig.get("detail") or _WATCH_WORDS.get(key) or key.replace("_", " ")
        else:
            key = str(sig)
            text = _WATCH_WORDS.get(key) or key.replace("_", " ").capitalize() + "."
        if contradicted(key, opp):
            log.info("watch_signal_contradicted", signal=key, opp=getattr(opp, "id", None))
            continue
        if text and text not in out:
            out.append(text)
    if ghost_score is not None and ghost_score >= 0.6:
        out.append("May be a ghost posting.")
    return out


def _rule_reason(opp, override: dict | None) -> str | None:
    reason = opp.stage_reason if opp.stage == "t0_reject" else None
    if reason is None and override:
        reason = override.get("reason")
    return reason or None


async def load_rule_profile(detail: dict) -> None:
    """Put the role's track profile on `detail["rule_profile"]` for rule_context, read in a
    thread (profile.yml is a file read and a parse: never on uvicorn's loop). Only when a rule's
    sentence will be shown. A failure leaves None: the sentence falls back to its generic form."""
    import asyncio

    from kestrel.pipeline.profile import load_profile

    opp = detail.get("opp")
    if opp is None or not _rule_reason(opp, detail.get("tier0_override")):
        return
    try:
        detail["rule_profile"] = await asyncio.to_thread(load_profile, opp.track)
    except Exception as exc:  # a display sentence never breaks the page
        log.warning("rule_why_profile_failed", error=str(exc))
        detail["rule_profile"] = None


def rule_context(detail: dict) -> tuple[RuleWhy | None, str | None]:
    """(why a Tier 0 rule set the role aside, the "You asked to score this despite ..." note)
    for the decision panel and the Evaluation tab (D-UI-73, D-UI-74). The detail is computed now
    from the role and the operator's current profile; Tier 0 stores only the rule id."""
    from kestrel.config import get_settings

    opp = detail["opp"]
    override = detail.get("tier0_override")
    reason = _rule_reason(opp, override)
    if not reason:
        return None, None
    # Read off the event loop by load_rule_profile; None (the generic sentence) when not loaded.
    profile = detail.get("rule_profile")
    why = explain_rule(reason, opp=opp, profile=profile,
                       settings=detail.get("settings") or get_settings(), org=detail.get("org"))
    return (why if opp.stage == "t0_reject" else None), (why.override_note if override else None)


def decision_extras(detail: dict) -> dict:
    """What partials/opp_decision.html needs beyond get_detail: the fit word, the Fit line's two
    opinions when the model and the scoring rules differ, the one-line Why, the Watch lines, the
    operator's own word, and the tailor link that offers the way back to this role."""
    opp, ev, override = detail["opp"], detail["ev"], detail.get("override")
    fit = row_fit(
        opp, ev,
        pursued=detail.get("pursuit") is not None and detail["pursuit"].closed_at is None,
        pursuit_status=detail["pursuit"].status if detail.get("pursuit") is not None else None,
        operator_verdict=override.operator_verdict if override is not None else None,
    )
    model_word = rules_word = None
    if ev is not None and (
        ev.needs_review or (ev.verdict_model and ev.verdict_model != ev.verdict)
    ):
        model_word = fit_word_for_verdict(ev.verdict_model, ev.score)
        rules_word = fit_word_for_verdict(ev.verdict, ev.weighted_score)
        if model_word == rules_word:
            model_word = rules_word = None
    why = first_sentence(row_summary_from_json(detail.get("ev_json")))
    legit = (detail.get("ev_json") or {}).get("legitimacy")
    mine = None
    if override is not None:
        mine = fit_word_for_verdict(override.operator_verdict, override.operator_score)
    rule_why, override_note = rule_context(detail)
    master = detail.get("tailor_master")
    tailor_href = None
    if master is not None:
        tailor_href = (
            f"/renders/new?master_id={master.id}&opportunity_id={opp.id}"
            f"&from=/opportunities/{opp.id}"
        )
    return {
        "fit": fit,
        "model_word": model_word,
        "rules_word": rules_word,
        "why": why,
        "watch": watch_lines(legit, opp.ghost_score, opp),
        # The legitimacy check doubted the posting (P1/P2 "suspect" or "reject").
        "legit_flag": isinstance(legit, dict) and legit.get("status") in ("suspect", "reject"),
        "ai_disclaimer": AI_DISCLAIMER,
        "mine": mine,
        "fit_words": FIT_WORDS,
        "tailor_href": tailor_href,
        "tailor_label": words(opp.track)["tailor"],
        "archive_reasons": ARCHIVE_REASONS,
        "forced_words": forced_words,
        "rule_why": rule_why,
        "override_note": override_note,
    }


async def decision_context(s, opp_id: int) -> dict | None:
    """The full context for re-rendering partials/opp_decision.html after an action: the detail
    page's (redacted) context, which already carries decision_extras."""
    return await _detail_context(s, opp_id)


def row_summary_from_json(ev_json: dict | None) -> str | None:
    """The drawer's block A text from get_detail's redacted copy (never the raw ORM field)."""
    from kestrel.analytics.today import summary_line

    return summary_line((ev_json or {}).get("blocks")) if ev_json else None


@router.get("/opportunities/{opp_id}/expand", response_class=HTMLResponse)
async def opportunity_expand(request: Request, opp_id: int):
    """The pre-drawer inline expansion. Kept for API/extension callers and its redaction test;
    the triage list itself loads /drawer."""
    async with sessionmaker()() as s:
        detail = await get_detail(s, opp_id)
    if detail is None:
        return HTMLResponse("<tr><td>not found</td></tr>", status_code=404)
    await load_rule_profile(detail)
    detail.update(decision_extras(detail))
    return templates().TemplateResponse(request, "partials/row_expand.html", detail)


# --------------------------------------------------------------------- opportunity detail
# The Role page (kestrel.web.pages.role) builds on this context. Every org mention goes through
# the redacted copies built in _detail_context.

_TABS = ("evaluation", "posting", "organization", "documents", "history")


def _money(value: float, currency: str | None) -> str:
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get((currency or "USD").upper(), "")
    if value >= 10_000:
        return f"{sym}{value / 1000:.0f}k"
    return f"{sym}{value:,.0f}"


def _comp_line(opp) -> str | None:
    """'$230k to $260k' (a year is the default) or '$2.2k a day'; None when nothing was parsed."""
    lo, hi = opp.comp_min, opp.comp_max
    if lo is None and hi is None:
        return None
    units = {"day": " a day", "hour": " an hour", "month": " a month"}
    unit = units.get(opp.comp_interval or "", "")
    if lo is not None and hi is not None and lo != hi:
        return f"{_money(lo, opp.comp_currency)} to {_money(hi, opp.comp_currency)}{unit}"
    return f"{_money(hi if hi is not None else lo, opp.comp_currency)}{unit}"


def _pick_master(masters: list, track: str):
    """The document to tailor for this track: the resume for employment, the capability
    statement for consulting, else the first active master of the track."""
    want = "resume" if track == "employment" else "capability_statement"
    for m in masters:
        if m.doc_kind == want:
            return m
    return masters[0] if masters else None


def _cached_prompt_price(model: str) -> float | None:
    """USD per million prompt tokens for `model` from a provider catalog already held in this
    process (kestrel.llm.providers.list_models fills one when Settings > Hosted lists models).
    Never a network call: a page load must not reach the provider. None when nothing is cached."""
    from kestrel.llm import providers

    cache = getattr(providers, "MODEL_CATALOG_CACHE", None)
    if not isinstance(cache, dict):
        return None
    for models in cache.values():
        for info in models or []:
            if getattr(info, "id", None) == model and info.prompt_usd_per_m is not None:
                return float(info.prompt_usd_per_m)
    return None


def _cost_line(model: str, tokens: int) -> str:
    price = _cached_prompt_price(model)
    if price is None:
        return "cost depends on the model's price"
    usd = tokens / 1_000_000 * price
    return "under $0.01" if usd < 0.01 else f"about ${usd:.2f}"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _employer_tokens(mapping: dict[str, str], domain: str | None) -> set[str]:
    """The squashed forms of the employer's names and its domain's main label, for matching
    inside URLs: a board slug ("northwind-freight"), a careers host ("northwindfreight.com") and
    a bare mention all collapse to the same letters. Short tokens are left out (false matches)."""
    tokens: set[str] = set()
    for e in getattr(mapping, "employers", []):
        for literal in e.hide():
            sq = _squash(literal)
            if len(sq) >= 4:
                tokens.add(sq)
    if domain:
        labels = [lab for lab in domain.lower().split(".") if lab and lab != "www"]
        if labels:
            sq = _squash(max(labels, key=len))
            if len(sq) >= 4:
                tokens.add(sq)
    return tokens


def _url_reveals(url: str | None, tokens: set[str]) -> bool:
    """Whether a URL names the employer anywhere (host or path), by squashed substring."""
    if not url or not tokens:
        return False
    sq = _squash(url)
    return any(t in sq for t in tokens)


def _link(
    url: str | None, text: str | None, conf: bool, mapping: dict, tokens: set[str], placeholder: str
) -> dict | None:
    """{"text", "href"} for an external link in a redacted tab: the text is redacted; a URL that
    reveals the employer keeps neither its text nor its href (the status bar shows an href) and
    shows the placeholder word instead. None when there is no URL."""
    url = safe_web_url(url)
    if not url:
        return None
    if conf and _url_reveals(url, tokens):
        return {"text": placeholder, "href": None}
    return {"text": redact_text(text or url, mapping, enabled=conf), "href": url}


async def _detail_context(s, opp_id: int) -> dict | None:
    """The shared context for the detail page and its tab partials, with confidential-mode
    redaction applied once, here, to every org mention: the org row and opp.org_name (display-only
    edits on an uncommitted session), the title and description, and the redacted ev_json copy
    that get_detail already builds."""
    from kestrel.web.pages.opportunity_actions import action_context
    from kestrel.web.redact import confidential

    detail = await get_detail(s, opp_id)
    if detail is None:
        return None
    settings = await load_settings_with_db(s)
    conf, mapping = await confidential(s)
    opp, org, source = detail["opp"], detail["org"], detail["source"]
    org_domain = org.domain if org is not None else None
    if conf:
        if org is not None:
            redacted = redact_name(org.name, mapping, enabled=True)
            if redacted == org.name:
                org_domain = None  # not the employer: its domain is not a secret
            org.name = redacted
        opp.org_name = redact_name(opp.org_name, mapping, enabled=True)
    org_name = org.name if org is not None else opp.org_name
    # URLs, the source name and the dedup members name the employer as readily as the prose
    # (a careers host, a board slug, a "<employer> careers" source), so the tabs render these
    # display copies, never the raw fields, in confidential mode.
    tokens = _employer_tokens(mapping, org_domain) if conf else set()
    org_url = (org.url or (f"https://{org.domain}" if org.domain else None)) if org else None
    org_site = _link(
        org_url, (org.domain if org else None), conf, mapping, tokens, "website redacted"
    )
    posting_link = _link(opp.url, None, conf, mapping, tokens, "source posting")
    source_name = redact_text(source.name, mapping, enabled=conf) if source is not None else None
    dedup_rows = [
        {
            "id": m.id,
            "title": redact_text(m.title, mapping, enabled=conf) or m.id,
            "href": None if conf and _url_reveals(m.url, tokens) else safe_web_url(m.url),
        }
        for m in detail["dedup_members"]
    ]
    # The actions partial needs `pursuit` and `masters` (see action_context's docstring on why it
    # returns neither opp nor org: merging those would undo the redaction above).
    detail.update(await action_context(s, opp_id, opp=opp))
    detail["tier0_override"] = await tier0_override.get(s, opp_id)  # Score anyway (D-UI-74)
    detail.update(
        {
            "settings": settings,
            "confidential": conf,
            "mapping": mapping,
            "org_name": org_name,
            "org_site": org_site,
            "posting_link": posting_link,
            "source_name": source_name,
            "dedup_rows": dedup_rows,
            "title_display": redact_text(opp.title, mapping, enabled=conf) or "(untitled)",
            "description_display": redact_text(opp.description_md, mapping, enabled=conf),
            "tailor_master": _pick_master(detail["masters"], opp.track),
            "tabs": _TABS,
        }
    )
    await load_rule_profile(detail)
    detail.update(decision_extras(detail))
    return detail


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
async def opportunity_detail(request: Request, opp_id: int):
    """The Role page (kestrel.web.pages.role): Evaluation is the page itself; the other four tabs
    have their own URLs (/opportunities/{id}/posting and so on)."""
    from kestrel.web.pages.role import render_page

    return await render_page(request, opp_id, "evaluation")


async def _questions_view(s, opp_id: int, detail: dict) -> dict:
    """The Likely questions card's data, redacted like every other tab in confidential mode."""
    from kestrel.web.pages.prep import redacted_question_view

    return await redacted_question_view(s, opp_id)


@router.post("/opportunities/{opp_id}/questions/regenerate", response_class=HTMLResponse)
async def regenerate_questions(request: Request, opp_id: int):
    """Queue a fresh question list for a role on the list. The old list stays until the new one
    validates. Never a 500: a failure is logged and the card says so."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.models import Pursuit
    from kestrel.interview.question_task import is_applied, request_interview_questions
    from kestrel.services.online_consent import consent_payload, consent_problem

    note: str | None = None
    form = await request.form()
    async with sessionmaker()() as s:
        try:
            from kestrel.web.pages.prep import questions_route

            route = await questions_route(s)
            pursuit = (
                await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            ).scalar_one_or_none()
            if pursuit is None:
                note = "Add this role to your list first."
            elif not await is_applied(s, opp_id):
                note = "The list is written when you mark this role Applied."
            elif problem := consent_problem(route, form):
                note = problem  # an online Tier 2 sends the role: ask first, checked here
            else:
                queued = await request_interview_questions(
                    s, opp_id, pursuit.id, regenerate=True, consent=consent_payload(route)
                )
                await s.commit()
                note = (
                    "Queued. The new list replaces this one when it is ready."
                    if queued else "A list is already being written for this role."
                )
        except SQLAlchemyError as exc:
            log.warning("questions_regenerate_failed", opp_id=opp_id, error_type=type(exc).__name__)
            await s.rollback()
            note = "Could not queue that. Try again."
        detail = await _detail_context(s, opp_id)
        if detail is None:
            return HTMLResponse("Not found", status_code=404)
        qview = await _questions_view(s, opp_id, detail)
    # The Interview prep page asks for its own, smaller strip (`?view=prep`); the role page keeps
    # the card.
    tmpl = (
        "partials/pp_qstate.html" if request.query_params.get("view") == "prep"
        else "partials/opp_questions.html"
    )
    return templates().TemplateResponse(
        request, tmpl, {"qview": qview, "qnote": note, "opp_id": opp_id},
    )


@router.get("/opportunities/{opp_id}/tab/{name}", response_class=HTMLResponse)
async def opportunity_tab(request: Request, opp_id: int, name: str):
    """One tab of the Role page as a partial (partials/role_tab_<name>.html), which the tab
    links load in place."""
    from kestrel.web.pages.role import render_tab

    return await render_tab(request, opp_id, name)


async def _mode_context(s) -> dict:
    """What the status strip and the Runs page say about the run mode: the mode the worker is on
    (the stored setting until the serve loop's first cycle), a queued one-shot request, and why
    "hosted" is unavailable, if it is."""
    from kestrel.config import hosted_mode_problem
    from kestrel.services.setting_overrides import peek_run_mode
    from kestrel.web.app import current_run_mode

    settings = await load_settings_with_db(s)
    return {
        "mode": current_run_mode() or settings.llm.mode,
        "settings_mode": settings.llm.mode,
        "pending_mode": await peek_run_mode(s),
        "hosted_problem": hosted_mode_problem(settings),
        **(await _schedule_context(s)),
    }


async def _schedule_context(s) -> dict:
    """The Runs page Schedule panel (SPEC.md 6.5): the stored schedule, when it fires next on
    the operator's clock, and the weekday names for the form."""
    from kestrel.services import run_schedule as rs

    sched, last = await rs.load(s)
    now_local = rs.local_now()
    nxt = rs.next_fire(sched, now_local, last)
    return {
        "schedule": sched,
        "schedule_next": format_display(nxt, "weekday_iso", kind="datetime", time_style="24h")
        if nxt else None,
        "schedule_last": last,
        "weekdays": rs.WEEKDAYS,
        "local_tz": now_local.tzname() or "local time",
    }


@router.post("/runs/schedule", response_class=HTMLResponse)
async def runs_schedule(request: Request):
    """Write the run schedule from the Runs page form (off, daily at a time, weekly on a weekday
    at a time). Malformed input reads as off rather than erroring; the panel re-renders with
    what was stored and the next firing."""
    from kestrel.services import run_schedule as rs

    form = await request.form()
    sched = rs.parse(
        {
            "kind": str(form.get("kind") or "off"),
            "time": str(form.get("time") or "07:00"),
            "weekday": str(form.get("weekday") or "0"),
        }
    )
    async with sessionmaker()() as s:
        await rs.save(s, sched)
        await s.commit()
        from kestrel.web.pages.runs_page import card_context

        ctx = await card_context(s)
    ctx["schedule_saved"] = True
    return templates().TemplateResponse(request, "partials/run_schedule.html", ctx)


async def _keep_awake_context(s) -> dict:
    from kestrel.services import keep_awake as ka

    return {"keep_awake": await ka.load(s)}


@router.post("/runs/keep-awake", response_class=HTMLResponse)
async def runs_keep_awake(request: Request):
    """The Runs page toggle: hold off system sleep while Kestrel runs (D-RUNS-2). Stores the
    choice, applies it on this thread (uvicorn's loop thread, the one that holds the state) and
    re-renders the panel saying whether it took."""
    from kestrel.services import keep_awake as ka

    form = await request.form()
    on = str(form.get("on") or "") == "on"
    async with sessionmaker()() as s:
        await ka.save(s, on)
        await s.commit()
    problem = ka.apply(on)
    ctx = {"keep_awake": on, "keep_awake_saved": True, "keep_awake_problem": problem}
    return templates().TemplateResponse(request, "partials/keep_awake.html", ctx)


@router.get("/runs/strip", response_class=HTMLResponse)
async def runs_strip(request: Request):
    async with sessionmaker()() as s:
        activity = await live_activity(s)
        ctx = {"activity": activity, **(await _mode_context(s))}
    return templates().TemplateResponse(request, "partials/run_strip.html", ctx)


@router.post("/runs/now", response_class=HTMLResponse)
async def runs_now(request: Request):
    """The Runs page "Run now" form: queue the next worker cycle on local or hosted models (the
    same one-shot row as POST /api/run/now). Returns the message partial; a refused "hosted"
    says what is missing."""
    from kestrel.config import MODES, HostedModeError
    from kestrel.llm import credit
    from kestrel.services.online_consent import online_run_refusal
    from kestrel.services.setting_overrides import (
        HOSTED_CHANGED,
        hosted_run_problem,
        queue_run_mode,
    )

    credit.clear()  # the operator asked for a run: let sends to an online key resume (D-LLM-8)
    form = await request.form()
    mode = str(form.get("mode") or "")
    async with sessionmaker()() as s:
        if mode not in MODES:
            mode = (await load_settings_with_db(s)).llm.mode
        if mode in ("hosted", "both") and str(form.get("confirmed") or "") != "1":
            return HTMLResponse(
                '<span class="warnbox">Nothing was sent. Please confirm the online send '
                'first.</span>',
                status_code=400,
            )
        try:
            settings = await load_settings_with_db(s, mode=mode)
        except HostedModeError:
            # The reason is read again from the settings, not taken from the exception's text
            # (D-SEC-4); if they changed meanwhile it is the fixed sentence.
            problem = await hosted_run_problem(s, mode) or HOSTED_CHANGED
            return HTMLResponse(
                f'<span class="warnbox">Not queued: {html.escape(problem)}.</span>',
                status_code=400,
            )
        refusal = await online_run_refusal(
            s, settings, mode=mode, confirmed=form.get("confirmed"),
            approval=form.get("online_approval")
        )
        if refusal is not None:
            return HTMLResponse(
                f'<span class="warnbox">{html.escape(refusal[0])}</span>', status_code=refusal[1]
            )
        try:
            await queue_run_mode(s, mode, online_approval=form.get("online_approval"))
            await s.commit()
        except HostedModeError:  # the settings changed since they were loaded above
            return HTMLResponse(
                f'<span class="warnbox">Not queued: {html.escape(HOSTED_CHANGED)}.</span>'
            )
        except StaleDataError:  # the loop consumed the row mid-request: ask again, never a 500
            return HTMLResponse(
                '<span class="warnbox">Not queued: the queue changed while queuing; try '
                "again.</span>",
                status_code=409,
            )
    return HTMLResponse(
        f"Queued: the next worker cycle runs on <b>{mode}</b> models. Later cycles return to "
        "the Settings choice."
    )
