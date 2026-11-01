"""Analytics page (D-UI-101, SPEC-INTERFACES 2.1, SPEC-MODULES 7): is the search working, and where
is the leak. One date range for the whole page, a Table view for accessibility, and an Export menu.

  GET  /analytics?range=30|90|all&view=table   the page (the body alone for an HTMX request)
  POST /analytics/narrative?range=             Rewrite summary: the one place the page asks a model
  GET  /analytics/export/numbers.csv           every figure on the page, with its base
  GET  /analytics/export/applications.csv      every application with its status history
  GET  /analytics/export/summary.pdf           the summary and charts, rendered locally

The numbers come from `kestrel.analytics.report` (one shared cohort, every figure beside the count
it rests on, anything under five labeled; hard rule 6). The honest summary keeps its rule that the
code-computed verdicts lead: Working, Not working and Worth changing are built in code from those
counts, and a model-written narrative is only added beneath them, on request, never on a visit.
Exports are saved through the shell's Save dialog; nothing is sent anywhere (hard rule 1).

`honest_summary` degrades gracefully when the model is unreachable (a GuardResult with ok=False,
never a raised exception), and the try/except around it is a second safety net so this page never
500s for that reason."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.analytics import report as rpt
from kestrel.analytics.metrics import collect_metrics
from kestrel.analytics.summary import honest_summary
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Pursuit, Setting, StatusEvent
from kestrel.llm import client as llm_client
from kestrel.pipeline._shared import tier_is_hosted
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import fixed_reason, plain_error
from kestrel.web.pages.needs_try import _employer_state
from kestrel.web.redact import confidential_once, redact_json, redact_text

log = structlog.get_logger()

router = APIRouter()

_TRACKS = ("employment", "consulting")
_RANGE_CHOICES = (("30", "Last 30 days"), ("90", "Last 90 days"), ("all", "All time"))


def _track(request: Request) -> str:
    track = active_track(request)
    return track if track in _TRACKS else "employment"


def _updated(iso: str | None) -> str:
    """"Updated today, 8:02 AM" / "Updated Oct 23, 8:02 AM", in the computer's own time."""
    try:
        when = datetime.fromisoformat(iso or "").astimezone()
    except ValueError:
        return ""
    from kestrel.date_display import format_display

    clock = format_display(when, kind="time")
    if when.date() == datetime.now().astimezone().date():
        return f"Updated today, {clock}"
    return f"Updated {format_display(when, 'short')}, {clock}"


def _narrative_key(track: str, range_key: str) -> str:
    return f"analytics.narrative.{track}.{range_key}"


async def _saved_narrative(s: AsyncSession, track: str, range_key: str) -> dict | None:
    row = await s.get(Setting, _narrative_key(track, range_key))
    return row.value if row is not None and isinstance(row.value, dict) else None


async def _save_narrative(s: AsyncSession, track: str, range_key: str, summary: Any) -> dict:
    text = summary.summary_md or ""
    lead = summary.headline_finding or ""
    if summary.not_discriminating and lead and text.startswith(lead):
        # The not-discriminating note above the verdicts already says this; do not say it twice.
        text = text[len(lead):].lstrip()
    value = {
        "generated_at": utcnow_iso(), "summary_md": text,
        "one_thing": summary.one_thing_to_change, "evidence": summary.evidence_for_it,
        "warnings": list(summary.sample_size_warnings),
    }
    row = await s.get(Setting, _narrative_key(track, range_key))
    if row is None:
        s.add(Setting(key=_narrative_key(track, range_key), value=value))
    else:
        row.value = value
    await s.commit()
    return value


async def _redactor(s: AsyncSession):
    conf, mapping = await confidential_once(s)
    return lambda text: redact_text(text or "", mapping, enabled=conf) or ""


async def _context(
    request: Request, s: AsyncSession, track: str, range_key: str, view: str, *,
    ask_model: bool = False, note: str | None = None,
    settings=None,
) -> dict[str, Any]:
    """Assemble the page. The model is asked only when `ask_model` (Rewrite summary); a visit
    shows the code verdicts at once (D-UI-19)."""
    report = await rpt.build_report(s, track, range_key)
    period = rpt.period_facts(report)
    summary = None
    model_note: str | None = note
    settings = settings if settings is not None else await load_settings_with_db(s)
    # P12 runs on tier 2: when that tier is an online model, Rewrite summary asks first.
    hosted_send = tier_is_hosted(settings, "tier2")
    narrative = await _saved_narrative(s, track, range_key)
    try:
        if ask_model:
            async with httpx.AsyncClient() as http:
                client = llm_client.LLMClient(settings, http=http)
                summary = await honest_summary(s, client, track, period=period)
            if summary.from_model:  # a real model answer; an empty one_thing_to_change is fine
                narrative = await _save_narrative(s, track, range_key, summary)
            elif summary.held:
                model_note = "Not sent: choose your current employer in Settings first."
            elif not summary.insufficient_data:
                model_note = "The model did not give a usable answer. The numbers are unchanged."
        else:
            summary = await honest_summary(s, None, track, period=period)
        svo = summary.metrics["score_vs_outcome"]
    except Exception as exc:  # a problem outside honest_summary's own model-call guard
        log.warning("analytics_summary_failed", error_type=type(exc).__name__, error=str(exc))
        svo = (await collect_metrics(s, track))["score_vs_outcome"]
        model_note = f"Summary unavailable ({plain_error(exc)}); the numbers below are computed."
    conf, mapping = await confidential_once(s)
    report = redact_json(report, mapping, enabled=conf)
    narrative = redact_json(narrative, mapping, enabled=conf)
    return {
        "title": "Analytics", "track": track, "range": range_key, "view": view,
        "range_choices": _RANGE_CHOICES, "report": report, "svo": svo,
        "plain": rpt.plain_summary(report, svo), "summary": summary, "narrative": narrative,
        "model_note": model_note, "hosted_send": hosted_send,
        "can_write": summary is not None and not summary.insufficient_data,
        "updated": _updated((narrative or {}).get("generated_at")),
        "tabs": rpt.breakdown_tabs(track),
        "tab": request.query_params.get("tab") or "src",
        "min_n": rpt.MIN_N, "query": dict(request.query_params),
    }


