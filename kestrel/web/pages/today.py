"""Today, the / landing page (D-UI-49, D-UI-51, D-UI-59, D-UI-60). The persistent run panel on
top, always present (searching, rating, paused, or the last run), then the Review / Act / Schedule
cards, then the system's asks under a rule so they never outrank a role.

Every figure comes from kestrel.analytics.today and the one run summary per request
(kestrel.analytics.run_summary), which read existing rows only; no model call happens on the
landing page. The `first_run` context key is set by the setup flow (another module); when
present the template renders the first-run banner, otherwise nothing."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from kestrel.analytics.progress import scoring_progress
from kestrel.analytics.run_summary import run_summary
from kestrel.analytics.run_summary import view as run_view
from kestrel.analytics.today import (
    act_summary,
    dismiss_ask,
    ghosting_count,
    happening_now,
    honest_line,
    progress_view,
    switched_off_work,
    system_asks,
    worth_a_look,
)
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.routes_control import scan_outcome_text, scan_state

router = APIRouter()


def _track_param(request: Request) -> str:
    """The active tab's track (D-UI-57): never None, there is no "both" view."""
    from kestrel.web.deps import active_track

    return active_track(request)


async def _progress(s, settings=None) -> dict:
    return progress_view(await scoring_progress(s, settings))


@router.get("/", response_class=HTMLResponse)
async def today(request: Request):
    track = _track_param(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = {
            "title": "Today",
            "query": dict(request.query_params),
            "track": track,
            "today": date.today(),
            **(await run_panel(s, track, settings)),
            "act": await act_summary(s, track),
            "ghosting": {
                "n": await ghosting_count(s, track, days=settings.pursuits.ghosting_days),
                "days": settings.pursuits.ghosting_days,
            },
            "off_tracks": await switched_off_work(s),
            "schedule": await schedule_card(s, settings),
            "honest": await honest_line(s, track),
            "asks": await system_asks(s, settings, track),
            "scan_state": await scan_state(s),
            "scan_msg": scan_outcome_text(request.query_params),
            "confidential": settings.confidential_mode,
        }
    return templates().TemplateResponse(request, "dashboard.html", ctx)


async def run_panel(s, track: str, settings=None) -> dict:
    """Everything the persistent run panel and the Review card read (D-UI-59, D-UI-60), from ONE
    run summary per request (R1, R7): the run is global (one run scans every track); the
    additions and the Review count are the active tab's. `review.n` is worth_a_look's count, the
    number the Review card shows, so "now N in Review" can never differ from it."""
    from kestrel.analytics.progress import _model_in_use, tier2_pace_seconds
    from kestrel.analytics.run_summary import added_for, track_touched
    from kestrel.web.queries import open_quarantine_count

    if settings is None:
        settings = await load_settings_with_db(s)
    run = run_view(await run_summary(s, None))
    review = await worth_a_look(s, track, top=3)
    eta = model = None
    if run["active"] and run["stage"] in ("full_review", "quick_screen"):
        model = await _model_in_use(s, settings, True)
        pace = await tier2_pace_seconds(s, since=run["started_at"])
        remaining = int(run["rating_now"]) + int(run["waiting"])
        if pace is not None and remaining:
            eta = max(1, round(remaining * pace / 60.0))
    return {
        "run": run,
        "review": review,
        "added": added_for(run, track),
        "track_ran": await track_touched(s, track) if run["has_run"] else False,
        "rate_model": model,
        "eta_minutes": eta,
        "open_failed": await open_quarantine_count(s, track),  # D-UI-70
    }


async def schedule_card(s, settings) -> dict:
    """The Schedule card (D-UI-60): the mode in words, the next scheduled run and how the last
    one went. The status bar's tooltip is the run panel's step words (run_summary.step_words)."""
    from kestrel.analytics.run_summary import local_when
    from kestrel.services import run_schedule as rs
    from kestrel.web.app import current_run_mode

    sched, last = await rs.load(s)
    now_local = rs.local_now()
    nxt = rs.next_fire(sched, now_local, last)
    fired = await rs.last_fired(s)
    mode = current_run_mode() or settings.llm.mode
    if mode == "hosted":
        tier2 = settings.llm.hosted_tiers.get("tier2")
        free = bool(tier2 and str(tier2.model or "").endswith(":free"))
        mode_words = "Online model, free" if free else "Online model"
    else:
        mode_words = "Model on this computer"
    hour = (nxt.hour % 12 or 12) if nxt else None
    return {
        "kind": sched.kind,
        "mode_words": mode_words,
        "next": (f"{nxt:%a} {hour}:{nxt:%M} {'AM' if nxt.hour < 12 else 'PM'}" if nxt else None),
        "last": ({"when": local_when(fired["at"]), "outcome": fired["outcome"]}
                 if fired else None),
    }


async def _now(s, settings) -> dict:
    from kestrel.web.app import current_run_mode, is_paused

    return await happening_now(
        s, mode=current_run_mode() or settings.llm.mode, paused=is_paused()
    )


@router.get("/today/now", response_class=HTMLResponse)
async def today_now(request: Request):
    """The "Now" line (D-UI-37). Today shows the run panel instead (D-UI-59); the partial stays
    for any surface that embeds it."""
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = {"now": await _now(s, settings)}
    return templates().TemplateResponse(request, "partials/today_now.html", ctx)


@router.get("/today/progress", response_class=HTMLResponse)
async def today_progress(request: Request):
    """The old scoring progress panel (D-UI-49). Today renders the run panel instead (R1: one
    progress figure on screen); this partial is kept for callers outside Today."""
    async with sessionmaker()() as s:
        ctx = {"progress": await _progress(s)}
    return templates().TemplateResponse(request, "partials/today_progress.html", ctx)


@router.get("/today/run", response_class=HTMLResponse)
async def today_run(request: Request):
    """The persistent run panel and the Review card (D-UI-59, D-UI-60), re-polled together so
    the panel's "now N in Review" and the card's count come from one statement: every 5 s while
    a run is active, every 30 s idle or paused. The Review card arrives out of band."""
    track = _track_param(request)
    async with sessionmaker()() as s:
        ctx = {**(await run_panel(s, track)), "track": track, "oob": True}
    return templates().TemplateResponse(request, "partials/today_run.html", ctx)


@router.post("/today/asks/{key}/dismiss")
async def dismiss(key: str):
    """Wave away a dismissable ask for good (the calibrate ask). A settings row, not browser
    storage, like the guide strips."""
    async with sessionmaker()() as s:
        ok = await dismiss_ask(s, key)
    return Response(status_code=200 if ok else 404)
