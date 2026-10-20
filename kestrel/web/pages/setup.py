"""Settings > System (D-UI-31): the machine checks that used to be the Setup page, and the first-run
routing of GET / (D-UI-30).

The Setup page is folded into Settings. GET /settings/system renders the System tab's panel (the
Local / Hosted / Both cards, the detected hardware, the low-spec warning, steps 2 to 6 with their
pull, build and verify buttons, and Restart onboarding). The action routes keep their /setup/*
paths so the CLI docs, the tray and old bookmarks hold: GET /setup/steps is the steps partial the
panel polls every 2 s while an action runs; POST /setup/mode writes the choice; POST
/setup/profile records a hardware profile; POST /setup/action/{name} runs one button: pull and
build queue task rows that the setup loop runs through Ollama's HTTP API (model_tasks, release
round R1), cancel stops them, check probes Ollama again, verify runs `kestrel doctor` in a
background thread. GET /setup itself redirects: to the onboarding while it is not complete,
otherwise to /settings#system.

This router also serves GET / ahead of the dashboard (it is listed first in app._PAGE_MODULES). A
fresh install (no completed onboarding, no profile.yml, no stored llm.mode) is redirected to
/onboarding. Any other install whose onboarding is not complete gets the dashboard plus a
"Finish setup" banner that links to the onboarding. The redirect can be switched off with
FIRST_RUN_REDIRECT, which the test suite does so the many tests that hit / on an empty data home
still get the dashboard; the onboarding tests switch it back on."""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from kestrel.db.models import Setting
from kestrel.llm import profiles
from kestrel.services import model_tasks as mt
from kestrel.services import onboarding as ob
from kestrel.services import setup_state as ss
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.flash import flash_redirect, flash_text

log = structlog.get_logger()
router = APIRouter()

ACTIONS = ("pull", "build", "verify", "check", "cancel")
FIRST_RUN_REDIRECT = True

BANNER = (
    '<div class="warnbox" id="finish-setup" style="display:flex;align-items:center;gap:1rem;'
    'flex-wrap:wrap;margin-bottom:1rem"><div><b>Finish setup.</b> Kestrel is not fully set up '
    "yet. The setup guide picks up where you left off: how Kestrel thinks, who you are, what you "
    'want, and your first search.</div><a class="btn pri" href="/onboarding">Finish setup</a>'
    "</div>"
)
# The banner goes just inside <main> (the shell's content column), or <body> when there is none.
_MAIN_TAG = re.compile(r"<main\b[^>]*>", re.IGNORECASE)
_BODY_TAG = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


async def _rows(session) -> dict[str, Any]:
    keys = (ss.MODE_KEY, ss.CHOICE_KEY)
    found = (await session.execute(select(Setting.key, Setting.value).where(Setting.key.in_(keys))))
    return {k: v for k, v in found.all()}


async def setup_state() -> ss.SetupState:
    """The machine's setup state, after writing a pending hosted mode that can now run."""
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _rows(s)
        # Hosted chosen before its key was connected: the mode row waits until hosted can run.
        if await ss.apply_pending_hosted_mode(s, settings, rows):
            await s.commit()
            rows = await _rows(s)
    return await asyncio.to_thread(ss.assess, settings, rows)


async def models_snapshot() -> dict[str, Any]:
    """The pull and build task rows the page shows (model_tasks.snapshot)."""
    async with sessionmaker()() as s:
        return await mt.snapshot(s)


def action_ctx(models: dict[str, Any]) -> dict[str, Any]:
    """The running-state keys both the Settings steps and the onboarding local box read: the
    verify thread's log, the pull or build task rows, and whether either is live (the partial
    polls while it is)."""
    act = ss.latest_action()
    snap = act.snapshot() if act is not None else None
    verify_running = bool(snap and snap["status"] == "running")
    return {
        "action": snap,
        "models": models,
        "running": verify_running or bool(models.get("live")),
    }


async def _ctx(request: Request, state: ss.SetupState, **extra) -> dict[str, Any]:
    return {
        "query": dict(request.query_params),
        "st": state,
        **action_ctx(await models_snapshot()),
        "choices": ss.CHOICES,
        "profile_names": profiles.load_catalog().names(),
        **extra,
    }


async def _steps_partial(request: Request, state: ss.SetupState, status_code: int = 200,
                         **extra):
    return templates().TemplateResponse(
        request, "partials/setup_steps.html", await _ctx(request, state, **extra),
        status_code=status_code,
    )


async def _system_partial(request: Request, state: ss.SetupState, **extra):
    return templates().TemplateResponse(
        request, "partials/system_panel.html", await _ctx(request, state, **extra)
    )


def _back_to_system(code: str) -> RedirectResponse:
    return flash_redirect("/settings", code, "system")


@router.get("/setup")
async def setup_page():
    """The old Setup page: the onboarding while it is not complete, else Settings > System."""
    async with sessionmaker()() as s:
        done = await ob.is_complete(s)
    return RedirectResponse("/settings#system" if done else "/onboarding", status_code=303)


@router.get("/settings/system", response_class=HTMLResponse)
async def system_panel(request: Request):
    msg = flash_text(request.query_params.get("msg"))  # a known code only, never the raw text
    return await _system_partial(request, await setup_state(), message=msg)


@router.get("/setup/steps", response_class=HTMLResponse)
async def setup_steps(request: Request):
    return await _steps_partial(request, await setup_state())


