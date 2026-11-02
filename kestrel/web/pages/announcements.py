"""The announcement banner fragment and its dismiss route (WP-22).

Same shape as the update banner: the base template carries an empty slot that loads
GET /announcements, and the dismiss button swaps the slot empty. Purely local; nothing here
makes a network call."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.web import announcements
from kestrel.web.deps import sessionmaker, templates

log = structlog.get_logger()
router = APIRouter()


def _as_ids(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


async def _model_upgrade_notice() -> dict | None:
    from kestrel.analytics.today import model_upgrade_gb
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    gb = await model_upgrade_gb(settings)
    return announcements.model_upgrade_notice(gb) if gb else None


@router.post("/announcements/model-upgrade/download", response_class=HTMLResponse)
async def model_upgrade_download(request: Request):
    """The notice's button: queue the pulls exactly as Settings > Download models does (start_action
    also owes the rebuild of the derived models for when they are present)."""
    from kestrel.web.pages.setup import start_action

    _state, message, _status = await start_action("pull")  # also owes the rebuild when stale
    note = message or ("Downloading the corrected model. Kestrel rebuilds its models from it "
                       "when it finishes; you can follow it in Settings.")
    return templates().TemplateResponse(
        request, "partials/announcement.html",
        {"notice": {"id": announcements.MODEL_UPGRADE_ID, "text": note}},
    )


@router.get("/announcements", response_class=HTMLResponse)
async def banner(request: Request):
    notice = None
    try:
        async with sessionmaker()() as s:
            row = await s.get(Setting, announcements.DISMISSED_KEY)
        dismissed = _as_ids(row.value if row else None)
        notice = announcements.pending(dismissed)
        if notice is None and announcements.MODEL_UPGRADE_ID not in dismissed:
            notice = await _model_upgrade_notice()
    except SQLAlchemyError as exc:
        log.warning("announcement_read_failed", error_type=type(exc).__name__)
    return templates().TemplateResponse(
        request, "partials/announcement.html", {"notice": notice}
    )


@router.post("/announcements/{announcement_id}/dismiss", response_class=HTMLResponse)
async def dismiss(announcement_id: str):
    """Unknown ids are ignored. A failed write is logged and answered plainly, never a 500."""
    if announcement_id not in announcements.known_ids():
        return HTMLResponse("")
    try:
        async with sessionmaker()() as s:
            row = await s.get(Setting, announcements.DISMISSED_KEY)
            ids = _as_ids(row.value if row else None)
            if announcement_id not in ids:
                ids.append(announcement_id)
            if row is None:
                s.add(Setting(key=announcements.DISMISSED_KEY, value=ids))
            else:
                row.value = ids
                row.updated_at = utcnow_iso()
            await s.commit()
    except SQLAlchemyError as exc:
        log.warning("announcement_dismiss_failed", error_type=type(exc).__name__)
        return HTMLResponse(
            '<div class="sf-notice" role="status">Could not save that, so the notice will show '
            "again next time Kestrel opens. Close it again then.</div>"
        )
    return HTMLResponse("")
