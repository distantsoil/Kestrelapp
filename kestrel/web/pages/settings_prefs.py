"""Settings > Preferences and the saves every section shares (D-UI-106, D-UI-109, D-UI-112).

  POST /settings/ui                  one interface-owned key (scene, touches, text size, region,
                                     sound, quiet hours ...), validated by kestrel.ui_settings
  POST /settings/prefs/tracks        Employment and Consulting switches (at least one stays on)
  POST /settings/prefs/scheme        the color scheme every browser sees first
  POST /settings/prefs/notify        a "Tell me about" kind, or Computer notifications
  POST /settings/prefs/avatar/photo  Upload a photo (kept on this computer, resized)
  POST /settings/prefs/avatar/photo/remove
  POST /settings/undo                take one change back, only if nothing changed it since

Every save answers JSON (settings_io.reply) that the page turns into a message with Undo. A
refusal is a plain sentence, never a stack trace, and writes nothing. Nothing here sends
anything anywhere."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from kestrel import ui_settings as u
from kestrel.config import UI_SCHEMES
from kestrel.services import profile_photo, ui_state
from kestrel.services import tracks as tracks_svc
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import settings_io as io
from kestrel.web import settings_view as sv
from kestrel.web.deps import sessionmaker, set_ui_scheme_cache
from kestrel.web.errors import plain_error

log = structlog.get_logger()
router = APIRouter()

# The interface's own words for each key, as the message under the control reads (SPEC-INTERFACES
# 2.6, matching the locked mockup).
_TEXT_SIZE = {"small": "small", "default": "default", "large": "large"}


def _scene_name(value: str) -> str:
    return value.replace("_", " ").title()


def message_for(key: str, value: Any) -> str:
    if key == "ui.scene":
        return "Scene off." if value == u.NO_SCENE else f"{_scene_name(value)} scene on."
    if key == "ui.scene_daily":
        return ("The scene will change every day." if value
                else "The scene will stay as you picked it.")
    if key == "ui.scene_animate":
        return "The scene is moving." if value else "The scene is still."
    if key == "ui.scene_rotation":
        n = len(value)
        return f"Rotation has {n} scene{'' if n == 1 else 's'}."
    if key == "ui.touches_enabled":
        return "Seasonal touches are on." if value else "Seasonal touches are off."
    if key in ("ui.touches_off", "ui.touch_groups_off"):
        return "Seasonal touches updated."
    if key == "ui.avatar":
        return "Picture updated."
    if key == "ui.text_size":
        return f"Text size set to {_TEXT_SIZE.get(value, value)}."
    if key == "ui.reduce_motion":
        return "Motion reduced." if value else "Motion back on."
    if key == "ui.classic_nav":
        return "Pages now load the classic way." if value else "Pages now change in place."
    if key == "notify.sound":
        return "Sounds are on." if value else "Sounds are off."
    if key == "notify.sound_name":
        label = dict(sv.SOUND_LABELS).get(value, value)
        return f"{label} selected."
    if key == "notify.sound_volume":
        return ""  # dragging a slider is not worth a message each time
    if key == "notify.quiet_hours":
        return "Quiet hours are on." if value.get("enabled") else "Quiet hours are off."
    if key == "region.date_format":
        label = dict(sv.date_format_options()).get(value, value)
        return f"Dates show as {label}."
    if key == "region.time_format":
        return "Times show as 12-hour." if value == "12h" else "Times show as 24-hour."
    return "Saved."


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/settings/ui")
async def save_ui_key(request: Request) -> Response:
    """Save one key the interface owns. Unknown key: 404 text. A value that is not allowed is
    refused with the plain sentence from kestrel.ui_settings and nothing is written."""
    body = await _json(request)
    key = str(body.get("key") or "")
    if key not in ui_state.OWNED_KEYS:
        return io.refuse("That setting is not one Kestrel can change here.")
    try:
        clean = ui_state.validate(key, body.get("value"))
    except ValueError as exc:
        return io.refuse(str(exc))
    quiet = bool(body.get("quiet"))
    async with sessionmaker()() as s:
        prev = await io.read(s, key)
        if prev == clean:
            return io.reply("", noop=True, value=clean)
        try:
            await ui_state.set_value(s, key, clean)
            await s.commit()
        except SQLAlchemyError as exc:
            await s.rollback()
            log.warning("settings_ui_save_failed", key=key, error=f"{type(exc).__name__}: {exc}")
            return io.refuse(f"Could not save that: {plain_error(exc)}")
    extra: dict[str, Any] = {"value": clean}
    if key.startswith("region."):
        async with sessionmaker()() as s:
            st = await load_settings_with_db(s)
        extra["preview"] = sv.region_preview(st.region.date_format, st.region.time_format)
    msg = "" if quiet else message_for(key, clean)
    return io.reply(msg, undo=io.undo_url(key, prev, clean) if msg else None, **extra)


# ------------------------------------------------------------------------------- tracks
@router.post("/settings/prefs/tracks")
async def save_tracks(request: Request) -> Response:
    """What you're looking for: Employment and Consulting. At least one stays on; turning one off
    hides it everywhere and deletes nothing (D-UI-26, D-UI-28)."""
    from kestrel.ingest.generated import rebuild_generated_sources

    body = await _json(request)
    picked = [t for t in sv.TRACKS if bool(body.get(t))]
    if not picked:
        return io.refuse("Keep at least one on.")
    async with sessionmaker()() as s:
        before = tuple(await tracks_svc.load_enabled(s))
        after = tuple(await tracks_svc.save_enabled(s, picked))
        await s.commit()
        try:
            await rebuild_generated_sources(s)
            await s.commit()
        except (SQLAlchemyError, OSError, ValueError) as exc:  # saved; the searches catch up
            await s.rollback()
            log.warning("generated_sources_rebuild_failed", error=str(exc))
    msgs = {
        ("employment", True): "Employment is on.",
        ("employment", False): "Employment is off and hidden.",
        ("consulting", True): "Consulting and contract work is on.",
        ("consulting", False): "Consulting and contract work is off and hidden.",
    }
    changed = [t for t in sv.TRACKS if (t in before) != (t in after)]
    message = msgs[(changed[0], changed[0] in after)] if changed else "Saved."
    undo = ("/settings/prefs/tracks/undo?was=" + ",".join(before) + "&now=" + ",".join(after))
    return io.reply(message, undo=undo if changed else None, reload=bool(changed),
                    tracks=list(after))


@router.post("/settings/prefs/tracks/undo")
async def undo_tracks(request: Request) -> Response:
    """Undo for the track switches: put back the tracks as they were, only while the choice is
    still the one the save wrote (a track switched since is never overwritten)."""
    from kestrel.ingest.generated import rebuild_generated_sources

    q = request.query_params
    want = [t for t in sv.TRACKS if t in (q.get("was") or "").split(",")]
    now = [t for t in sv.TRACKS if t in (q.get("now") or "").split(",")]
    if not want:
        return io.refuse(io.NO_UNDO)
    async with sessionmaker()() as s:
        if list(await tracks_svc.load_enabled(s)) != now:
            return io.refuse(io.NO_UNDO)
        await tracks_svc.save_enabled(s, want)
        await s.commit()
        try:
            await rebuild_generated_sources(s)
            await s.commit()
        except (SQLAlchemyError, OSError, ValueError):
            await s.rollback()
    return io.reply("Put back.", reload=True)


# ------------------------------------------------------------------------------- scheme
@router.post("/settings/prefs/scheme")
async def save_scheme(request: Request) -> Response:
    """The color scheme every browser sees first (`ui.scheme`). Only the schemes in
    config.UI_SCHEMES are accepted."""
    body = await _json(request)
    scheme = str(body.get("scheme") or "").strip().lower()
    if scheme not in UI_SCHEMES:
        return io.refuse("Choose one of the color schemes shown.")
    async with sessionmaker()() as s:
        prev = await io.read(s, "ui.scheme")
        await io.write(s, "ui.scheme", scheme)
        await s.commit()
    set_ui_scheme_cache(scheme)
    names = dict(sv.SCHEMES)
    return io.reply(f"{names.get(scheme, scheme.title())} selected.",
                    undo=io.undo_url("ui.scheme", prev, scheme))


# --------------------------------------------------------------------------- notifications
@router.post("/settings/prefs/notify")
async def save_notify(request: Request) -> Response:
    """A "Tell me about" kind (one switch per group of notification kinds) or Computer
    notifications. Kinds not listed are on, so turning one off writes `false` for each of the
    group's kinds and turning it back on removes those rows."""
    body = await _json(request)
    group = str(body.get("group") or "")
    on = bool(body.get("on"))
    if group == "desktop":
        async with sessionmaker()() as s:
            prev = await io.read(s, "notify.toast")
            await io.write(s, "notify.toast", on)
            await s.commit()
        return io.reply(
            "Computer notifications are on." if on else "Computer notifications are off.",
            undo=io.undo_url("notify.toast", prev, on),
        )
    if group not in sv.NOTIFY_KINDS:
        return io.refuse("That kind of alert is not one Kestrel has.")
    kinds = sv.NOTIFY_KINDS[group][2]
    async with sessionmaker()() as s:
        was = {k: await io.read(s, f"notify.events.{k}") for k in kinds}
        for k in kinds:
            await io.write(s, f"notify.events.{k}", io.ABSENT if on else False)
        await s.commit()
    words = {
        "matches": ("You will hear about new strong matches.",
                    "No more alerts for new strong matches."),
        "followups": ("You will hear about follow-ups due.", "No more alerts for follow-ups."),
        "runs": ("You will hear when a run finishes.", "No more alerts when a run finishes."),
        "attention": ("You will hear when something needs attention.",
                      "No more alerts for problems."),
    }[group]
    # One Undo for the group: the first kind's token restores that kind, and the others follow
    # because the page asks for each; here the group is undone as one by repeating the switch.
    undo = ("/settings/prefs/notify/undo?" + "&".join(
        f"{k}={io.dumps(was[k])}" for k in kinds) + f"&now={'on' if on else 'off'}")
    return io.reply(words[0] if on else words[1], undo=undo)


