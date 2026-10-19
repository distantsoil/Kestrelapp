"""The remaining six of the eight opportunity-detail actions (SPEC-INTERFACES 2.1). Escalate to
Tier 3 already exists (kestrel/web/pages/escalate.py) and the override control already exists
(partials/override_control.html); this module adds Shortlist, Tailor, Research, Find contacts,
Prep pack, and Archive.

D-UI-45 adds the two primary actions of the drawer and the detail page, which re-render
partials/opp_decision.html: Add to my shortlist (`/add`: the shortlist write plus the "operator
agreed" label the learning loop reads) and Not for me (`/not-for-me`: Hide with reason
not_interested plus a pass label), and `/restore` (Unhide from the same panel). Each response
carries the nav count badges out of band and an `HX-Trigger: pipelineRefresh` header, so the
row and the badge change with the panel. Shortlist itself writes the same label: they were one
action already.

Every route re-renders `partials/opp_actions.html` with a result message, mirroring
kestrel/web/pages/prep.py: a write path must never turn an unexpected failure into a 500 (CLAUDE.md
hard rules), so every DB/service call is wrapped and an inline message replaces a stack trace.

Research and Find contacts touch `kestrel.research.company.research_org` and
`kestrel.research.contacts.discover_contacts`. Both take excerpts already acquired (or acquire them
internally); here `http=None` is passed to excerpt acquisition deliberately, which keeps this
action to the DB-derived (already-ingested ATS postings) and operator-paste tiers rather than
opening a new live-fetch surface -- `kestrel.research.excerpts.acquire_excerpts` documents `http`
as optionally None ("fully offline"). Wiring the live company-page/EDGAR/SearXNG tiers is a
separate, larger change (a rate-limited kestrel.ingest.http.HttpClient instance shared across
requests) and is out of scope for wiring up the action buttons themselves. The LLM call itself
(research_org's synthesis, discover_contacts' extraction) still runs for real, against whatever
backend Settings points at."""

from __future__ import annotations

import html

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import ARCHIVE_REASONS, PURSUIT_STATUS, valid_pursuit_status
from kestrel.db.models import (
    DocMaster,
    EngagementOffer,
    Notification,
    Offer,
    Opportunity,
    Org,
    Outcome,
    PrepPack,
    Pursuit,
    StatusEvent,
    VerdictOverride,
)
from kestrel.ingest.http import HttpClient
from kestrel.llm.client import LLMClient
from kestrel.research.company import research_org
from kestrel.research.contacts import discover_contacts
from kestrel.research.excerpts import acquire_excerpts
from kestrel.services.org_research import run_org_research
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.fit import GOOD, NOT_FIT, verdict_for_fit
from kestrel.web.queries import latest_evaluation, record_override
from kestrel.web.words import words

router = APIRouter()
log = structlog.get_logger()


