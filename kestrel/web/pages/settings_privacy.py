"""Settings > Privacy and access (D-UI-106, D-SEC-3, D-MCP-1, SPEC-INTERFACES 2.6 item 4).

  POST /settings/privacy/confidential   the switch, the employer and the label it shows instead
  POST /settings/privacy/access         Access from other devices: on (with the confirm) or off
  POST /settings/privacy/access/rotate  a new key, which locks out every device holding the old
  GET  /settings/privacy/access         the card
  GET  /settings/privacy/mcp            Connect an AI assistant (MCP)
  POST /settings/privacy/mcp/port       change the port
  (POST /settings/mcp, the switch, keeps its older path and is shared with the tests.)

Access from other devices is one shared key over plain HTTP, so it is off by default, needs the
person to confirm what that means, and can only be changed from this computer even with the key.
The change takes effect after Kestrel restarts (D-SEC-3). The assistant section lists every
action the MCP server exposes in plain words. Nothing here sends anything anywhere."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from kestrel import config as config_mod
from kestrel.db.models import Setting
from kestrel.docs.current_employer import (
    choose_href,
    clean_placeholder,
    current_employers,
    suggested_placeholder,
)
from kestrel.services import lan_access
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import auth as _auth
from kestrel.web import settings_io as io
from kestrel.web import settings_view as sv
from kestrel.web.auth import SETTING_KEY as SECRET_KEY
from kestrel.web.auth import (
    generate_key,
    is_loopback,
    join_url,
    qr_svg,
    set_cached_key,
)
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import authored_error, plain_error
from kestrel.web.pages.profile import apply_confidential
from kestrel.web.pages.settings_roles import trigger
from kestrel.web.redact import confidential_is_inert

log = structlog.get_logger()
router = APIRouter()

# Said when Undo cannot change settings.yml for a reason that is not on lan_access.REFUSALS: the
# file could not be read or written. The type goes to the log, not the page (D-SEC-4).
LAN_UNDO_FAILED = "settings.yml could not be changed, so nothing was changed."


# ------------------------------------------------------------------------ confidential mode
async def conf_ctx(*, note: str | None = None, problem: str | None = None) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        employers = await current_employers(s)
        inert = await confidential_is_inert(s)
        choose = await choose_href(s)
    first = employers[0] if employers else None
    return {
        "conf_on": settings.confidential_mode,
        "conf_employer": first["employer"] if first else "",
        "conf_shown": first["shown_as"] if first else suggested_placeholder("employment"),
        "conf_inferred": bool(first and first["inferred"]),
        "conf_inert": inert, "conf_choose": choose, "conf_note": note, "conf_problem": problem,
        "conf_href": first["href"] if first else choose,
        "conf_section_id": first["section_id"] if first else 0,
    }


def _conf_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_conf.html", ctx,
                                        headers=headers or {})


@router.post("/settings/privacy/confidential", response_class=HTMLResponse)
async def confidential_save(request: Request):
    """Save the switch and the two labels. Backups keep the real data so a restore is complete."""
    form = await request.form()
    async with sessionmaker()() as s:
        prev = await io.read(s, "confidential_mode")
    on = "on" in form.getlist("conf_mode") or str(form.get("conf_mode") or "") in ("1", "true")
    employer = " ".join(str(form.get("conf_employer") or "").split())[:200]
    shown = clean_placeholder(form.get("conf_shown"))
    note, problem = await apply_confidential(on, employer, shown)
    msg = ""
    undo = None
    if str(form.get("changed") or "") == "mode":
        msg = "Confidential mode is on." if on else "Confidential mode is off."
        undo = io.undo_url("confidential_mode", prev, on)
    elif problem is None:
        msg = "Confidential labels updated."
    return _conf_card(request, await conf_ctx(note=note, problem=problem),
                      trigger(msg, undo=undo,
                              refresh={"refresh": "/settings/section/privacy",
                                       "target": "#st-pane-privacy"}))


# ------------------------------------------------------------------------------- access
async def access_ctx(request: Request, *, note: str | None = None, ok: bool = True) -> dict:
    async with sessionmaker()() as s:
        row = await s.get(Setting, SECRET_KEY)
    secret = row.value if row is not None and isinstance(row.value, str) and row.value else None
    lan = lan_access.state(running_on=getattr(request.app.state, "bind_all", None),
                           has_secret=bool(secret))
    host = port = url = svg = None
    address = ""
    if secret and lan.file_on:
        try:
            host = _auth.lan_host_ip()
            port = request.url.port or 8765
        except (ValueError, OSError):
            host = None
        if host:
            address = f"http://{host}:{port}"
            url = join_url(host, port, secret)
            svg = qr_svg(url)
    return {
        "lan": lan, "lan_note": note, "lan_ok": ok, "lan_secret": bool(secret),
        "lan_address": address, "lan_join": url, "lan_qr": svg, "lan_host": host,
        "lan_local": is_loopback(request),
    }


def _acc_card(request: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_access.html", ctx,
                                        headers=headers or {})


@router.get("/settings/privacy/access", response_class=HTMLResponse)
async def access_card(request: Request):
    return _acc_card(request, await access_ctx(request))


@router.post("/settings/privacy/access", response_class=HTMLResponse)
async def access_save(request: Request):
    """Turn Access from other devices on or off. Only this computer may change it, even with the
    key. Turning it on needs the confirm (`ack=on`) and makes a key when there is none."""
    if not is_loopback(request):
        return HTMLResponse("Access can only be changed from this computer.", status_code=403)
    form = await request.form()
    want_on = str(form.get("on") or "") in ("1", "true", "on")
    if want_on and str(form.get("ack") or "") != "on":
        return _acc_card(request, await access_ctx(
            request, note="Confirm that you understand first.", ok=False))
    async with sessionmaker()() as s:
        row = await s.get(Setting, SECRET_KEY)
        if want_on and not (row and isinstance(row.value, str) and row.value):
            token = generate_key()
            from kestrel.web.pages.settings import _set

            await _set(s, SECRET_KEY, token)
            await s.commit()
            set_cached_key(token)
    prev = lan_access.file_value()
    try:
        lan_access.set_bind_all(want_on)
    except lan_access.LanAccessError as exc:
        return _acc_card(request, await access_ctx(request, note=plain_error(exc), ok=False))
    ctx = await access_ctx(request)
    note = "Takes effect after Kestrel restarts."
    after = ctx["lan"]
    if after.env_name and after.effective_on != want_on:
        note += (f" The environment variable {after.env_name} is set to {after.env_value} and "
                 "wins over this switch until you remove it.")
    ctx["lan_note"] = note
    msg = ("Other devices on your network can open Kestrel." if want_on
           else "Other devices can no longer open Kestrel.")
    return _acc_card(request, ctx, trigger(
        msg, undo=f"/settings/privacy/access/undo?was={'1' if prev else '0'}"
                  f"&now={'1' if want_on else '0'}"))


@router.post("/settings/privacy/access/undo")
async def access_undo(request: Request) -> Response:
    if not is_loopback(request):
        return io.refuse("Access can only be changed from this computer.")
    q = request.query_params
    was, now = q.get("was") == "1", q.get("now") == "1"
    if lan_access.file_value() != now:
        return io.refuse(io.NO_UNDO)
    try:
        lan_access.set_bind_all(was)
    except lan_access.LanAccessError as exc:
        log.warning("lan_access_undo_failed", error=str(exc))
        return io.refuse(authored_error(exc, lan_access.REFUSALS, LAN_UNDO_FAILED))
    return io.reply("Put back.")


@router.post("/settings/privacy/access/rotate", response_class=HTMLResponse)
async def access_rotate(request: Request):
    """A new key. It takes effect at once and signs out every device holding the old one, which
    is the only way to take access away from one of them."""
    if not is_loopback(request):
        return HTMLResponse("Access can only be changed from this computer.", status_code=403)
    token = generate_key()
    from kestrel.web.pages.settings import _set

    async with sessionmaker()() as s:
        await _set(s, SECRET_KEY, token)
        await s.commit()
    set_cached_key(token)
    return _acc_card(request, await access_ctx(
        request, note="New key made. Devices that used the old one are signed out."),
        trigger("New key made. Devices that used the old one are signed out."))


# ---------------------------------------------------------------------------------- MCP
async def mcp_ctx(*, message: str | None = None, ok: bool = True) -> dict:
    from kestrel.web.pages.settings import _mcp_ctx

    async with sessionmaker()() as s:
        ctx = await _mcp_ctx(s, message=message, ok=ok)
    ctx["mcp_actions"] = sv.MCP_ACTIONS
    ctx["mcp_never"] = sv.MCP_NEVER
    return ctx


def _mcp_card(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/st_mcp.html", ctx)


@router.get("/settings/privacy/mcp", response_class=HTMLResponse)
async def mcp_card(request: Request):
    return _mcp_card(request, await mcp_ctx())


@router.post("/settings/privacy/mcp/port", response_class=HTMLResponse)
async def mcp_port(request: Request):
    """Change the port the assistant connects to: 1024 to 65535. A running server moves to it at
    once; one that is not running picks it up the next time it starts."""
    from kestrel.mcp import runtime as mcp_runtime
    from kestrel.web.pages.settings import _set

    form = await request.form()
    raw = str(form.get("port") or "").strip()
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        return _mcp_card(request, await mcp_ctx(
            message="Use a number from 1024 to 65535.", ok=False))
    port = int(raw)
    async with sessionmaker()() as s:
        prev = await io.read(s, "mcp.port")
        settings = await load_settings_with_db(s)
        if settings.mcp.port == port:
            return _mcp_card(request, await mcp_ctx())
        await _set(s, "mcp.port", port)
        await s.commit()
        settings = await load_settings_with_db(s)
    message, ok = f"Port changed to {port}.", True
    try:
        if mcp_runtime.is_live() and settings.mcp.enabled and mcp_runtime.running():
            await mcp_runtime.stop()
            problem = await mcp_runtime.start(
                port, bind_all=config_mod.get_settings().server.bind_all)
            if problem:
                message, ok = problem, False
    except Exception as exc:  # noqa: BLE001 - the panel says it plainly; the row is saved
        log.warning("mcp_port_restart_failed", error=f"{type(exc).__name__}: {exc}")
        message, ok = f"Saved, but the assistant could not move to it: {plain_error(exc)}", False
    ctx = await mcp_ctx(message=message, ok=ok)
    return templates().TemplateResponse(
        request, "partials/st_mcp.html", ctx,
        headers=trigger(message, undo=io.undo_url("mcp.port", prev, port)),
    )
