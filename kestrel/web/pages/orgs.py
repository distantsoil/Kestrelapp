"""Company page (D-UI-123, SPEC-INTERFACES 2.1 `/orgs/{id}`): one page per company with the
sourced summary, public facts, pay, the people the operator knows there and the public contacts.

  GET  /orgs/{id}                       the page (the detail alone for an HTMX request)
  GET  /orgs/{id}/summary               the summary card (polled while a pass is queued)
  POST /orgs/{id}/summary/refresh       Refresh. On an online Tier 2 it answers with the ask-first
                                        dialog until the operator confirms (`confirmed=1`)
  POST /orgs/{id}/hide, /unhide         Hide this company, and Undo / Show this company here
  GET  /orgs/{id}/state                 the header buttons and the hidden banner (after an Undo)

Nothing here calls a model or fetches a page: Refresh writes a task row and the worker does the
rest (hard rules 5 and 7). Hide only sets `org.hidden_at`; it deletes nothing. Nothing is ever
sent to a company or a contact (hard rule 1). The card and the header swap in place."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Org, OrgResearch
from kestrel.research import org_summary as osum
from kestrel.services import hidden_orgs
from kestrel.services.online_consent import destination_id
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import company_view as cv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.redact import redact_name, redaction_map

log = structlog.get_logger()
router = APIRouter()


def _toast(message: str, undo_url: str | None = None) -> str:
    body: dict[str, Any] = {"message": message}
    if undo_url:
        body["undoUrl"] = undo_url
    return json.dumps({"k-toast": body})


def _online_model(settings: Any, online: bool) -> str | None:
    """The online model's name when Tier 2 runs on one, else None."""
    if not online:
        return None
    try:
        return str(settings.llm.tiers["tier2"].model) or "an online model"
    except (AttributeError, KeyError):
        return "an online model"


async def _redaction(session: AsyncSession, settings: Any):
    if not settings.confidential_mode:
        return None, None
    mapping = await redaction_map(session)
    return mapping, cv.redactor(mapping, True)


async def _summary_ctx(
    session: AsyncSession, org: Org, *, notice: str | None = None, dialog: dict | None = None
) -> dict[str, Any]:
    settings = await load_settings_with_db(session)
    online = await osum.runs_online(session)
    live = await osum.is_queued(session, org.id)
    _, redact = await _redaction(session, settings)
    view = cv.summary_view(
        await osum.load_record(session, org.id),
        live=live,
        online_model=_online_model(settings, online),
        redact=redact,
    )
    if notice is None and online and not live and await osum.is_held(session, org.id):
        notice = (
            "Nothing was sent. This summary now runs on an online model. "
            "Refresh again to confirm the online send."
        )
    return {"org": org, "sm": view, "notice": notice, "dialog": dialog}


async def _ask_dialog(session: AsyncSession, org: Org, settings: Any) -> dict[str, Any]:
    """What an online refresh sends, in the operator's words. The Tier 2 line is the text they
    acknowledged for online use (`disclosure_terms`), shown under Show details."""
    record = await osum.load_record(session, org.id)
    terms = settings.llm.hosted_mode.disclosure_terms()["sends"]
    return {
        "model": _online_model(settings, True),
        "dest": destination_id(settings, "tier2"),
        "pages": int((record or {}).get("source_count") or 0),
        "tier2_line": terms.get("tier2", ""),
    }


def _held(settings: Any, mapping: Any, org: Org) -> bool:
    """Confidential mode: this company is the operator's own employer, or Kestrel cannot tell
    because no employer is chosen (the same fail-closed rule as Tier 3), so its name never goes
    online. A server-side check, whatever the page showed."""
    if not settings.confidential_mode:
        return False
    return not mapping or redact_name(org.name, mapping, enabled=True) != org.name


async def _queue_summary_on_open(session: AsyncSession, org_id: int) -> None:
    """Opening a company with no (or a week-old) summary queues the P16 task (D-LLM-9). Never
    fails the page: the summary is an extra."""
    from sqlalchemy.exc import SQLAlchemyError

    try:
        if await osum.request_if_needed(session, org_id, "opened"):
            await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        log.warning("org_summary_queue_failed", error_type=type(exc).__name__, error=str(exc))


