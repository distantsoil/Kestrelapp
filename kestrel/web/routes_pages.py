"""Server-rendered pages (SPEC-INTERFACES 2.1). The pipeline triage list (docs/UI_PROPOSAL.html
Screen 2: chips, striped rows, a side drawer), the opportunity detail, the always-visible override
control (a partial shared by both), and the runs progress view. HTMX swaps the list on
chip/search/sort/paginate and loads the drawer beside it; full navigation is only for the detail
page."""

from __future__ import annotations

import html
import re
from dataclasses import replace

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm.exc import StaleDataError

from kestrel.db.enums import ARCHIVE_REASONS
from kestrel.pipeline import lane
from kestrel.services import tier0_override
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import plain_error
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
from kestrel.web.queries import (
    GROUP_ORDER,
    OppFilters,
    any_opportunities,
    first_sentence,
    get_detail,
    list_opportunities,
    list_runs,
    live_activity,
    opportunity_history,
    opportunity_renders,
    org_context,
    quarantined_count,
    recent_scans,
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
# The sort toggle on the triage list, inside each fit group: Newest is the default, Best fit is
# one click (D-UI-45). Older sort keys (date, pre_score, ghost) still work from a URL.
_SORTS = [("first_seen", "Newest"), ("score", "Best fit")]
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
)


def page_filters(params, request=None) -> OppFilters:
    """The list filters plus the two chip-only ones (`shortlisted`, `new_today`), which the JSON
    API does not expose and so `filters_from_query` does not parse. With `request`, the track is
    the active tab's (D-UI-57): the page never lists both tracks at once."""
    f = filters_from_query(params)
    f.track = active_track(request) if request is not None else f.track
    f.shortlisted = params.get("shortlisted") in _TRUE
    f.new_today = params.get("new_today") in _TRUE
    f.grouped = True
    if not params.get("sort"):
        f.sort = "first_seen"
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