@router.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    track = _track(request)
    range_key = rpt.normalize_range(request.query_params.get("range"))
    view = "table" if request.query_params.get("view") == "table" else "charts"
    async with sessionmaker()() as s:
        ctx = await _context(request, s, track, range_key, view)
    tmpl = "partials/analytics_body.html" if request.headers.get("HX-Request") else "analytics.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/analytics/narrative", response_class=HTMLResponse)
async def analytics_narrative(request: Request):
    """Rewrite summary: write the P12 narrative on request, the one place the page calls a model."""
    track = _track(request)
    range_key = rpt.normalize_range(request.query_params.get("range"))
    from kestrel.services.online_consent import HELD, consent_problem, online_route

    form = await request.form()
    async with sessionmaker()() as s:
        # An online model is asked only after the dialog (Hosted mode, hard rule on what is
        # sent): without a confirmation for the model configured now, and with no confidential
        # hold, nothing leaves this computer, whatever was posted.
        settings = await load_settings_with_db(s)
        route = await online_route(s, settings, "tier2")
        problem = consent_problem(route, form)
        if problem:
            note = HELD if problem == HELD else "Nothing was sent. Confirm in the dialog first."
            ctx = await _context(request, s, track, range_key, "charts", note=note)
        else:
            ctx = await _context(
                request, s, track, range_key, "charts", ask_model=True, settings=settings
            )
    return templates().TemplateResponse(request, "partials/analytics_summary.html", ctx)


@router.get("/analytics/narrative/online", response_class=HTMLResponse)
async def analytics_online_dialog(request: Request):
    """The ask-first dialog for Rewrite summary on an online model: what is sent (the Tier 2 line
    of the Hosted mode disclosure the operator acknowledged, plus what this call carries), what is
    not, and what confidential mode does. Nothing leaves this computer until Send and rewrite."""
    from kestrel.services.online_consent import destination_id

    track = _track(request)
    range_key = rpt.normalize_range(request.query_params.get("range"))
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        employer = await _employer_state(s, settings)
    tcfg = settings.llm.tiers.get("tier2")
    provider = (tcfg.backend if tcfg else None) or "the online service"
    sends = settings.llm.hosted_mode.disclosure_terms()["sends"]["tier2"]
    ctx = {
        "track": track, "range": range_key, "provider": provider,
        "model": tcfg.model if tcfg else "",
        "dest": destination_id(settings, "tier2"),
        "sends": sends[:1].upper() + sends[1:], "employer": employer,
        "hosted": tier_is_hosted(settings, "tier2"),
    }
    return templates().TemplateResponse(request, "partials/analytics_online.html", ctx)


