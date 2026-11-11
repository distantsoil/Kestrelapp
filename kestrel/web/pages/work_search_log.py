"""Work search log export (D-EXPORT-1, SPEC-MODULES 7 "Work search log").

  GET /work-search-log/panel                   the sheet body on Applications: range and count
  GET /analytics/export/work-search-log.csv    Role, Company, Applied for the range (local only)

The count in the sheet and the rows in the file come from one function, so they cannot differ.
Nothing is sent anywhere (hard rule 1); the file is saved through the shell's Save dialog."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from kestrel.date_display import format_display
from kestrel.services.work_search_log import QUICK_PICKS, WorkLog, collect, pick_range
from kestrel.web.auth import LOCAL_DOWNLOAD
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.pages.analytics import _csv_response, _redactor
from kestrel.web.redact import confidential_once

router = APIRouter()

BAD_RANGE = "Choose a start date that is on or before the end date."


def _today() -> date:
    return date.today()


def _range(params: Any) -> tuple[date, date] | None:
    """The `from` and `to` dates, or this week's when neither is given; None when unusable."""
    raw_from, raw_to = params.get("from") or "", params.get("to") or ""
    if not raw_from and not raw_to:
        return pick_range(params.get("pick") or "this_week", _today())
    try:
        start, end = date.fromisoformat(raw_from), date.fromisoformat(raw_to)
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _label(start: date, end: date) -> str:
    return f"{format_display(start, 'short')} to {format_display(end, 'short')}"


def _redacted(log: WorkLog, redact) -> list[list[Any]]:
    rows: list[list[Any]] = [["Role", "Company", "Applied"]]
    rows += [[redact(r.role), redact(r.company), r.applied] for r in log.rows]
    if log.unknown:
        rows.append(["Date unknown", "", ""])
        rows += [[redact(r.role), redact(r.company), ""] for r in log.unknown]
    return rows


def _typed(raw: str) -> str:
    """A date the person typed, as the date input wants it back; "" when it is not a date."""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


@router.get("/work-search-log/panel", response_class=HTMLResponse)
async def panel(request: Request):
    span = _range(request.query_params)
    start, end = span or pick_range("this_week", _today())
    start_text, end_text = start.isoformat(), end.isoformat()
    if span is None:
        # A bad range keeps what was typed beside the message; nothing is swapped for this week's.
        start_text = _typed(request.query_params.get("from") or "")
        end_text = _typed(request.query_params.get("to") or "")
    log = None
    async with sessionmaker()() as s:
        if span is not None:
            log = await collect(s, start, end)
        conf, mapping = await confidential_once(s)
    # A quick pick is lit only while the dates are its own; typed dates light none.
    typed = bool(request.query_params.get("from") or request.query_params.get("to"))
    pick = "" if typed else request.query_params.get("pick") or "this_week"
    return templates().TemplateResponse(request, "partials/work_search_log.html", {
        "start": start_text, "end": end_text, "log": log, "error": BAD_RANGE
        if span is None else None, "label": _label(start, end), "picks": QUICK_PICKS,
        "pick": pick, "no_employer": bool(conf and not mapping),
    })


@router.get("/analytics/export/work-search-log.csv", dependencies=[LOCAL_DOWNLOAD])
async def export_work_search_log(request: Request):
    span = _range(request.query_params)
    if span is None:
        return Response(content=BAD_RANGE, media_type="text/plain; charset=utf-8",
                        status_code=400, headers={"Cache-Control": "no-store"})
    start, end = span
    async with sessionmaker()() as s:
        log = await collect(s, start, end)
        redact = await _redactor(s)
    return _csv_response(_redacted(log, redact),
                         f"kestrel-work-search-log-{start.isoformat()}-to-{end.isoformat()}.csv")
