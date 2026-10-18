"""Backup page: download a backup archive, export or import the operator's profile.yml.

Everything here reuses kestrel.services.backup so the archive the browser downloads is byte-for-
byte what `kestrel backup` writes, and the same exclusions hold (never .env, never API keys).
Restore is deliberately NOT here: it lives on Settings > LAN & backup (`POST /settings/restore`)
and this page links to it rather than duplicating a destructive route.

The archive is built in a private temp directory off the event loop, streamed with FileResponse,
and removed by a BackgroundTask once the response has been sent."""

from __future__ import annotations

import asyncio
import html

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from kestrel import paths
from kestrel.services import backup as backup_service
from kestrel.web.deps import templates
from kestrel.web.uploads import closing_upload

router = APIRouter()


@router.get("/backup", response_class=HTMLResponse)
async def page(request: Request):
    ctx = {
        "title": "Backup",
        "query": dict(request.query_params),
        "data_dir": str(paths.data_dir()),
        "config_dir": str(paths.config_dir()),
        "has_profile": backup_service.export_profile() is not None,
        "profile_bak": str(paths.config_dir() / backup_service.PROFILE_BAK),
    }
    return templates().TemplateResponse(request, "backup.html", ctx)


@router.get("/backup/download")
async def download_backup():
    """Stream a fresh backup zip. The build (migrations + zip) is file I/O, so it runs in a
    thread; the temp copy is deleted after the bytes have gone out."""
    try:
        archive = await asyncio.to_thread(backup_service.create_backup_to_temp)
    except OSError as exc:
        return HTMLResponse(
            f'<div class="warnbox">Could not build the backup: {html.escape(str(exc))}</div>',
            status_code=500,
        )
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
        # Bound at request time (not import time) so a wrapper installed on the service module,
        # e.g. a test confirming the temp archive is gone, is what actually runs.
        background=BackgroundTask(backup_service.discard_temp_backup, archive),
    )


@router.get("/backup/export-profile")
async def export_profile():
    """Just profile.yml. There is no separate service for the document masters, so the profile
    goes alone; the masters travel inside the full backup's database."""
    p = backup_service.export_profile()
    if p is None:
        return HTMLResponse(
            "<p>No profile.yml yet. Kestrel is running on the shipped example profile, which is "
            "fiction and is not exported. Create config/profile.yml first.</p>",
            status_code=404,
        )
    return FileResponse(p, media_type="application/yaml", filename=backup_service.PROFILE_NAME)


@router.post("/backup/import-profile", response_class=HTMLResponse)
async def import_profile(request: Request):
    """Accept a profile.yml upload. A refused file leaves the previous profile untouched and
    comes back as a readable message in the page (HTMX swaps a 200 fragment; a 4xx would be
    dropped silently by the default htmx config, so the status is 200 either way)."""
    form = await request.form()
    upload = form.get("profile_file")
    if upload is None or not getattr(upload, "filename", ""):
        return HTMLResponse('<div class="warnbox">Choose a profile.yml file first.</div>')
    try:
        # `closing_upload` because the upload owns a spooled temp file that holds an OS handle
        # once it has rolled to disk. Reading it is not closing it, and nothing else will
        # (D-RR1-4); on Windows the open handle is a sharing violation.
        async with closing_upload(upload):
            data = await upload.read()
    except OSError as exc:
        return HTMLResponse(
            f'<div class="warnbox">Could not read the upload: {html.escape(str(exc))}</div>'
        )
    try:
        result = await asyncio.to_thread(backup_service.import_profile, data)
    except backup_service.InvalidProfileError as exc:
        return HTMLResponse(f'<div class="warnbox">{html.escape(str(exc))}</div>')
    except OSError as exc:
        return HTMLResponse(
            f'<div class="warnbox">Import failed: {html.escape(str(exc))}</div>'
        )
    kept = (
        f" The previous file is at <span class=\"mono\">{html.escape(str(result.previous_backup))}"
        "</span>."
        if result.previous_backup
        else ""
    )
    return HTMLResponse(
        f'<div class="muted">Profile imported for {html.escape(", ".join(result.tracks))}.'
        f"{kept} The next worker cycle uses it; no restart needed.</div>"
    )