# ------------------------------------------------------------------------------------ exports
def _csv_response(rows: list[list[Any]], name: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(
        content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _stamp() -> str:
    return datetime.now(UTC).date().isoformat()


def _number_rows(r: dict[str, Any]) -> list[list[Any]]:
    """Every figure on the page as section, label, value, base. A rate under five says "Too few"
    in the note column instead of a percent (hard rule 6)."""
    rows: list[list[Any]] = [
        ["Section", "Label", "Value", "Out of", "Note"],
        ["Page", "Track", r["track"], "", ""],
        ["Page", "Range", r["range_label"], "", ""],
    ]
    for t in r["tiles"]:
        rows.append(["Counts", t["label"], t["n"], t["base"] if t["base"] is not None else "",
                     "Not enough data yet" if t["few"] else (t["sub"] or "")])
    for s in r["standing"]["segments"]:
        rows.append(["Where they stand", s["label"], s["n"], r["standing"]["total"], ""])
    for group, key in (("Funnel, found to read", "a"), ("Funnel, shortlisted onward", "b")):
        for st in r["funnel"][key]:
            note = "Too few" if st["few"] else (
                f"{st['pct']}% of the step before" if st["pct"] is not None else "")
            rows.append([group, st["label"], st["n"],
                         st["base"] if st["base"] is not None else "", note])
    cols = r["bands"]["cols"]
    for b in r["bands"]["rows"]:
        for label, cell in ((cols[1], b["reply"]), (cols[2], b["deep"])):
            rows.append(["Replies by fit score", f"{b['label']}: {label}", cell["n"], cell["base"],
                         "Too few" if cell["few"] else f"{cell['pct']}%"])
    for tab in r["tabs"].values():
        for row in tab["rows"]:
            rows.append([f"Where they come from: {tab['label']}", row["name"], row["replied"],
                         row["sent"], "Too few" if row["few"] else f"{row['rate']}% replied"])
    t = r["timing"]
    rows.append(["Reply timing", "Median days to first reply",
                 t["median"] if t["median"] is not None else "", t["n"],
                 "Too few" if t["median"] is None else "Unanswered excluded"])
    for label, key in (("Waiting under 7 days", "lt7"), ("Waiting 7 to 14 days", "7to14"),
                       ("Waiting over 14 days", "gt14")):
        rows.append(["Reply timing", label, t["waiting"][key], t["waiting"]["total"], ""])
    for b in r["pace"]["bars"]:
        rows.append(["Pace", f"Week of {b['label']}", b["n"], "", ""])
    return rows


async def _held_export(s) -> Response | None:
    """Confidential mode with no employer chosen: nothing could be hidden, so no export leaves
    this computer's screen (every export, not only the applications list)."""
    conf, mapping = await confidential_once(s)
    if conf and not mapping:
        return Response(
            content=("Confidential mode is on but no employer is set, so nothing could be "
                     "hidden. Add your current employer in Profile, then export again."),
            media_type="text/plain; charset=utf-8", status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    return None


@router.get("/analytics/export/numbers.csv")
async def export_numbers(request: Request):
    track = _track(request)
    range_key = rpt.normalize_range(request.query_params.get("range"))
    async with sessionmaker()() as s:
        if (held := await _held_export(s)) is not None:
            return held
        r = await rpt.build_report(s, track, range_key)
        conf, mapping = await confidential_once(s)
        r = redact_json(r, mapping, enabled=conf)
    return _csv_response(_number_rows(r), f"kestrel-analytics-{_stamp()}.csv")


@router.get("/analytics/export/applications.csv")
async def export_applications(request: Request):
    """Every application on the track with its full status history, oldest first."""
    track = _track(request)
    async with sessionmaker()() as s:
        if (held := await _held_export(s)) is not None:
            return held
        redact = await _redactor(s)
        events: dict[int, list[StatusEvent]] = {}
        for ev in (
            await s.execute(select(StatusEvent).order_by(StatusEvent.occurred_at, StatusEvent.id))
        ).scalars():
            events.setdefault(ev.pursuit_id, []).append(ev)
        found = (
            await s.execute(
                select(Pursuit, Opportunity)
                .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
                .where(Pursuit.track == track)
                .order_by(Pursuit.id)
            )
        ).all()
    rows: list[list[Any]] = [["Role", "Company", "Status", "Sent", "Outcome", "Closed", "History"]]
    for p, o in found:
        history = "; ".join(
            f"{(e.occurred_at or '')[:10]} {e.from_status or 'new'} to {e.to_status}"
            for e in events.get(p.id, [])
        )
        rows.append([
            redact(o.title or ""), redact(o.org_name or ""), p.status,
            (p.applied_at or "")[:10], p.outcome or "", (p.closed_at or "")[:10], redact(history),
        ])
    return _csv_response(rows, f"kestrel-applications-{_stamp()}.csv")


@router.get("/analytics/export/summary.pdf")
async def export_pdf(request: Request):
    """The summary and charts as a PDF, rendered on this computer from the same numbers. A
    machine with no Edge or Playwright says so instead of failing (CLAUDE.md platform traps)."""
    from kestrel.docs.render import render_pdf

    track = _track(request)
    range_key = rpt.normalize_range(request.query_params.get("range"))
    async with sessionmaker()() as s:
        if (held := await _held_export(s)) is not None:
            return held
        ctx = await _context(request, s, track, range_key, "charts")
    css_dir = Path(paths.web_static_dir()) / "css"
    ctx["css"] = "\n".join(
        (css_dir / name).read_text(encoding="utf-8")
        for name in ("tokens.css", "components.css", "pages/analytics.css")
    )
    ctx["print_mode"] = True
    html = templates().get_template("analytics_print.html").render(request=request, **ctx)
    try:
        out = await render_pdf(html)
    except Exception as exc:  # no Edge or Playwright on this machine
        log.warning("analytics_pdf_failed", error_type=type(exc).__name__, error=str(exc))
        return Response(
            content=f"The PDF needs Microsoft Edge on this computer: {fixed_reason(exc)}",
            status_code=503, media_type="text/plain; charset=utf-8",
        )
    return Response(
        content=out.pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="kestrel-analytics-{_stamp()}.pdf"'},
    )
