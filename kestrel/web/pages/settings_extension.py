"""Settings > Browser extension (issue #186, SPEC-INTERFACES 5).

  POST /settings/extension/key       Copy key: the connection key, made first if there is none
  POST /settings/extension/key/new   Make a new key (it replaces the old one)
  POST /settings/extension/features  one of the three switches, {key, on}

The connection key is the one shared key (`server.shared_secret`, kestrel.web.auth.SETTING_KEY)
that Access from other devices also uses. Copy key never turns that access on and never replaces
a key that exists: it only makes one when there is none. Make a new key replaces it at once, so
the extension and every device that joined need the new one. Both are this computer only.

The switches are the same Setting rows the extension's own options page writes
(kestrel.services.extension), so there is one value for each. Nothing here sends anything
anywhere (hard rule 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.services import extension as ext
from kestrel.web import settings_io as io
from kestrel.web.auth import LOCAL_ONLY, generate_key, set_cached_key
from kestrel.web.auth import SETTING_KEY as SECRET_KEY
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.notifications_view import relative_time
from kestrel.web.pages.settings_roles import trigger

log = structlog.get_logger()
router = APIRouter()

# The module's own clock, so a test patches this name and never the global one (D-TEST-2).
_now = datetime.now

# (feature, the label the card shows, said when it turns on, said when it turns off).
FEATURE_TEXT: dict[str, tuple[str, str, str]] = {
    "badges": ("Show Kestrel scores on job boards",
               "Kestrel scores will show on job boards.",
               "Kestrel scores will no longer show on job boards."),
    "waiting": ("Offer roles that are waiting for a description",
                "The extension will offer roles that are waiting for a description.",
                "The extension will no longer offer roles that are waiting for a description."),
    "toolbar_count": ("Show a count on the toolbar icon",
                      "The toolbar icon will show a count.",
                      "The toolbar icon will no longer show a count."),
}
# While a store listing is empty its button opens Help, which explains loading it by hand.
HELP_HREF = "/help"
NEW_KEY_SAID = "New key made. Paste it into the extension again."
NOT_SAVED = "Could not save that. Try again."


def _valid(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def _stored_key(session) -> str | None:
    row = await session.get(Setting, SECRET_KEY)
    return _valid(row.value) if row is not None else None


def _seen_line(seen: dict[str, str] | None, now: datetime) -> str | None:
    """"Chrome · used 2 minutes ago" for the status pill, or None when it has never called."""
    if not seen:
        return None
    ago = relative_time(seen["at"], now)
    ago = "yesterday" if ago == "Yesterday" else ago
    parts = [seen["browser"]] if seen["browser"] and seen["browser"] != "Other" else []
    if ago:
        parts.append(f"used {ago}")
    return " · ".join(parts)


async def extension_ctx(request: Request, *, note: str | None = None, ok: bool = True) -> dict:
    async with sessionmaker()() as s:
        on = await ext.features(s)
        seen = await ext.last_seen(s)
        has_key = await _stored_key(s) is not None
    return {
        "xt_seen": _seen_line(seen, _now(UTC)),
        "xt_connected": seen is not None,
        "xt_features": [(name, FEATURE_TEXT[name][0], on[name]) for name in ext.FEATURES],
        "xt_store": {k: (v or HELP_HREF) for k, v in ext.STORE_URLS.items()},
        "xt_store_live": {k: bool(v) for k, v in ext.STORE_URLS.items()},
        "xt_has_key": has_key,
        "xt_port": request.url.port or 8765,
        "xt_note": note, "xt_ok": ok,
    }


def _key_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_xt_key.html", ctx,
                                        headers=headers or {})


@router.post("/settings/extension/key", dependencies=[LOCAL_ONLY])
async def copy_key(request: Request) -> Response:
    """Copy key. Returns the key the extension needs, making one only when there is none. It
    never touches whether other devices may open Kestrel (server.bind_all), and an insert that
    loses a race to another first copy keeps the key that won, so a key that exists is never
    replaced here."""
    try:
        async with sessionmaker()() as s:
            row = await s.get(Setting, SECRET_KEY)
            if row is None:
                await s.execute(
                    sqlite_insert(Setting)
                    .values(key=SECRET_KEY, value=generate_key(), updated_at=utcnow_iso())
                    .on_conflict_do_nothing(index_elements=["key"]))
                await s.commit()
            elif _valid(row.value) is None:  # a row with no usable key in it: fill it once
                row.value = generate_key()
                await s.commit()
        async with sessionmaker()() as s:
            key = _valid((await s.execute(
                select(Setting.value).where(Setting.key == SECRET_KEY))).scalar_one_or_none())
    except SQLAlchemyError as exc:
        log.warning("extension_key_failed", error=type(exc).__name__)
        return io.refuse(NOT_SAVED)
    if key is None:
        return io.refuse(NOT_SAVED)
    set_cached_key(key)
    return JSONResponse({"ok": True, "key": key, "message": "Copied"},
                        headers={"Cache-Control": "no-store"})


@router.post("/settings/extension/key/new", response_class=HTMLResponse,
             dependencies=[LOCAL_ONLY])
async def new_key(request: Request):
    """Make a new key. It replaces the old one at once (auth's cached key too), so the extension
    and any device that joined need the new one; the dialog says so first (ack=on)."""
    form = await request.form()
    if str(form.get("ack") or "") != "on":
        return _key_card(request, await extension_ctx(
            request, note="Confirm that you understand first.", ok=False))
    token = generate_key()
    from kestrel.web.pages.settings import _set

    async with sessionmaker()() as s:
        await _set(s, SECRET_KEY, token)
        await s.commit()
    set_cached_key(token)
    return _key_card(request, await extension_ctx(request, note=NEW_KEY_SAID),
                     trigger(NEW_KEY_SAID))


@router.post("/settings/extension/features")
async def save_feature(request: Request) -> Response:
    """One switch of What it does: {"key": "badges", "on": true}. The same row the extension's
    options page writes, so the two always show one value."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return io.refuse("That setting is not one Kestrel can change here.")
    key, on = body.get("key"), body.get("on")
    if key not in ext.FEATURES or not isinstance(on, bool):
        return io.refuse("That setting is not one Kestrel can change here.")
    try:
        async with sessionmaker()() as s:
            now = await ext.set_features(s, {key: on})
            await s.commit()
    except SQLAlchemyError as exc:
        log.warning("extension_feature_failed", key=key, error=type(exc).__name__)
        return io.refuse(NOT_SAVED)
    _label, said_on, said_off = FEATURE_TEXT[key]
    return io.reply(said_on if now[key] else said_off, value=now[key])