@router.post("/settings/prefs/notify/undo")
async def undo_notify(request: Request) -> Response:
    q = request.query_params
    now = q.get("now")
    keys = [k for k in q.keys() if k != "now"]
    async with sessionmaker()() as s:
        for k in keys:
            if k not in {x for g in sv.NOTIFY_KINDS.values() for x in g[2]}:
                return io.refuse(io.NO_UNDO)
        # Only while every kind still reads as the save left it.
        for k in keys:
            cur = await io.read(s, f"notify.events.{k}")
            expect = io.ABSENT if now == "on" else False
            if cur != expect:
                return io.refuse(io.NO_UNDO)
        try:
            values = {k: io.loads(q.get(k) or "absent") for k in keys}
        except ValueError:
            return io.refuse(io.NO_UNDO)
        if not all(v is io.ABSENT or isinstance(v, bool) for v in values.values()):
            return io.refuse(io.NO_UNDO)
        for k, value in values.items():
            await io.write(s, f"notify.events.{k}", value)
        await s.commit()
    return io.reply("Put back.", reload=True)


# ------------------------------------------------------------------------------------ undo
@router.post("/settings/undo")
async def undo_setting(request: Request) -> Response:
    """Put one setting back to what it was before a save, but only while it still holds exactly
    what that save wrote."""
    q = request.query_params
    key = q.get("key") or ""
    if not io.undoable(key):
        return io.refuse(io.NO_UNDO)
    try:
        prev = io.loads(q.get("prev") or "absent")
        now = io.loads(q.get("now") or "absent")
    except ValueError:
        return io.refuse(io.NO_UNDO)
    if not io.plausible(key, prev):
        return io.refuse(io.NO_UNDO)
    async with sessionmaker()() as s:
        if await io.read(s, key) != now:
            return io.refuse(io.NO_UNDO)
        await io.write(s, key, prev)
        await s.commit()
    await _after_undo(key)
    return io.reply("Put back.", key=key)


