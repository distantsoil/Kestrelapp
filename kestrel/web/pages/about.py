"""About page (SPEC-INTERFACES 2.1 `/about`, D-UI-6): the mark, the name, the version, the build
facts, what Kestrel is and its privacy stance in a few sentences, the project links (source and
issues on GitHub, Ko-fi for anyone who wants to support it), the prior art it stands on, and the
diagnostics download a tester can hand to a reviewer.

The diagnostics zip is built by services/diagnostics in a thread, streamed with FileResponse, and
removed by a BackgroundTask once the bytes have gone out, exactly as the Backup page does. The
confidential-mode redaction map is resolved here on the event loop (it is a database read) and
passed into the build so the bundle's logs and quarantine rows carry the operator's placeholder,
never the employer's name.

In the desktop app the zip is not a browser download (WebView2 would show Edge's downloads
flyout): POST /about/diagnostics/save writes it to the Downloads folder (paths.downloads_dir,
falling back to Kestrel's exports folder) and answers with an inline "Saved to" note whose Open
folder button asks the shell, through desktop_bridge, to show the file in Explorer (D-UI-41). A
plain browser (`kestrel serve`) has no shell to ask and keeps the download.

Help (/help) used to answer at this path as well. It now has its own module (pages/help.py);
its router is included from here because app.py registers page modules by name and that list
is not this page's to edit."""

from __future__ import annotations

import asyncio
import html
import platform
import re
import sqlite3
import zipfile
from pathlib import Path

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

import kestrel
from kestrel import paths
from kestrel.config import Settings, get_settings
from kestrel.services import diagnostics
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import desktop_bridge
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.pages import help as help_page
from kestrel.web.redact import redaction_map

log = structlog.get_logger()
router = APIRouter()
router.include_router(help_page.router)

# Only a file this page saved can be revealed: a bare name of this shape, in one of the folders
# the save writes to.
_SAVED_NAME = re.compile(r"^kestrel-diagnostics-[A-Za-z0-9.+_-]+\.zip$")

GITHUB_URL = "https://github.com/distantsoil/Kestrelapp"
KOFI_URL = "https://ko-fi.com/distantsoil"

# The prior art REFERENCES.md credits, split by how Kestrel uses it (D-LIC-1). "Built on" is
# software Kestrel ships, under its license (all MIT); "Inspired by" is ideas only, no code or text
# taken, checked by a 12-word overlap scan against each repository on 2026-10-13.
BUILT_ON: tuple[dict[str, str], ...] = (
    {
        "name": "JobSpy",
        "url": "https://github.com/speedyapply/JobSpy",
        "license": "MIT",
        "what": "the job board ingestion layer, bundled as is",
    },
)
INSPIRED_BY: tuple[dict[str, str], ...] = (
    {
        "name": "career-ops",
        "url": "https://github.com/santifer/career-ops",
        "license": "MIT",
        "what": "the evaluation rubric, the scoring dimensions, the legitimacy check and the "
        "story bank concept",
    },
    {
        "name": "parametric-resume-builder",
        "url": "https://github.com/ConstanzaSchibber/parametric-resume-builder",
        "license": "MIT",
        "what": "the parametric resume: slots holding pre-authored variants, selected rather "
        "than generated",
    },
)
CREDITS = BUILT_ON + INSPIRED_BY


@router.get("/about/notices", response_class=PlainTextResponse)
async def notices() -> PlainTextResponse:
    """Every bundled library's license text (D-LIC-1): the file the build generated, or a live
    build in a source checkout (a thread: it reads every package's license files)."""
    from kestrel.services.notices import notices_text

    return PlainTextResponse(await asyncio.to_thread(notices_text))


