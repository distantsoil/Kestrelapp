"""The page shown when Kestrel could not load a page (D-UI-152).

A browser navigation that ends in an unhandled exception gets a calm page instead of a bare
"Internal Server Error": your roles and settings are safe, Try again, and the details behind a
"Show details" toggle for a bug report. Everything the details say is plain: the page, the time,
what happened in the words of web/errors.plain_error (never the text of an unknown exception,
which can carry a path, a URL or a key), and the Kestrel and system version. The real exception is
logged with its context by the handler. A request that is not a page navigation (htmx, JSON, an
asset) keeps the default answer, because a fragment swap or an API client cannot use a page."""

from __future__ import annotations

import platform
from datetime import datetime
from html import escape

import structlog
from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

import kestrel
from kestrel.web.errors import plain_error

log = structlog.get_logger()


def wants_page(request: Request) -> bool:
    """True for a full-page browser navigation: not htmx, and the client accepts HTML."""
    if request.headers.get("HX-Request"):
        return False
    return "text/html" in request.headers.get("accept", "")


def page_name(path: str) -> str:
    """The page as the nav names it, else the path itself."""
    from kestrel.web.deps import nav_title

    return nav_title(path) or path


def _system() -> str:
    try:
        return f"{platform.system()} {platform.release()}".strip()
    except Exception:  # a label is never worth failing the error page
        return "this computer"


def details_text(request: Request, exc: BaseException, *, now: datetime | None = None) -> str:
    from kestrel.date_display import format_display

    when = now or datetime.now().astimezone()
    return "\n".join([
        f"Page: {page_name(request.url.path)}",
        "Time: " + format_display(when, "short", kind="datetime",
                                  time_style="24h_seconds", separator=", "),
        f"What happened: {plain_error(exc)}",
        f"Kestrel {kestrel.__version__} on {_system()}",
    ])


def render(request: Request, exc: BaseException) -> Response:
    """The error page (status 500). If the page itself cannot render, a small static one."""
    text = details_text(request, exc)
    try:
        from kestrel.web.deps import templates

        return templates().TemplateResponse(
            request, "error.html", {"details": text, "retry_href": request.url.path or "/"},
            status_code=500,
        )
    except Exception as render_exc:  # the error page must never raise
        log.warning("error_page_render_failed", error_type=type(render_exc).__name__)
        body = (
            "<!doctype html><meta charset=utf-8><title>Kestrel</title>"
            "<h1>Kestrel couldn't load this page</h1>"
            "<p>Your roles and settings are safe on this computer. Try again.</p>"
            f"<pre>{escape(text)}</pre>"
        )
        return HTMLResponse(body, status_code=500)


async def handle(request: Request, exc: Exception) -> Response:
    log.error(
        "unhandled_error", path=request.url.path, error_type=type(exc).__name__,
        exc_info=exc,
    )
    if wants_page(request):
        return render(request, exc)
    return PlainTextResponse("Internal Server Error", status_code=500)  # Starlette's own