def _groups(page) -> list[dict]:
    """The Pipeline's visible fit groups in order (Not a fit is its own section, D-UI-62), each
    with its label, its count over the WHOLE filter set and this page's rows. Every group that
    has rows anywhere renders its header, even when its rows sit on another page, so a Being
    scored row is never invisible behind paging; such a header says which page its rows start
    on (rows arrive in GROUP_ORDER, so that is arithmetic over the counts of the groups ahead
    of it)."""
    out = []
    before = 0
    size = max(1, page.page_size)
    for key in GROUP_ORDER:
        if key == GROUP_NOT_FIT:
            continue
        count = page.group_counts.get(key, 0)
        rows = [r for r in page.rows if r.fit and r.fit.group == key]
        if count or rows:
            out.append(
                {
                    "key": key,
                    "label": GROUP_LABELS[key],
                    "count": max(count, len(rows)),
                    "rows": rows,
                    "explain": FAILED_NOTE if key == GROUP_FAILED else "",
                    "ids": page.failed_ids if key == GROUP_FAILED else [],
                    "first_page": before // size + 1,
                }
            )
        before += count
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
    redacted with the same map as the org name and the title."""
    if not settings.confidential_mode:
        return
    mapping = await redaction_map(s)
    for row in rows:
        row.org_name = redact_name(row.org_name, mapping, enabled=True)
        row.opp.org_name = redact_name(row.opp.org_name, mapping, enabled=True)
        row.opp.title = redact_text(row.opp.title, mapping, enabled=True)
        row.summary = redact_text(row.summary, mapping, enabled=True)


@router.get("/opportunities", response_class=HTMLResponse)
async def opportunities(request: Request):
    from sqlalchemy import select

    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Task
    from kestrel.queue.queue import in_flight
    from kestrel.services.tracks import switched_off_tracks
    from kestrel.web.routes_control import run_active

    f = page_filters(request.query_params, request)
    async with sessionmaker()() as s:
        page = await list_opportunities(s, f)
        settings = await load_settings_with_db(s)
        await _redact_rows(s, settings, page.rows)
        n_not_fit = page.group_counts.get(GROUP_NOT_FIT, 0)
        # Only Not a fit to show: its first page opens in place (D-UI-62).
        nf_page = None
        if n_not_fit and not page.total:
            nf_page = await list_opportunities(s, replace(f, section="not_fit", page=1))
            await _redact_rows(s, settings, nf_page.rows)
        quarantined = await quarantined_count(s, f.track)
        # Only when the list is empty: is that "nothing matches" or "nothing ingested yet"?
        has_any = True if (page.total or n_not_fit) else await any_opportunities(s, f.track)
        running = await run_active(s)
        # Roles waiting in the background lane explain themselves under Being scored (#34): how
        # many, and whether they start on their own or only on Score everything now.
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
    query = dict(request.query_params)
    groups = _groups(page)
    ctx = {
        "page": page,
        "groups": groups,
        "not_fit": {"count": n_not_fit, "note": not_fit_note(page), "page": nf_page},
        "running": running,
        "polling": polling,
        "f": f,
        "stages": _STAGES,
        "verdicts": _VERDICTS,
        "sorts": _SORTS,
        "query": query,
        "confidential": settings.confidential_mode,
        "quarantined": quarantined,
        "has_any": has_any,
        "filters_active": _filters_active(f),
        "carried": {k: query[k] for k in _CARRIED if query.get(k)},
        "archive_reasons": ARCHIVE_REASONS,
        # The header's "N open · M still being scored" (D-UI-51) and the Score again selection.
        "being_scored": being_scored,
        "background": background,
        "background_hosted": background_hosted,
        "scoring_failed": page.group_counts.get(GROUP_FAILED, 0),
        "page_ids": [r.opp.id for r in page.rows],
    }
    # HTMX chip/search/sort/paginate requests swap only the list; a full load renders the page.
    tmpl = "partials/opp_rows.html" if request.headers.get("HX-Request") else "opportunities.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.get("/opportunities/not-fit", response_class=HTMLResponse)
async def opportunities_not_fit(request: Request):
    """The Not a fit section's rows (D-UI-62), loaded by its Show button, with their own paging
    (`nf_page`) under the same filters as the list above."""
    f = page_filters(request.query_params, request)
    try:
        nf = max(1, int(request.query_params.get("nf_page") or 1))
    except ValueError:
        nf = 1
    async with sessionmaker()() as s:
        page = await list_opportunities(s, replace(f, section="not_fit", page=nf))
        settings = await load_settings_with_db(s)
        await _redact_rows(s, settings, page.rows)
    ctx = {
        "nf": page, "notfit_only": True, "f": f, "query": dict(request.query_params),
        "archive_reasons": ARCHIVE_REASONS,
    }
    return templates().TemplateResponse(request, "partials/opp_rows.html", ctx)


@router.get("/opportunities/{opp_id}/drawer", response_class=HTMLResponse)
async def opportunity_drawer(request: Request, opp_id: int):
    """The triage drawer beside the list: the fit word, the Fit / Why / Watch card, the two
    primary actions, the Disagree control and, once the role is on the list, the follow-ups
    (partials/opp_decision.html, shared with the detail page). Nothing here writes."""
    async with sessionmaker()() as s:
        detail = await _detail_context(s, opp_id)
        if detail is None:
            return HTMLResponse('<div class="card">Not found.</div>', status_code=404)
    detail["view"] = "drawer"
    return templates().TemplateResponse(request, "partials/opp_drawer.html", detail)


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


def watch_lines(legitimacy: dict | None, ghost_score: float | None = None) -> list[str]:
    """The drawer's Watch line: the legitimacy flags in plain words, plus a likely ghost."""
    out: list[str] = []
    for sig in (legitimacy or {}).get("signals") or []:
        if isinstance(sig, dict):
            key = str(sig.get("kind") or "")
            text = sig.get("detail") or _WATCH_WORDS.get(key) or key.replace("_", " ")
        else:
            key = str(sig)
            text = _WATCH_WORDS.get(key) or key.replace("_", " ").capitalize() + "."
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
        "watch": watch_lines(legit, opp.ghost_score),
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
# docs/UI_PROPOSAL.html Screen 3: the decision at the top (verdict pill, three actions), a one-line
# fact row, five HTMX-loaded tabs, and a right column with Your call, Documents and the Tier 3
# card. Every org mention goes through the redacted copies built in _detail_context.

_TABS = ("evaluation", "posting", "organization", "documents", "history")

# Provider ids as the operator knows them (the Tier 3 card names the destination).
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openmodel": "OpenModel",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}


def _days_ago(iso: str | None) -> str | None:
    """'today', '6 days ago', or None when the timestamp is missing or unparseable."""
    if not iso:
        return None
    from datetime import UTC, datetime

    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    days = (datetime.now(UTC) - when).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


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