def _card(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/company_summary.html", ctx)


@router.get("/orgs/{org_id}/summary", response_class=HTMLResponse)
async def summary_card(request: Request, org_id: int):
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            return HTMLResponse('<div class="warnbox">Company not found.</div>', status_code=404)
        ctx = await _summary_ctx(s, org)
    return _card(request, ctx)


@router.post("/orgs/{org_id}/summary/refresh", response_class=HTMLResponse)
async def refresh_summary_route(
    request: Request,
    org_id: int,
    confirmed: str = Form(""),
    model: str = Form(""),
    dest: str = Form(""),
):
    """Refresh: queue the P16 task. On a local model the click is the consent. On an online
    Tier 2 the click only opens the dialog; the send is `confirmed=1` from that dialog."""
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            return HTMLResponse('<div class="warnbox">Company not found.</div>', status_code=404)
        settings = await load_settings_with_db(s)
        if await osum.runs_online(s):
            mapping, _ = await _redaction(s, settings)
            if _held(settings, mapping, org):
                ctx = await _summary_ctx(
                    s, org,
                    notice="Nothing was sent. Confidential mode is on and this company is your "
                    "current employer, or no employer is chosen yet, so its name stays on this "
                    "computer. Use a model on this computer to refresh it.",
                )
                return _card(request, ctx)
            if confirmed != "1":
                ctx = await _summary_ctx(s, org, dialog=await _ask_dialog(s, org, settings))
                return _card(request, ctx)
            # The dialog posts the model it showed. Checked here too, whatever the page said.
            if not settings.llm.hosted_mode.active:
                ctx = await _summary_ctx(
                    s, org,
                    notice="Nothing was sent. Online models need Hosted mode, which is off.",
                )
                return _card(request, ctx)
            if model.strip() != _online_model(settings, True) or (
                dest.strip() != destination_id(settings, "tier2")
            ):
                ctx = await _summary_ctx(
                    s, org,
                    notice="Nothing was sent. The model changed since the dialog opened. "
                    "Press Refresh again to see what would be sent.",
                )
                return _card(request, ctx)
        queued = await osum.refresh_summary(s, org_id)
        await s.commit()
        msg = (
            "Refreshing the summary. It runs in the background."
            if queued else "A summary refresh is already waiting."
        )
        ctx = await _summary_ctx(s, org, notice=msg)
    return _card(request, ctx)


def _state(request: Request, org: Org, *, toast: str | None = None) -> HTMLResponse:
    resp = templates().TemplateResponse(
        request, "partials/company_state.html",
        {"org": org, "hidden": org.hidden_at is not None, "display_name": org.name},
    )
    if toast:
        resp.headers["HX-Trigger"] = toast
    return resp


@router.post("/orgs/{org_id}/hide", response_class=HTMLResponse)
async def hide(request: Request, org_id: int):
    async with sessionmaker()() as s:
        before = await s.get(Org, org_id)
        already = before is not None and before.hidden_at is not None
        if not await hidden_orgs.hide_org(s, org_id):
            return HTMLResponse('<div class="warnbox">Company not found.</div>', status_code=404)
        await s.commit()
        org = await s.get(Org, org_id)
        settings = await load_settings_with_db(s)
        name = org.name
        if settings.confidential_mode:
            name = redact_name(org.name, await redaction_map(s), enabled=True) or org.name
        stamp = org.hidden_at
    if already or not stamp:  # nothing changed, so there is nothing to undo
        return _state(request, org, toast=_toast(f"{name} was already hidden."))
    return _state(
        request, org,
        toast=_toast(f"Hidden {name}. Its roles are out of your Pipeline.",
                     f"/orgs/{org_id}/unhide?at={quote(stamp)}"),
    )


@router.post("/orgs/{org_id}/unhide", response_class=HTMLResponse)
async def unhide(request: Request, org_id: int, at: str = ""):
    """Put a company back. From a toast's Undo `at` is the stamp that hide wrote; the company
    comes back only while it still carries it."""
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            return HTMLResponse('<div class="warnbox">Company not found.</div>', status_code=404)
        if at and org.hidden_at != at:
            return HTMLResponse('<span data-undo-msg data-ok="0">'
                                "This can no longer be undone.</span>")
        await hidden_orgs.unhide_org(s, org_id)
        await s.commit()
        org = await s.get(Org, org_id)
    return _state(request, org)


@router.get("/orgs/{org_id}/state", response_class=HTMLResponse)
async def state(request: Request, org_id: int):
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            return HTMLResponse('<div class="warnbox">Company not found.</div>', status_code=404)
    return _state(request, org)


@router.get("/orgs/{org_id}", response_class=HTMLResponse)
async def page(request: Request, org_id: int):
    async with sessionmaker()() as s:
        org = await s.get(Org, org_id)
        if org is None:
            if request.headers.get("HX-Request"):
                return HTMLResponse(
                    '<div class="warnbox">Company not found.</div>', status_code=404
                )
            ctx = {
                "title": "Company", "query": dict(request.query_params),
                "found": False, "org_id": org_id,
            }
            return templates().TemplateResponse(request, "orgs.html", ctx, status_code=404)

        data = await cv.gather(s, org)
        await _queue_summary_on_open(s, org.id)
        settings = await load_settings_with_db(s)
        mapping, redact = await _redaction(s, settings)
        display_name = redact_name(org.name, mapping, enabled=True) if mapping else org.name
        summary = await _summary_ctx(s, org)
        research = (
            await s.execute(
                select(OrgResearch)
                .where(OrgResearch.org_id == org.id)
                .order_by(OrgResearch.researched_at.desc(), OrgResearch.id.desc())
                .limit(1)
            )
        ).scalars().first()

    employer = bool(
        redact is not None and mapping and redact_name(org.name, mapping, enabled=True) != org.name
    )
    if employer and org.domain:
        # Its own address names it too: mask the domain wherever a page title or source shows it.
        base = redact
        host = re.compile(re.escape(org.domain.removeprefix("www.")), re.I)
        redact = lambda text: host.sub("the company's site", base(text))  # noqa: E731
    if redact is not None:
        data = cv.redact_tree(data, redact)
    if research is not None:
        research = cv.redact_research(research, redact or (lambda text: text))
    if employer:
        # The company is the operator's employer: its own address and drafts name it, so they
        # stay off the page while confidential mode is on.
        data = cv.strip_urls(data)
        for c in data["contacts"]:
            c["drafts"] = []
        for p in data["know"]["people"]:
            p["drafts"] = []
    ctx = {
        "title": display_name or f"Org {org.id}",
        "query": dict(request.query_params),
        "found": True,
        "display_name": display_name,
        "site": None
        if employer
        else (org.url or (f"https://{org.domain}" if org.domain else None)),
        "site_label": None if employer else (org.domain or org.url),
        "hidden": org.hidden_at is not None,
        "research": research,
        **summary,
        **data,
    }
    tmpl = "partials/org_detail.html" if request.headers.get("HX-Request") else "orgs.html"
    return templates().TemplateResponse(request, tmpl, ctx)