@router.get("/about", response_class=HTMLResponse)
async def page(request: Request):
    from kestrel.services import updater
    from kestrel.web.pages.updates import format_last_checked

    # The top card's Check for updates button shows when the last check ran, as Settings does. A
    # database that cannot be read yet (first launch, mid-restore) shows "never" rather than a 500.
    try:
        async with sessionmaker()() as s:
            update_last_checked = format_last_checked(
                (await updater.load_state(s)).last_checked_at
            )
    except (SQLAlchemyError, sqlite3.Error) as exc:
        log.warning("about_last_checked_failed", error=f"{type(exc).__name__}: {exc}")
        update_last_checked = format_last_checked(None)
    ctx = {
        "title": "About",
        "query": dict(request.query_params),
        "version": kestrel.__version__,
        "frozen": paths.is_frozen(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "data_home": str(paths.data_home()),
        "github_url": GITHUB_URL,
        # Release notes are the changelog in the source repository, newest version first.
        "changelog_url": f"{GITHUB_URL}/blob/main/CHANGELOG.md",
        "update_last_checked": update_last_checked,
        "kofi_url": KOFI_URL,
        "built_on": BUILT_ON,
        "inspired_by": INSPIRED_BY,
        "bundle_contents": diagnostics.CONTENTS,
        "bundle_excluded": diagnostics.EXCLUDED,
        "desktop": desktop_bridge.can_reveal(),
    }
    return templates().TemplateResponse(request, "about.html", ctx)


async def _effective() -> tuple[Settings, dict[str, str] | None]:
    """The effective settings (setting-table rows applied, as every page reads them) and the
    confidential-mode redaction map, or None for the map when the mode is off. When the database
    cannot answer (a fresh home with no migration yet) the YAML settings serve and no map: the
    bundle still masks tokens regardless."""
    try:
        async with sessionmaker()() as session:
            settings = await load_settings_with_db(session)
            mapping = await redaction_map(session) if settings.confidential_mode else {}
    except (SQLAlchemyError, OSError):
        return get_settings(), None
    return settings, (mapping or None)


@router.get("/about/diagnostics")
async def download_diagnostics():
    """Stream a fresh diagnostics zip; the temp copy is deleted after the bytes have gone out."""
    settings, redaction = await _effective()
    try:
        archive = await asyncio.to_thread(
            diagnostics.build_bundle_to_temp, settings, redaction=redaction
        )
    except OSError as exc:
        return HTMLResponse(
            f'<div class="warnbox">Could not build the diagnostics bundle: '
            f"{html.escape(str(exc))}</div>",
            status_code=500,
        )
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
        # Bound at request time so a wrapper installed on the service module (a test confirming
        # the temp archive is gone) is what actually runs.
        background=BackgroundTask(diagnostics.discard_bundle, archive),
    )


def _save_dirs() -> list[Path]:
    """Where a save goes, in order: the Downloads folder, then Kestrel's exports folder."""
    first = paths.downloads_dir()
    return [first] if first == paths.exports_dir() else [first, paths.exports_dir()]


def _saved_partial(request: Request, **ctx) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/diagnostics_saved.html", ctx)


@router.post("/about/diagnostics/save", response_class=HTMLResponse)
async def save_diagnostics(request: Request):
    """Desktop app: write the zip to Downloads (or Kestrel's exports folder when Downloads cannot
    be written) and say where, inline. No browser download, so no WebView2 flyout (D-UI-41)."""
    settings, redaction = await _effective()
    errors: list[str] = []
    for folder in _save_dirs():
        try:
            archive = await asyncio.to_thread(
                diagnostics.build_bundle, settings, out_dir=folder, redaction=redaction
            )
        except OSError as exc:
            log.warning("diagnostics_save_failed", folder=str(folder), error=str(exc))
            errors.append(f"{folder}: {exc}")
            continue
        except (sqlite3.Error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            # Not about the folder (the database or the archive itself): trying the next folder
            # would fail the same way, so say so now rather than leave the button looking dead.
            log.warning("diagnostics_build_failed", folder=str(folder),
                        error_type=type(exc).__name__, error=str(exc))
            return _saved_partial(
                request, ok=False,
                error=f"Could not build the diagnostics file ({type(exc).__name__}: {exc}).",
            )
        log.info("diagnostics_saved", path=str(archive))
        return _saved_partial(
            request, ok=True, path=str(archive), name=archive.name,
            can_reveal=desktop_bridge.can_reveal(),
        )
    return _saved_partial(request, ok=False, error="; ".join(errors))


@router.post("/about/diagnostics/reveal", response_class=HTMLResponse)
async def reveal_diagnostics(request: Request, name: str = Form("")):
    """Ask the desktop shell to show a zip this page saved in Explorer. A local action on the
    operator's own file; anything that is not such a file is refused."""
    target = None
    if _SAVED_NAME.match(name):
        target = next((d / name for d in _save_dirs() if (d / name).is_file()), None)
    if target is None:
        return HTMLResponse(
            '<div class="warnbox" role="status" data-testid="diagnostics-reveal">'
            "That file is not one Kestrel saved here.</div>"
        )  # 200, not 404: htmx 2 does not swap an error response, so the box would never show
    if desktop_bridge.request_reveal(target):
        return HTMLResponse(
            '<span class="muted" role="status" data-testid="diagnostics-reveal">'
            "Opened the folder.</span>"
        )
    return HTMLResponse(
        '<div class="warnbox" role="status" data-testid="diagnostics-reveal">'
        f"Could not open the folder. The file is at {html.escape(str(target))}.</div>"
    )