def _facts(opp, org_name: str | None, source) -> list[str]:
    """The one-line fact row under the title: org, location and remote type, comp, posted or
    first seen, source kind, seen count and whether the posting changed."""
    facts: list[str] = [org_name or "Unknown organization"]
    place = opp.location_raw or ", ".join(p for p in (opp.city, opp.state) if p) or None
    remote = opp.remote_type if opp.remote_type and opp.remote_type != "unknown" else None
    if place and remote:
        facts.append(f"{place}, {remote}")
    elif place or remote:
        facts.append(place or remote)
    comp = _comp_line(opp)
    if comp:
        facts.append(comp)
    posted = _days_ago(opp.date_posted)
    if posted:
        facts.append(f"posted {posted}")
    else:
        seen = _days_ago(opp.first_seen_at)
        facts.append(f"first seen {seen}" if seen else "first seen unknown")
    if source is not None:
        facts.append(f"via {source.kind.replace('_', ' ').title() if source.kind else source.name}")
    elif opp.opp_type == "manual":
        facts.append("pasted by you")
    n = opp.seen_count or 1
    times = "once" if n == 1 else f"{n} times"
    changed = "unchanged" if (opp.version or 1) <= 1 else f"changed {opp.version - 1}x"
    facts.append(f"seen {times}, {changed}")
    return facts


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


async def _tier3_card(s, settings, opp_id: int) -> dict:
    """What the Tier 3 card states BEFORE the preview: the destination (backend, provider, model),
    the token estimate from the real payload builder (read-only, nothing is sent), the cost line,
    and why a send is unavailable, if it is. A failure to build the estimate is shown on the card,
    never a 500 on the page."""
    from kestrel.llm.providers import provider_status
    from kestrel.pipeline.tier3 import build_escalation_preview

    st = provider_status(settings)
    card = {
        "backend": st.backend_name,
        "provider": _PROVIDER_LABELS.get(st.provider, st.backend_name),
        "model": st.model or "(no model configured)",
        "base_url": st.base_url,
        "key_present": st.key_present,
        "tokens": None,
        "redacted": False,
        "unavailable": None,
        "cost": "cost depends on the model's price",
    }
    try:
        preview = await build_escalation_preview(s, settings, opp_id)
    except Exception as exc:  # the card must say why; the page itself must still render
        log.warning("tier3.preview_failed", opp_id=opp_id, error=str(exc))
        card["unavailable"] = f"estimate unavailable: {plain_error(exc)}"
        return card
    if preview.error:
        card["unavailable"] = preview.error
        return card
    card["tokens"] = preview.prompt_tokens_estimate
    card["redacted"] = preview.confidential
    card["unavailable"] = preview.unavailable
    card["cost"] = _cost_line(preview.model, preview.prompt_tokens_estimate)
    return card


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
            "href": None if conf and _url_reveals(m.url, tokens) else m.url,
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
            "facts": _facts(opp, org_name, detail["source"]),
            "tailor_master": _pick_master(detail["masters"], opp.track),
            "tabs": _TABS,
        }
    )
    await load_rule_profile(detail)
    detail.update(decision_extras(detail))
    return detail


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
async def opportunity_detail(request: Request, opp_id: int):
    async with sessionmaker()() as s:
        detail = await _detail_context(s, opp_id)
        if detail is None:
            return HTMLResponse("Not found", status_code=404)
        # The Deeper read note shows only once the role is on the list (D-UI-45); building its
        # estimate runs the payload builder, so it is skipped otherwise.
        detail["tier3"] = (
            await _tier3_card(s, detail["settings"], opp_id) if detail["pursuit"] else None
        )
        detail["renders"] = await opportunity_renders(s, opp_id)
        if detail["pursuit"]:
            detail["qview"] = await _questions_view(s, opp_id, detail)
    detail["request"] = request
    detail["query"] = dict(request.query_params)
    detail["tab"] = "evaluation"
    return templates().TemplateResponse(request, "opportunity_detail.html", detail)


async def _questions_view(s, opp_id: int, detail: dict) -> dict:
    """The Likely questions card's data, redacted like every other tab in confidential mode (the
    model's reply is restored from its stand-in codes, so it can name the employer)."""
    from kestrel.interview.question_task import question_view
    from kestrel.web.redact import redact_json

    return redact_json(
        await question_view(s, opp_id), detail["mapping"], enabled=detail["confidential"]
    )