@router.post("/setup/mode")
async def setup_mode(request: Request):
    form = await request.form()
    choice = str(form.get("mode") or "")
    if choice not in ss.CHOICES:
        return HTMLResponse("unknown run mode", status_code=400)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        msg = await ss.set_run_mode(s, choice, settings)
        await s.commit()
    if request.headers.get("HX-Request"):
        return await _system_partial(request, await setup_state(), message=msg)
    code = "mode_hosted_pending" if msg.startswith(ss.HOSTED_PENDING) else f"mode_{choice}"
    return _back_to_system(code)


@router.post("/setup/profile")
async def setup_profile(request: Request):
    form = await request.form()
    name = str(form.get("profile") or "").strip()
    try:
        await asyncio.to_thread(ss.record_profile, name)
    except KeyError as exc:
        # A fixed sentence with the submitted name, never the exception's own text (D-SEC-1).
        log.info("setup_profile_unknown", profile=name, error=str(exc))
        return HTMLResponse(
            f"Unknown hardware profile: {html.escape(name)}.", status_code=400
        )
    except OSError as exc:
        log.warning("setup_profile_record_failed", error_type=type(exc).__name__, error=str(exc))
        return HTMLResponse(
            f"could not record the profile: {html.escape(plain_error(exc))}", status_code=500
        )
    msg = f"Profile {name} recorded."
    if request.headers.get("HX-Request"):
        return await _system_partial(request, await setup_state(), message=msg)
    return _back_to_system("profile_recorded")


async def start_action(name: str) -> tuple[ss.SetupState, str | None, str | None]:
    """Run one Setup button. Returns (state, message, status) where status is None when it
    started, "busy" when another action runs, "error" when it could not start. Shared with the
    onboarding model step.

    pull and build queue task rows (model_tasks), so they never block this request and survive a
    restart; cancel stops them; check drops the cached Ollama probe so the page asks again;
    verify runs the doctor in a background thread as before."""
    if name == "check":
        ss.forget_ollama_probe()
        return await setup_state(), None, None
    if name == "cancel":
        async with sessionmaker()() as s:
            n = await mt.cancel_all(s)
        await asyncio.sleep(0)  # let the running stream see its cancel event and close
        msg = None
        if mt.stopping():
            msg = "Stopping…"
        elif n:
            msg = "Stopped. Press the button again to continue from where it stopped."
        return await setup_state(), msg, None
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _rows(s)
        live = await mt.live_tasks(s)
    if name in ("pull", "build"):
        ss.forget_ollama_probe()  # the buttons act on what Ollama has now
    state = await asyncio.to_thread(ss.assess, settings, rows)
    running = ss.running_action()
    if live:
        kind = mt.KIND.get(live[0].type, "a download")
        return state, f"Not started: {kind} is still running.", "busy"
    if running is not None:
        return state, f"Not started: {running.name} is still running.", "busy"
    if name == "verify":
        try:
            ss.verify()
        except ss.ActionBusy as exc:
            log.info("setup_action_busy", action=name, error=str(exc))
            return state, f"Not started: {plain_error(exc)}.", "busy"
        return state, None, None
    if state.ollama_version is None:
        return state, (
            f"Not started: Ollama is not running at {state.ollama_url}. Open Ollama, then press "
            "Check again."
        ), "error"
    try:
        async with sessionmaker()() as s:
            if name == "pull":
                n = await mt.queue_pulls(s, state.missing_tags)
                if not n:
                    return state, "Every model this profile needs is already downloaded.", None
            else:
                if state.missing_tags:
                    return state, "Not started: download the models first.", "error"
                await mt.queue_builds(s, state.profile, state.profile_reason)
    except (KeyError, ValueError, OSError) as exc:
        log.warning("setup_action_not_started", action=name, error_type=type(exc).__name__,
                    error=str(exc))
        return state, f"Not started: {plain_error(exc)}.", "error"
    return state, None, None


@router.post("/setup/action/{name}", response_class=HTMLResponse)
async def setup_action(request: Request, name: str):
    """Run one Setup button and return the steps partial, which polls while work runs.
    A second request while one runs answers 200 with the message and data-setup-status="busy":
    htmx 2 does not swap a 4xx response by default, so the operator would see nothing."""
    if name not in ACTIONS:
        return HTMLResponse("unknown action", status_code=404)
    state, message, status = await start_action(name)
    if status is not None:
        return await _steps_partial(request, state, message=message, setup_status=status)
    return await _steps_partial(request, state, message=message)


@router.get("/", response_class=HTMLResponse)
async def dashboard_with_banner(request: Request):
    """A fresh install opens the onboarding; an unfinished one gets the Finish setup banner."""
    from kestrel.web.pages.dashboard import dashboard

    async with sessionmaker()() as s:
        fresh = await ob.is_fresh_install(s)
        done = (not fresh) and await ob.is_complete(s)
    if fresh and FIRST_RUN_REDIRECT and not request.headers.get("HX-Request"):
        return RedirectResponse("/onboarding", status_code=303)
    response = await dashboard(request)
    gap = linux_gap_html()
    if done and not gap:
        return response
    body = bytes(response.body).decode("utf-8", "replace")
    m = _MAIN_TAG.search(body) or _BODY_TAG.search(body)
    if m is None:
        return response
    body = body[: m.end()] + gap + ("" if done else BANNER) + body[m.end() :]
    return HTMLResponse(body, status_code=response.status_code)


def linux_gap_html() -> str:
    """The Linux browser-fallback panel, or "" (kestrel.desktop.linux_check, release round
    R1). Set only in the desktop process after its window could not open."""
    from kestrel.desktop import linux_check

    gap = linux_check.fallback()
    if gap is None:
        return ""
    return templates().get_template("partials/linux_webkit.html").render(gap=gap)