async def _latest_prep_pack(session: AsyncSession, opportunity_id: int) -> PrepPack | None:
    """Return the most recent prep pack for an opportunity, if any."""
    return (
        await session.execute(
            select(PrepPack)
            .where(PrepPack.opportunity_id == opportunity_id)
            .order_by(PrepPack.created_at.desc(), PrepPack.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _context(
    session: AsyncSession,
    opp_id: int,
    *,
    message: str | None = None,
    error: str | None = None,
) -> dict:
    opp = await session.get(Opportunity, opp_id)
    org: Org | None = None
    if opp is not None and opp.org_id is not None:
        org = await session.get(Org, opp.org_id)
    return {
        "opp": opp,
        "org": org,
        **await action_context(session, opp_id, opp=opp),
        "archive_reasons": ARCHIVE_REASONS,
        "message": message,
        "error": error or ("Opportunity not found." if opp is None else None),
    }


async def action_context(
    session: AsyncSession, opp_id: int, *, opp: Opportunity | None = None
) -> dict:
    """The keys the actions partial needs BEYOND opp/org: the pursuit and the track's document
    masters.

    Deliberately excludes opp and org so the opportunity detail route can merge this into a context
    where it has already applied confidential-mode redaction. Overwriting those with freshly loaded
    rows would silently undo the redaction and put the current employer's real name back on the
    page, which is a privacy regression, not a cosmetic one.
    """
    if opp is None:
        opp = await session.get(Opportunity, opp_id)
    if opp is None:
        return {"pursuit": None, "masters": []}
    pursuit = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
    ).scalar_one_or_none()
    masters = list(
        (
            await session.execute(
                select(DocMaster).where(DocMaster.track == opp.track, DocMaster.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    return {
        "pursuit": pursuit,
        "pursuit_column": _status_word(pursuit) if pursuit is not None else None,
        "masters": masters,
    }


async def _render(
    request: Request,
    session: AsyncSession,
    opp_id: int,
    *,
    message: str | None = None,
    error: str | None = None,
    prep_ready: bool | None = None,
) -> HTMLResponse:
    ctx = await _context(session, opp_id, message=message, error=error)
    if prep_ready is None:
        prep_ready = await _latest_prep_pack(session, opp_id) is not None
    ctx["prep_ready"] = prep_ready
    return templates().TemplateResponse(request, "partials/opp_actions.html", ctx)


# ====================================================================== the decision panel
_POSITIVE = ("apply", "pursue")

# Every label an Add writes carries one of these reasons, so Remove can take back exactly the
# labels the Add wrote and leave every other judgment alone (D-UI-75).
ADD_REASON = "Added to my shortlist."
ADD_REASON_WITH_MODEL = "Added to my shortlist, siding with the model."
ADD_REASONS = (ADD_REASON, ADD_REASON_WITH_MODEL)


async def _latest_label(
    session: AsyncSession, opp_id: int, ev_id: int | None
) -> VerdictOverride | None:
    """The operator's latest word on this evaluation (on no evaluation when ev_id is None)."""
    stmt = select(VerdictOverride).where(VerdictOverride.opportunity_id == opp_id)
    stmt = stmt.where(
        VerdictOverride.evaluation_id == ev_id
        if ev_id is not None
        else VerdictOverride.evaluation_id.is_(None)
    )
    return (
        await session.execute(stmt.order_by(VerdictOverride.id.desc()).limit(1))
    ).scalar_one_or_none()


async def record_add_label(
    session: AsyncSession, opp: Opportunity, *, commit: bool = True
) -> VerdictOverride | None:
    """The label Add to my shortlist teaches the ranker. Idempotent per evaluation: it is
    skipped only when the operator's LATEST word on this evaluation is already a yes (apply /
    pursue). Any other latest word (a Not for me pass, then Unhide, then Add) is superseded by a
    new label, so the learning loop reads the operator's current judgment, not a stale one.

    A positive system read (apply / pursue) is an agree. A "your call" whose model read was
    positive records the model's verdict: the operator sided with the model, at the model's
    score when that clears the apply bar and at the "good fit" training score otherwise (an
    apply label carrying a consider-range score would teach the ranker a contradiction).
    Anything else (a maybe, a not a fit, nothing scored yet) records the "good fit" word,
    because adding a role the system did not rate is a disagreement, not an agreement."""
    ev = await latest_evaluation(session, opp.id)
    latest = await _latest_label(session, opp.id, ev.id if ev else None)
    if latest is not None and latest.operator_verdict in _POSITIVE:
        return None
    good_verdict, good_score = verdict_for_fit(GOOD, opp.track)
    if ev is not None and ev.tier != 1 and ev.verdict in _POSITIVE:
        return await record_override(
            session, opp_id=opp.id, mode="agree", reason=ADD_REASON, commit=commit
        )
    if ev is not None and ev.needs_review and ev.verdict_model in _POSITIVE:
        score = ev.score if ev.score is not None and ev.score >= good_score else good_score
        return await record_override(
            session, opp_id=opp.id, mode="override", operator_verdict=ev.verdict_model,
            operator_score=score, reason=ADD_REASON_WITH_MODEL, commit=commit,
        )
    return await record_override(
        session, opp_id=opp.id, mode="override", operator_verdict=good_verdict,
        operator_score=good_score, reason=ADD_REASON, commit=commit,
    )


async def record_not_for_me_label(
    session: AsyncSession, opp: Opportunity, *, commit: bool = True
) -> VerdictOverride | None:
    """The pass label Not for me teaches the ranker, skipped only when the operator's latest word
    on this evaluation is already a pass."""
    ev = await latest_evaluation(session, opp.id)
    verdict, score = verdict_for_fit(NOT_FIT, opp.track)
    latest = await _latest_label(session, opp.id, ev.id if ev else None)
    if latest is not None and latest.operator_verdict == verdict:
        return None
    return await record_override(
        session, opp_id=opp.id, mode="override", operator_verdict=verdict,
        operator_score=score, reason="Not for me.", commit=commit,
    )


def _status_word(p: Pursuit) -> str:
    """A pursuit's board column as the operator says it ("To apply"), never the stored status."""
    from kestrel.services import pursuit_board as board_svc

    key = board_svc.column_key(p.track, p.status)
    col = board_svc.column_by_key(p.track, key) if key else None
    return col.label if col else p.status.replace("_", " ")


async def add_to_list(session: AsyncSession, opp: Opportunity) -> tuple[Pursuit | None, str]:
    """Create the pursuit (idempotent) and record the Add label in ONE transaction, so a label
    failure can never leave a pursuit the message says was not added. Returns
    (pursuit, message); pursuit is None when the track has no valid first status."""
    existing = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    ).scalar_one_or_none()
    if existing is not None:
        await record_add_label(session, opp)
        where = words(opp.track)["on_list"].replace("my", "your")
        return existing, f"Already {where} ({_status_word(existing)})."
    status = PURSUIT_STATUS[opp.track][0]
    if not valid_pursuit_status(opp.track, status):
        return None, f"No valid initial pursuit status configured for track {opp.track}."
    pursuit = Pursuit(
        opportunity_id=opp.id, track=opp.track, status=status, last_activity_at=utcnow_iso()
    )
    session.add(pursuit)
    await session.flush()
    await record_add_label(session, opp, commit=False)
    await session.commit()
    return pursuit, f"Added to your {words(opp.track)['list']} ({_status_word(pursuit)})."


async def remove_from_list(session: AsyncSession, opp: Opportunity) -> str:
    """Undo an Add, at any stage, in ONE transaction (D-UI-75). The pursuit goes with its status
    history, outcome and offers, so a role added by mistake, or tracked as the wrong application,
    leaves nothing in the board, the funnel or the training signal. The labels the Add wrote go
    too (they carry ADD_REASONS); every other judgment stays, so the learning loop reads the
    operator's word from before the mistake. A notification about the role stays, unlinked."""
    where = words(opp.track)["on_list"].replace("my", "your")
    pursuit = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    ).scalar_one_or_none()
    if pursuit is None:
        return f"Not {where}."
    for model in (StatusEvent, Outcome, Offer, EngagementOffer):
        await session.execute(delete(model).where(model.pursuit_id == pursuit.id))
    await session.execute(
        update(Notification).where(Notification.pursuit_id == pursuit.id).values(pursuit_id=None)
    )
    await session.execute(
        delete(VerdictOverride).where(
            VerdictOverride.opportunity_id == opp.id, VerdictOverride.reason.in_(ADD_REASONS)
        )
    )
    # Hidden as applied (the applied-history import) was part of the same mistake: unhide it, or
    # the role stays hidden and every re-post of it is hidden as applied (normalize.py).
    if opp.archive_reason == "applied":
        opp.archived_at = None
        opp.archive_reason = None
        if opp.stage_reason == "Imported applied history":
            opp.stage_reason = None
    await session.delete(pursuit)
    await session.commit()
    return f"Removed from your {words(opp.track)['list']}."


async def _decision(
    request: Request,
    session: AsyncSession,
    opp_id: int,
    *,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Re-render the decision panel, the nav badges out of band, and ask the list to refresh."""
    from kestrel.web.routes_api import oob_nav_counts
    from kestrel.web.routes_pages import decision_context

    # Counts first: decision_context applies display-only redaction to ORM rows, and a query
    # after that would autoflush it.
    oob = await oob_nav_counts(session)
    headers = {"HX-Trigger": "pipelineRefresh"}
    ctx = await decision_context(session, opp_id)
    if ctx is None:
        note = html.escape(error or "Opportunity not found.")
        return HTMLResponse(
            f'<div id="decision" class="decision"><div class="warnbox">{note}</div></div>{oob}',
            headers=headers,
        )
    ctx.update(message=message, error=error)
    body = templates().TemplateResponse(request, "partials/opp_decision.html", ctx).body
    return HTMLResponse(body.decode("utf-8") + oob, headers=headers)


@router.post("/opportunities/{opp_id}/add", response_class=HTMLResponse)
async def add(request: Request, opp_id: int) -> HTMLResponse:
    """Add to my shortlist: the pursuit plus the agree-style label, then the panel re-rendered."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            else:
                pursuit, text = await add_to_list(s, opp)
                message, error = (text, None) if pursuit is not None else (None, text)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "add_to_list.failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not add it to your shortlist: {plain_error(exc)}"
        return await _decision(request, s, opp_id, message=message, error=error)


@router.post("/opportunities/{opp_id}/remove", response_class=HTMLResponse)
async def remove(request: Request, opp_id: int) -> HTMLResponse:
    """Remove from my shortlist: the undo for Add, then the panel re-rendered."""
    message: str | None = None
    error: str | None = None
    opp: Opportunity | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            else:
                message = await remove_from_list(s, opp)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "remove_from_list.failed", opp_id=opp_id,
                error_type=type(exc).__name__, error=str(exc),
            )
            await s.rollback()
            where = words(opp.track if opp else None)["list"]
            error = f"Could not remove it from your {where}: {plain_error(exc)}"
        return await _decision(request, s, opp_id, message=message, error=error)


@router.post("/opportunities/{opp_id}/not-for-me", response_class=HTMLResponse)
async def not_for_me(request: Request, opp_id: int) -> HTMLResponse:
    """Not for me: Hide with reason not_interested and record a pass label. Idempotent: an
    already hidden row is left as it is and no second label is written."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif opp.archived_at:
                message = "Already hidden."
            else:
                opp.archived_at = utcnow_iso()
                opp.archive_reason = "not_interested"
                await record_not_for_me_label(s, opp, commit=False)
                await s.commit()  # the hide and its label land together
                message = "Hidden as not for you. The Hidden chip shows it again."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "not_for_me.failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not hide it: {plain_error(exc)}"
        return await _decision(request, s, opp_id, message=message, error=error)


@router.post("/opportunities/{opp_id}/restore", response_class=HTMLResponse)
async def restore(request: Request, opp_id: int) -> HTMLResponse:
    """Unhide from the decision panel. The label a Not for me wrote stays: it was the operator's
    word at the time, and the history keeps it."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif not opp.archived_at:
                message = "Not hidden."
            else:
                opp.archived_at = None
                opp.archive_reason = None
                await s.commit()
                message = "Back in the active pipeline."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "restore_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not unhide: {plain_error(exc)}"
        return await _decision(request, s, opp_id, message=message, error=error)


# ============================================================================ 1. Shortlist
@router.post("/opportunities/{opp_id}/shortlist", response_class=HTMLResponse)
async def shortlist(request: Request, opp_id: int) -> HTMLResponse:
    """Create a Pursuit row for this opportunity and record the Add to my shortlist label (the same
    action, D-UI-45). Idempotent: a second click finds the existing row and says so rather than
    creating a duplicate (pursuit.opportunity_id is unique anyway, but the check lets the
    operator get a clear message instead of a constraint-violation 500)."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            else:
                pursuit, text = await add_to_list(s, opp)
                message, error = (text, None) if pursuit is not None else (None, text)
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "shortlist_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not add it to your shortlist: {plain_error(exc)}"
        return await _render(request, s, opp_id, message=message, error=error)


@router.get("/opportunities/{opp_id}/actions", response_class=HTMLResponse)
async def actions_panel(request: Request, opp_id: int) -> HTMLResponse:
    """The actions panel as it stands, for its own pipelineRefresh reload. Read-only."""
    async with sessionmaker()() as s:
        prep = await _latest_prep_pack(s, opp_id)
        return await _render(request, s, opp_id, prep_ready=prep is not None)


# ============================================================================ 2. Tailor
@router.get("/opportunities/{opp_id}/tailor", response_class=HTMLResponse)
async def tailor(request: Request, opp_id: int) -> HTMLResponse:
    """List the DocMaster rows for this opportunity's track, each linking to the existing render
    flow (`GET /renders/new?master_id=<id>&opportunity_id=<id>`). Read-only: no new render pipeline
    here, just the catalog and the link."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
        except Exception as exc:  # a route must never 500, read paths included
            log.warning(
                "tailor_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            error = f"Could not load document masters: {plain_error(exc)}"
        ctx = await _context(s, opp_id, message=message, error=error)
        if ctx["opp"] is not None and not ctx["masters"] and error is None:
            ctx["message"] = f"No active document masters for the {ctx['opp'].track} track yet."
        return templates().TemplateResponse(request, "partials/opp_actions.html", ctx)


# ============================================================================ 3. Research
@router.post("/opportunities/{opp_id}/research", response_class=HTMLResponse)
async def research(request: Request, opp_id: int) -> HTMLResponse:
    """Run P5 org research (kestrel.research.company.research_org) for this opportunity's org."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif opp.org_id is None:
                error = "No organization is linked to this opportunity yet."
            else:
                org = await s.get(Org, opp.org_id)
                if org is None:
                    error = "Organization record is missing."
                else:
                    # Shared with `kestrel research`: polite public-page client, model only via
                    # research_org, commit included. `research_org` is passed by name so this
                    # module's seam stays patchable.
                    record = await run_org_research(s, org, researcher=research_org)
                    if record.insufficient_sources:
                        message = (
                            f"Research recorded for {org.name}, but fewer than 2 sources were "
                            "available: labeled insufficient, not a solid summary."
                        )
                    else:
                        n = len(record.signals or [])
                        message = f"Research complete for {org.name}: {n} signal(s) captured."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "research_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Research failed: {plain_error(exc)}"
        return await _render(request, s, opp_id, message=message, error=error)


# ============================================================================ 4. Find contacts
@router.post("/opportunities/{opp_id}/contacts", response_class=HTMLResponse)
async def find_contacts(request: Request, opp_id: int) -> HTMLResponse:
    """Discover contacts (P6, kestrel.research.contacts.discover_contacts) for this opportunity's
    org. Drafts only (hard rule 1): nothing this creates is ever sent."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif opp.org_id is None:
                error = "No organization is linked to this opportunity yet."
            else:
                org = await s.get(Org, opp.org_id)
                if org is None:
                    error = "Organization record is missing."
                else:
                    settings = await load_settings_with_db(s)
                    # Same reasoning as Research: with no fetching client only the DB-derived ATS
                    # tier contributes, so contact discovery would run against a single excerpt.
                    async with (
                        httpx.AsyncClient() as llm_http,
                        HttpClient(
                            user_agent=settings.ingest.user_agent,
                            default_rate_per_sec=settings.ingest.default_rate_per_sec,
                            timeout=settings.ingest.request_timeout_s,
                            respect_robots=settings.ingest.respect_robots,
                        ) as fetch_http,
                    ):
                        excerpts = await acquire_excerpts(s, fetch_http, org, settings=settings)
                        if len(excerpts) == 0:
                            message = (
                                "No public sources found for this organization yet: add an open "
                                "posting or a company URL so there is something to cite."
                            )
                            created = []
                        else:
                            client = LLMClient(settings, http=llm_http)
                            created = await discover_contacts(
                                s, client, org, excerpts, opportunity=opp
                            )
                    if len(excerpts) > 0:
                        if not created:
                            message = (
                                "No contacts found (or none could be verified against a source)."
                            )
                        else:
                            message = f"Found {len(created)} contact(s) for {org.name}."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "find_contacts_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Contact discovery failed: {plain_error(exc)}"
        return await _render(request, s, opp_id, message=message, error=error)


# ============================================================================ 5. Prep pack
@router.post("/opportunities/{opp_id}/prep", response_class=HTMLResponse)
async def prep_pack(request: Request, opp_id: int) -> HTMLResponse:
    """Build an interview prep pack (kestrel.interview.prep.assemble_prep_pack). The full prep pack
    page (content, questions, panel, PDF) already lives at /prep/{opportunity_id}; this just kicks
    off assembly from the opportunity detail and links there."""
    from kestrel.interview.prep import assemble_prep_pack

    message: str | None = None
    error: str | None = None
    prep_ready = False
    async with sessionmaker()() as s:
        try:
            built = await assemble_prep_pack(s, opp_id)
            if built is None:
                error = "Opportunity not found."
            else:
                message = "Your interview prep is ready."
                prep_ready = True
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "prep_pack_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not build the interview prep: {plain_error(exc)}"
        return await _render(
            request, s, opp_id, message=message, error=error, prep_ready=prep_ready
        )


# ============================================================================ 6. Hide (archive)
# "Hide" is the operator-facing word; the column and the routes keep the archive name. A hide
# records why (ARCHIVE_REASONS: applied elsewhere, not interested, later, other) so the Hidden
# chip can show it and the learning loop can tell "already done" from "not wanted". A pursuit
# with status applied is a different thing and is never hidden by this.
@router.post("/opportunities/{opp_id}/archive", response_class=HTMLResponse)
async def archive(request: Request, opp_id: int) -> HTMLResponse:
    """Hide this opportunity from the active pipeline, with an optional reason from the form
    field `reason`. Reversible: see `unarchive` below."""
    message: str | None = None
    error: str | None = None
    form = await request.form()
    reason = str(form.get("reason") or "").strip() or None
    if reason is not None and reason not in ARCHIVE_REASONS:
        error = f"Unknown reason {reason!r}; choose one of: {', '.join(ARCHIVE_REASONS)}."
        async with sessionmaker()() as s:
            return await _render(request, s, opp_id, error=error)
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif opp.archived_at:
                message = f"Already hidden (since {opp.archived_at})."
            else:
                opp.archived_at = utcnow_iso()
                opp.archive_reason = reason
                await s.commit()
                why = f" ({ARCHIVE_REASONS[reason].lower()})" if reason else ""
                message = f"Hidden{why}. Out of the active pipeline; find it under Hidden."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "archive_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not hide: {plain_error(exc)}"
        return await _render(request, s, opp_id, message=message, error=error)


@router.post("/opportunities/{opp_id}/unarchive", response_class=HTMLResponse)
async def unarchive(request: Request, opp_id: int) -> HTMLResponse:
    """Reverse Hide: restore this opportunity to the active pipeline and clear the reason."""
    message: str | None = None
    error: str | None = None
    async with sessionmaker()() as s:
        try:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                error = "Opportunity not found."
            elif not opp.archived_at:
                message = "Not hidden."
            else:
                opp.archived_at = None
                opp.archive_reason = None
                await s.commit()
                message = "Unhidden. Back in the active pipeline."
        except Exception as exc:  # a write path must never 500 (CLAUDE.md hard rules)
            log.warning(
                "unarchive_failed", opp_id=opp_id, error_type=type(exc).__name__, error=str(exc)
            )
            await s.rollback()
            error = f"Could not unhide: {plain_error(exc)}"
        return await _render(request, s, opp_id, message=message, error=error)
