"""About page (SPEC-INTERFACES 2.1 `/about`, D-UI-6, D-UI-125): the mark, the name, the version
and an update check, the five public promises as rows that open a flyout, the data and logs
folders with Copy and Open, the project links, the AI models Kestrel was built with, what it is
built on and inspired by, collapsible component versions and third-party notices, and the
diagnostics download a tester can hand to a reviewer.

Every version shown comes from one place: `kestrel.__version__` for Kestrel and the installed
package metadata for each component, read at request time and never written into a template.
Updating is manual: the page says "Kestrel X.Y.Z is available. Get it from kestrelapp.co" and
nothing is installed on its own.

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
import json
import platform
import re
import sqlite3
import zipfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
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
from kestrel.web.errors import fixed_reason, plain_error
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
SITE_URL = "https://kestrelapp.co"

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


# The five public promises (CLAUDE.md) as the rows on this page. `body` is fixed HTML written
# here and shown in the flyout; `tone` and `icon` pick the badge.
PROMISES: tuple[dict[str, str], ...] = (
    {
        "tone": "good",
        "icon": "gift",
        "title": "Free",
        "summary": "No price, no trial, no ads.",
        "body": "<p>Kestrel is free for noncommercial use. There is no trial, no paid tier and "
        "no advertising.</p><h6>Chipping in</h6><p>If Kestrel helped you land an interview, "
        "you can support its development on Ko-fi. It is never required and nothing unlocks.</p>",
    },
    {
        "tone": "",
        "icon": "eye",
        "title": "You are not the product",
        "summary": "No accounts, no analytics. Nothing about you is sold.",
        "body": "<p>Your profile, roles, ratings and documents live in one folder on this "
        "computer.</p><h6>What Kestrel does reach out to</h6><ul><li>Public job boards and "
        "company career pages, to find openings.</li><li>Public company filings, for hiring "
        "signals.</li><li>Kestrel's own site, to see if a new version exists.</li><li>An online "
        "model, only if you chose one.</li></ul>",
    },
    {
        "tone": "good",
        "icon": "hand",
        "title": "Never applies for you",
        "summary": "It never applies, emails or messages anyone.",
        "body": "<p>Kestrel drafts, recommends and hands over. You press Apply on the "
        "employer's site yourself, and you send every email and message yourself.</p>"
        "<h6>LinkedIn</h6><p>Kestrel does not search it. A LinkedIn posting can only be pasted "
        "in by hand.</p>",
    },
    {
        "tone": "warn",
        "icon": "cloud",
        "title": "Asks before going online",
        "summary": "You see what would be sent, then decide.",
        "body": "<p>Everything runs on this computer by default. An online model needs your own "
        "key.</p><h6>Each time</h6><p>Kestrel lists what will be sent and what it will cost, "
        "then waits for your OK. Saying no changes nothing else.</p><h6>Change it</h6>"
        "<p>Models and Settings show where each step runs.</p>",
    },
    {
        "tone": "",
        "icon": "code",
        "title": "Source is public",
        "summary": "Under PolyForm Noncommercial 1.0.0.",
        "body": "<p>Anyone can read the code, run it and change it for noncommercial use. "
        "Commercial use needs the author's permission.</p><h6>Why it matters</h6><p>You do not "
        "have to take these promises on trust. You can read what the code does.</p>",
    },
)

# The AI models that helped write and review Kestrel (CLAUDE.md, "What this is").
BUILT_WITH: tuple[str, ...] = (
    "Claude",
    "ChatGPT",
    "Qwen",
    "DeepSeek",
    "NVIDIA Nemotron",
    "MiniMax M3",
    "LongCat",
    "Kimi K2.7",
    "MiMo",
)

# The packages whose versions the Component versions list shows, by the name pip knows them.
_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("FastAPI", "fastapi"),
    ("SQLAlchemy", "sqlalchemy"),
    ("Pydantic", "pydantic"),
    ("pywebview", "pywebview"),
    ("scikit-learn", "scikit-learn"),
    ("Playwright (makes the PDFs through Microsoft Edge)", "playwright"),
)


def _os_label() -> str:
    """The operating system in the words people use, from the standard library."""
    system = platform.system()
    if system == "Windows":
        return f"Windows {platform.release()}"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0] or platform.release()}"
    return f"{system} {platform.release()}".strip()


def _running_port() -> str:
    """The port this instance serves on, per runtime.json. Never raises: About must render when
    the file is absent (a test app, or an instance started without writing it)."""
    try:
        return str(json.loads(paths.runtime_json().read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return "not recorded"


def _hardware_profile() -> str | None:
    """The hardware profile `kestrel setup` chose, as one line, or None when there is none. The
    profiles module is imported defensively: About must render on a build without it."""
    try:
        from kestrel.llm.profiles import load_active_profile

        active = load_active_profile()
    except Exception:  # a missing module or a corrupt record is a missing row, never a 500
        return None
    return f"{active.profile} ({active.reason})" if active else None


def component_versions() -> list[tuple[str, str]]:
    """Label and version for each row of the Component versions list. Python and the packages come
    from the running interpreter and installed metadata; a package that is not installed (a
    minimal test environment) skips its row. Ollama is looked up separately (/about/ollama)
    because it asks a program on this computer."""
    rows: list[tuple[str, str]] = [("Python", platform.python_version())]
    for label, dist in _COMPONENTS:
        try:
            rows.append((label, pkg_version(dist)))
        except PackageNotFoundError:
            continue
    rows.append(("Database (SQLite)", sqlite3.sqlite_version))
    rows.append(("Kestrel is serving on port", _running_port()))
    profile = _hardware_profile()
    if profile:
        rows.append(("Hardware profile", profile))
    return rows


@router.get("/about/notices", response_class=PlainTextResponse)
async def notices() -> PlainTextResponse:
    """Every bundled library's license text (D-LIC-1): the file the build generated, or a live
    build in a source checkout (a thread: it reads every package's license files)."""
    from kestrel.services.notices import notices_text

    return PlainTextResponse(await asyncio.to_thread(notices_text))


@router.get("/about", response_class=HTMLResponse)
async def page(request: Request):
    from kestrel.services import updater
    from kestrel.web.pages.updates import relative_last_checked

    # The top card's Check for updates button shows when the last check ran, as Settings does. A
    # database that cannot be read yet (first launch, mid-restore) shows "never" rather than a 500.
    try:
        async with sessionmaker()() as s:
            state = await updater.load_state(s)
            update_last_checked = relative_last_checked(state.last_checked_at)
    except (SQLAlchemyError, sqlite3.Error) as exc:
        log.warning("about_last_checked_failed", error=f"{type(exc).__name__}: {exc}")
        update_last_checked = relative_last_checked(None)
    ctx = {
        "title": "About",
        "query": dict(request.query_params),
        "version": kestrel.__version__,
        "os_label": _os_label(),
        "frozen": paths.is_frozen(),
        "data_home": str(paths.data_home()),
        "logs_dir": str(paths.logs_dir()),
        "github_url": GITHUB_URL,
        # Release notes are the changelog in the source repository, newest version first.
        "changelog_url": f"{GITHUB_URL}/blob/main/CHANGELOG.md",
        "update_last_checked": update_last_checked,
        "kofi_url": KOFI_URL,
        "site_url": SITE_URL,
        "promises": PROMISES,
        "built_with": BUILT_WITH,
        "built_on": BUILT_ON,
        "inspired_by": INSPIRED_BY,
        "components": component_versions(),
        "bundle_contents": diagnostics.CONTENTS,
        "bundle_excluded": diagnostics.EXCLUDED,
        "desktop": desktop_bridge.can_reveal(),
        "can_open_folder": desktop_bridge.can_open_folder(),
    }
    return templates().TemplateResponse(request, "about.html", ctx)


async def _effective() -> tuple[Settings, dict[str, str] | None, bool]:
    """The effective settings (setting-table rows applied, as every page reads them), the
    confidential-mode redaction map (None when there is none), and `conservative`: True when the
    bundle cannot be sure it hides the employer, so the quarantine excerpts are left out. That is
    the case when confidential mode is on but no employer is chosen (nothing to redact with) and
    when the database cannot answer, since the mode itself is then unknown. The bundle still
    masks tokens regardless."""
    try:
        async with sessionmaker()() as session:
            settings = await load_settings_with_db(session)
            mapping = await redaction_map(session) if settings.confidential_mode else {}
    except (SQLAlchemyError, OSError):
        return get_settings(), None, True
    return settings, (mapping or None), bool(settings.confidential_mode and not mapping)


@router.get("/about/ollama", response_class=HTMLResponse)
async def ollama_row():
    """The Ollama row of Component versions: asks the program on this computer for its version
    (a thread, with a short timeout), so the page itself never waits on it."""
    from kestrel import setup

    url = get_settings().llm.ollama_native_url
    found = await asyncio.to_thread(setup.ollama_version, url, 1.5)
    return HTMLResponse(
        '<tr data-testid="about-ollama"><td>Ollama (on this computer)</td>'
        f"<td>{html.escape(found) if found else 'not found'}</td></tr>"
    )


@router.post("/about/folder/open", response_class=HTMLResponse)
async def open_folder(which: str = Form("")):
    """Ask the desktop shell to open the data or the logs folder. Only those two, never a path
    from the request."""
    target = {"data": paths.data_home(), "logs": paths.logs_dir()}.get(which)
    if target is None:
        return HTMLResponse(
            '<span class="ab-note" role="status" data-testid="folder-open">'
            "That is not a folder Kestrel can open.</span>"
        )  # 200, not 404: htmx 2 does not swap an error response
    if await asyncio.to_thread(desktop_bridge.request_open_folder, target):
        return HTMLResponse(
            '<span class="ab-note" role="status" data-testid="folder-open">Opened.</span>'
        )
    return HTMLResponse(
        '<span class="ab-note is-warn" role="status" data-testid="folder-open">'
        f"Could not open it. It is at {html.escape(str(target))}.</span>"
    )


@router.get("/about/diagnostics")
async def download_diagnostics():
    """Stream a fresh diagnostics zip; the temp copy is deleted after the bytes have gone out."""
    settings, redaction, conservative = await _effective()
    try:
        archive = await asyncio.to_thread(
            diagnostics.build_bundle_to_temp, settings, redaction=redaction,
            conservative=conservative,
        )
    except OSError as exc:
        log.warning("diagnostics_bundle_failed", error_type=type(exc).__name__, error=str(exc))
        return HTMLResponse(
            f'<div class="ab-saved is-warn">Could not build the diagnostics bundle: '
            f"{html.escape(fixed_reason(exc))}</div>",
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
    settings, redaction, conservative = await _effective()
    errors: list[str] = []
    for folder in _save_dirs():
        try:
            archive = await asyncio.to_thread(
                diagnostics.build_bundle, settings, out_dir=folder, redaction=redaction,
                conservative=conservative,
            )
        except OSError as exc:
            log.warning(
                "diagnostics_save_failed",
                folder=str(folder),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            errors.append(f"{folder}: {plain_error(exc)}")
            continue
        except (sqlite3.Error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            # Not about the folder (the database or the archive itself): trying the next folder
            # would fail the same way, so say so now rather than leave the button looking dead.
            log.warning(
                "diagnostics_build_failed",
                folder=str(folder),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return _saved_partial(
                request,
                ok=False,
                error=(
                    f"Could not build the diagnostics file "
                    f"({type(exc).__name__}: {plain_error(exc)})."
                ),
            )
        log.info("diagnostics_saved", path=str(archive))
        return _saved_partial(
            request,
            ok=True,
            path=str(archive),
            name=archive.name,
            can_reveal=desktop_bridge.can_reveal(),
        )
    return _saved_partial(request, ok=False, error="; ".join(errors))


@router.post("/about/diagnostics/reveal", response_class=HTMLResponse)
async def reveal_diagnostics(request: Request, name: str = Form("")):
    """Ask the desktop shell to show a zip this page saved in Explorer. A local action on the
    operator's own file; anything that is not such a file is refused."""
    target = None
    if _SAVED_NAME.match(name):
        target = await asyncio.to_thread(
            lambda: next((d / name for d in _save_dirs() if (d / name).is_file()), None))
    if target is None:
        return HTMLResponse(
            '<div class="ab-saved is-warn" role="status" data-testid="diagnostics-reveal">'
            "That file is not one Kestrel saved here.</div>"
        )  # 200, not 404: htmx 2 does not swap an error response, so the box would never show
    if await asyncio.to_thread(desktop_bridge.request_reveal, target):
        return HTMLResponse(
            '<span class="ab-note" role="status" data-testid="diagnostics-reveal">'
            "Opened the folder.</span>"
        )
    return HTMLResponse(
        '<div class="ab-saved is-warn" role="status" data-testid="diagnostics-reveal">'
        f"Could not open the folder. The file is at {html.escape(str(target))}.</div>"
    )