@router.post("/opportunities/{opp_id}/questions/regenerate", response_class=HTMLResponse)
async def regenerate_questions(request: Request, opp_id: int):
    """Queue a fresh question list for a role on the list. The old list stays until the new one
    validates. Never a 500: a failure is logged and the card says so."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from kestrel.db.models import Pursuit
    from kestrel.interview.question_task import is_applied, request_interview_questions

    note: str | None = None
    async with sessionmaker()() as s:
        try:
            pursuit = (
                await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            ).scalar_one_or_none()
            if pursuit is None:
                note = "Add this role to your list first."
            elif not await is_applied(s, opp_id):
                note = "The list is written when you mark this role Applied."
            else:
                queued = await request_interview_questions(s, opp_id, pursuit.id, regenerate=True)
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
    return templates().TemplateResponse(
        request, "partials/opp_questions.html",
        {"qview": qview, "qnote": note, "opp_id": opp_id},
    )


@router.get("/opportunities/{opp_id}/tab/{name}", response_class=HTMLResponse)
async def opportunity_tab(request: Request, opp_id: int, name: str):
    """One tab of the detail page as a partial (partials/opp_tab_<name>.html). The tab name is in
    the page's URL hash, so a reload re-requests the same tab here."""
    if name not in _TABS:
        return HTMLResponse("No such tab", status_code=404)
    from kestrel.web.redact import redact_json

    async with sessionmaker()() as s:
        detail = await _detail_context(s, opp_id)
        if detail is None:
            return HTMLResponse("Not found", status_code=404)
        conf, mapping = detail["confidential"], detail["mapping"]
        if name == "organization":
            org = detail["org"]
            ctx = await org_context(s, org.id if org is not None else None)
            detail["research"] = redact_json(ctx["research"], mapping, enabled=conf)
            detail["contacts"] = redact_json(ctx["contacts"], mapping, enabled=conf)
            detail["org_signals"] = redact_json(
                list(org.signals or []) if org is not None else [], mapping, enabled=conf
            )
        elif name == "documents":
            detail["renders"] = await opportunity_renders(s, opp_id)
        elif name == "history":
            history = await opportunity_history(s, opp_id)
            detail["history"] = redact_json(history, mapping, enabled=conf)
    detail["request"] = request
    detail["tab"] = name
    return templates().TemplateResponse(request, f"partials/opp_tab_{name}.html", detail)


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
        "schedule_next": nxt.strftime("%A %Y-%m-%d %H:%M") if nxt else None,
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
        ctx = await _mode_context(s)
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


@router.get("/runs", response_class=HTMLResponse)
async def runs(request: Request):
    async with sessionmaker()() as s:
        activity = await live_activity(s)
        recent = await list_runs(s)
        scans = await recent_scans(s)
        from kestrel.analytics.run_summary import run_summary
        from kestrel.analytics.run_summary import view as run_view

        # The live panel (D-UI-58) covers every track: the worker runs both.
        run = run_view(await run_summary(s, None))
        from kestrel.analytics.progress import _model_in_use

        # Only a one-run choice is worth a line here; the standing models live on Models.
        shown = await _model_in_use(s, await load_settings_with_db(s), bool(run.get("active")))
        from kestrel.llm import credit

        ctx = {"activity": activity, "runs": recent, "scans": scans, "run": run,
               "credit_notices": credit.notices(),
               "run_model": shown if shown and "(this run only)" in shown else None,
               **(await _mode_context(s)),
               **(await _keep_awake_context(s))}
    tmpl = "partials/runs_live.html" if request.headers.get("HX-Request") else "runs.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/runs/now", response_class=HTMLResponse)
async def runs_now(request: Request):
    """The Runs page "Run now" form: queue the next worker cycle on local or hosted models (the
    same one-shot row as POST /api/run/now). Returns the message partial; a refused "hosted"
    says what is missing."""
    from kestrel.config import MODES, HostedModeError
    from kestrel.llm import credit
    from kestrel.services.setting_overrides import queue_run_mode

    credit.clear()  # the operator asked for a run: let sends to an online key resume (D-LLM-8)
    form = await request.form()
    mode = str(form.get("mode") or "")
    async with sessionmaker()() as s:
        if mode not in MODES:
            mode = (await load_settings_with_db(s)).llm.mode
        try:
            await queue_run_mode(s, mode)
            await s.commit()
        except HostedModeError as exc:
            return HTMLResponse(
                f'<span class="warnbox">Not queued: {html.escape(plain_error(exc))}.</span>'
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
