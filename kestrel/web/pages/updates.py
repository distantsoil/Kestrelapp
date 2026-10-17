"""Update checker UI (SPEC-INTERFACES 1.5, D-UPD-1..5): the banner, the release-notes dialog, the
manual check, the one-time "what changed" and disclosure cards.

Two public promises bind this module. Kestrel asks and tells; it never downloads or installs
anything, so "Get the update" is a plain link to the download page and the operator does the rest.
And nothing about the user leaves the machine: the link carries no query string. Every fragment
is rendered by Jinja with autoescape on; the only value marked safe is the output of
updater.render_notes_html, which escapes its input first."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import kestrel
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.services import updater
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates

log = structlog.get_logger()
router = APIRouter()

ENABLED_KEY = "updates.enabled"
CHANGED_EVENT = "updates-changed"


def format_last_checked(iso: str | None) -> str:
    """'never' or a short local time. An unreadable stamp reads as never, not as an error."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "never"
    local = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone()
    return f"{local:%b} {local.day}, {local.year} at {local:%H:%M}"


def _download_page(state: updater.UpdateState) -> str:
    """Validated again at render time (defense in depth: old rows predate the check)."""
    return updater.safe_download_page(state.download_page) or updater.DOWNLOAD_PAGE


def _feed(state: updater.UpdateState) -> updater.LatestFeed | None:
    if not state.cached_feed:
        return None
    try:
        return updater.LatestFeed.model_validate(state.cached_feed)
    except ValidationError:
        return None


def _notes(releases: list[updater.ReleaseNote]) -> list[dict]:
    # render_notes_html escapes first, so Markup here is not an injection path.
    return [
        {
            "version": r.version,
            "date": r.date,
            "html": Markup(updater.render_notes_html(r.notes_md)),
        }
        for r in releases
    ]


def _fragment(request: Request, name: str, ctx: dict, *, trigger: bool = False) -> HTMLResponse:
    resp = templates().TemplateResponse(request, f"partials/{name}", ctx)
    if trigger:
        resp.headers["HX-Trigger"] = CHANGED_EVENT
    return resp


async def _enabled(s: AsyncSession) -> bool:
    return (await load_settings_with_db(s)).updates.enabled


async def _set_row(s: AsyncSession, key: str, value) -> None:
    row = await s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def _available(s: AsyncSession) -> tuple[str | None, updater.UpdateState]:
    state = await updater.load_state(s)
    if not await _enabled(s):
        return None, state
    return updater.banner_version(state, kestrel.__version__, utcnow_iso()), state


@router.get("/updates/banner", response_class=HTMLResponse)
async def banner(request: Request):
    async with sessionmaker()() as s:
        version, state = await _available(s)
    ctx = {"version": version, "download_page": _download_page(state)}
    return _fragment(request, "update_banner.html", ctx)


@router.get("/updates/details", response_class=HTMLResponse)
async def details(request: Request):
    async with sessionmaker()() as s:
        version, state = await _available(s)
    feed = _feed(state)
    releases = updater.pending_releases(feed, kestrel.__version__) if feed else []
    ctx = {
        "version": version or state.latest_version,
        "notes": _notes(releases),
        "download_page": _download_page(state),
    }
    return _fragment(request, "update_details.html", ctx)


@router.post("/updates/check", response_class=HTMLResponse)
async def check(request: Request):
    """Operator-triggered check. Never shows exception text: check_now already logs the cause."""
    current = kestrel.__version__
    outcome, version = "unreachable", None
    last_checked = "never"
    try:
        async with sessionmaker()() as s:
            cfg = (await load_settings_with_db(s)).updates
            state = await updater.check_now(s, feed_url=cfg.feed_url, current=current)
        last_checked = format_last_checked(state.last_checked_at)
        if state.last_check_ok:
            latest = state.latest_version
            if latest and updater.is_newer(latest, current):
                outcome, version = "available", latest
            else:
                outcome = "current"
    except Exception as exc:  # a check must never 500 the page; the log keeps the cause
        log.warning("update_check_ui_failed", error_type=type(exc).__name__)
    ctx = {"outcome": outcome, "version": version, "current": current, "last_checked": last_checked}
    return _fragment(request, "update_check_result.html", ctx, trigger=True)


@router.post("/updates/remind", response_class=HTMLResponse)
async def remind(request: Request):
    async with sessionmaker()() as s:
        await updater.remind_later(s)
    return _fragment(request, "update_banner.html", {"version": None}, trigger=True)


@router.post("/updates/skip", response_class=HTMLResponse)
async def skip(request: Request, version: str = Form("")):
    version = version.strip()
    if version and updater.parse_version(version) is not None:
        async with sessionmaker()() as s:
            await updater.skip_version(s, version)
    return _fragment(request, "update_banner.html", {"version": None}, trigger=True)


@router.get("/updates/whats-new-card", response_class=HTMLResponse)
async def whats_new_card(request: Request):
    current = kestrel.__version__
    async with sessionmaker()() as s:
        state = await updater.load_state(s)
    prev = state.upgraded_from
    if not prev or prev == current:
        return HTMLResponse("")
    feed = _feed(state)
    releases = []
    if feed:
        releases = [
            r for r in feed.releases
            if updater.is_newer(r.version, prev) and not updater.is_newer(r.version, current)
        ]
        releases.sort(key=lambda r: updater.parse_version(r.version) or (0, 0, 0, ()), reverse=True)
    ctx = {"current": current, "notes": _notes(releases), "download_page": _download_page(state)}
    return _fragment(request, "update_whats_new.html", ctx)


@router.post("/updates/whats-new/dismiss", response_class=HTMLResponse)
async def whats_new_dismiss():
    async with sessionmaker()() as s:
        state = await updater.load_state(s)
        state.upgraded_from = None
        await updater.save_state(s, state)
    return HTMLResponse("")


@router.get("/updates/disclosure-card", response_class=HTMLResponse)
async def disclosure_card(request: Request):
    async with sessionmaker()() as s:
        state = await updater.load_state(s)
    if state.disclosure_shown:
        return HTMLResponse("")
    return _fragment(request, "update_disclosure.html", {})


async def _ack_disclosure(*, turn_off: bool) -> None:
    async with sessionmaker()() as s:
        state = await updater.load_state(s)
        state.disclosure_shown = True
        await updater.save_state(s, state)
        if turn_off:
            await _set_row(s, ENABLED_KEY, False)
            await s.commit()


@router.post("/updates/disclosure/ok", response_class=HTMLResponse)
async def disclosure_ok():
    await _ack_disclosure(turn_off=False)
    return HTMLResponse("")


@router.post("/updates/disclosure/off", response_class=HTMLResponse)
async def disclosure_off():
    await _ack_disclosure(turn_off=True)
    return HTMLResponse("")