async def _after_undo(key: str) -> None:
    if key == "ui.scheme":
        async with sessionmaker()() as s:
            row = await io.read(s, "ui.scheme")
        set_ui_scheme_cache(row if isinstance(row, str) else None)
    if key.startswith("tier0.rules_off."):
        track = key.rsplit(".", 1)[-1]
        from kestrel.web.pages.settings_roles import queue_rule_recheck

        await queue_rule_recheck(track)


# ------------------------------------------------------------------------------- the photo
@router.post("/settings/prefs/avatar/photo")
async def avatar_photo(request: Request) -> Response:
    """Keep a chosen picture as the photo and show it as the picture. It stays on this computer,
    is resized when stored, and never reaches a resume or any export (D-UI-112, D-UI-117)."""
    form = await request.form()
    upload = form.get("photo")
    if upload is None or not hasattr(upload, "read"):
        return io.refuse("Choose a picture first.")
    from kestrel.web.uploads import closing_upload

    async with closing_upload(upload):
        data = await upload.read(profile_photo.MAX_BYTES + 1)
    try:
        await profile_photo.save_async(data)
    except profile_photo.PhotoError as exc:
        return io.refuse(plain_error(exc))
    async with sessionmaker()() as s:
        prev = await io.read(s, "ui.avatar")
        await ui_state.set_value(s, "ui.avatar", "photo")
        await s.commit()
    return io.reply("Picture updated.", undo=io.undo_url("ui.avatar", prev, "photo"),
                    value="photo", photo=f"/profile/photo?v={profile_photo.version()}")


@router.post("/settings/prefs/avatar/photo/remove")
async def avatar_photo_remove(request: Request) -> Response:
    profile_photo.remove()
    async with sessionmaker()() as s:
        await ui_state.set_value(s, "ui.avatar", "initial")
        await s.commit()
    return io.reply("Photo removed.", value="initial")

