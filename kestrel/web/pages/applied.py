"""Settings > Applied (SPEC.md 5.6, SPEC-INTERFACES 2.1): import a tracker of applications made
before Kestrel. GET renders the instructions and the paste box; POST /review parses the text
and shows every row with what was read, mapped and matched; POST /import re-parses the SAME
text (nothing is held server-side between the two) and writes only the rows ticked in the
review. The written rows are hidden opportunities plus pursuits, so the tracking board gains
them and re-posts of them are recognized (kestrel.ingest.normalize.suppress_if_applied)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from kestrel.services import applied_import as svc
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.uploads import large_form

router = APIRouter()
log = structlog.get_logger()

MAX_TEXT = 2_000_000  # bytes of pasted or uploaded text; a tracker is kilobytes

EXAMPLE_MD = (
    "| Company | Role | Date | Source | Status | Notes |\n"
    "|---|---|---|---|---|---|\n"
    "| Acme Corp | VP Information Technology | 2026-09-30 | LinkedIn | No response | "
    "Easy Apply, resume v12 |\n"
    "| Northwind | Director of IT | 2026-10-02 | Company site | In conversation | "
    "Recruiter screen 10/08 |"
)
EXAMPLE_CSV = (
    "Company,Role,Date,Source,Status,Notes\n"
    "Acme Corp,VP Information Technology,2026-09-30,LinkedIn,No response,Easy Apply\n"
    "Northwind,Director of IT,2026-10-02,Company site,In conversation,Recruiter screen 10/08"
)


def _ctx(**extra) -> dict:
    return {
        "aliases": svc.COLUMN_ALIASES,
        "required": svc.REQUIRED,
        "status_words": svc.STATUS_WORDS,
        "example_md": EXAMPLE_MD,
        "example_csv": EXAMPLE_CSV,
        "status_options": svc.status_options,
        **extra,
    }


def _partial(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/applied_import.html", ctx)


async def _text_from(request: Request) -> tuple[str, str | None]:
    """The pasted text, or the uploaded file's text (UTF-8, BOM tolerated). Returns (text,
    error)."""
    form = await large_form(request, svc.MAX_ROWS)
    upload = form.get("file")
    text = str(form.get("text") or "")
    if upload is not None and hasattr(upload, "read"):
        from kestrel.web.uploads import UploadTooLarge, read_upload

        try:
            # Size-limited and always closed (D-RR1-4): an oversize file is refused before it is
            # fully buffered, and the temp file never outlives the request.
            data = await read_upload(upload, limit=MAX_TEXT, too_large=UploadTooLarge)
        except UploadTooLarge:
            return "", "That file is larger than 2 MB; a tracker is a few kilobytes."
        if data:
            text = data.decode("utf-8-sig", errors="replace")
    if len(text.encode("utf-8")) > MAX_TEXT:
        return "", "That text is larger than 2 MB; a tracker is a few kilobytes."
    return text, None


@router.get("/settings/applied", response_class=HTMLResponse)
async def applied_panel(request: Request):
    return _partial(request, _ctx())


@router.post("/settings/applied/review", response_class=HTMLResponse)
async def applied_review(request: Request):
    text, error = await _text_from(request)
    if error:
        return _partial(request, _ctx(error=error, text=text))
    if not text.strip():
        return _partial(request, _ctx(error="Paste the table or choose a file first.", text=text))
    async with sessionmaker()() as s:
        rev = await svc.review(s, text)
    return _partial(request, _ctx(review=rev, text=text))


def _choices(form, rows: list[svc.ImportRow]) -> dict[int, svc.RowChoice]:
    out: dict[int, svc.RowChoice] = {}
    for r in rows:
        i = r.index
        out[i] = svc.RowChoice(
            include="on" in form.getlist(f"include_{i}"),
            status=str(form.get(f"status_{i}") or "") or None,
            allow_reposts="on" in form.getlist(f"reposts_{i}"),
            update_existing="on" in form.getlist(f"update_{i}"),
        )
    return out


@router.post("/settings/applied/import", response_class=HTMLResponse)
async def applied_import(request: Request):
    form = await large_form(request, svc.MAX_ROWS)
    text = str(form.get("text") or "")
    if not text.strip():
        return _partial(request, _ctx(error="Nothing to import: review a table first."))
    async with sessionmaker()() as s:
        rev = await svc.review(s, text)
        if rev.errors or not rev.rows:
            return _partial(request, _ctx(review=rev, text=text))
        try:
            result = await svc.commit(s, rev, _choices(form, rev.rows))
        except (SQLAlchemyError, ValueError) as exc:  # nothing partial was committed
            await s.rollback()
            log.warning("applied_import_failed", error=f"{type(exc).__name__}: {exc}")
            return _partial(
                request,
                _ctx(
                    review=rev,
                    text=text,
                    error=f"Nothing was imported: the write failed ({type(exc).__name__}). "
                    "Your review is still below; fix the row named in the log and import again.",
                ),
            )
    return _partial(request, _ctx(result=result))
