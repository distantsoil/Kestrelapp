"""Today, the / landing page (D-UI-92, D-UI-156). A greeting by first name over the page's
scene, a three-segment status strip (Last run fading to Next run, New roles found, the model),
the retry note when something needs it, New signals (Consulting), a Top matches carousel of
today's new roles, Decisions waiting and Applications needing action.

Every figure comes from existing rows (kestrel.web.today_view, kestrel.analytics.today and the
one run summary per request); no model is called and nothing is sent anywhere on this page. The
role actions reuse the Pipeline's own routes (/opportunities/{id}/decide/...), so Shortlist,
Save for later and Not for me behave, and Undo, exactly as there. The `first_run` flag marks the
Today that carries the walkthrough (D-UI-116)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from kestrel.analytics.progress import scoring_progress
from kestrel.analytics.run_summary import run_summary
from kestrel.analytics.run_summary import view as run_view
from kestrel.analytics.today import (
    dismiss_ask,
    happening_now,
    progress_view,
    switched_off_work,
    worth_a_look,
)
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import today_view as tv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.routes_control import scan_outcome_text, scan_state

router = APIRouter()


def _track_param(request: Request) -> str:
    """The active tab's track (D-UI-57): never None, there is no "both" view."""
    from kestrel.web.deps import active_track

    return active_track(request)


async def _progress(s, settings=None) -> dict:
    return progress_view(await scoring_progress(s, settings))


def _tour_wanted(request: Request) -> bool:
    return request.query_params.get("tour") == "1"


@router.get("/", response_class=HTMLResponse)
async def today(request: Request):
    track = _track_param(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = await page_context(s, request, track, settings)
    return templates().TemplateResponse(request, "dashboard.html", ctx)


async def page_context(s, request: Request, track: str, settings) -> dict:
    """Everything dashboard.html prints (D-UI-92, D-UI-156). One run summary per request feeds the
    strip, the subtitle and the retry note, so the three can never disagree."""
    import asyncio

    consulting = track == "consulting"
    panel = await run_panel(s, track, settings)
    # The first-run Today (the one that carries the walkthrough, D-UI-116): no run has ever
    # happened, or the walkthrough was asked for. It shows no retry note and no problem from a
    # past run.
    first_run = (not panel["run"]["has_run"]) or _tour_wanted(request)
    name = await asyncio.to_thread(tv.first_name_for, track)
    matches = await tv.top_matches(s, track, settings=settings)
    return {
        "title": "Today",
        "query": dict(request.query_params),
        "track": track,
        "consulting": consulting,
        "today": date.today(),
        "greeting": tv.greeting(name),
        "first_run": first_run,
        "tour": _tour_wanted(request),
        "strip": await strip_context(s, track, settings, panel, first_run=first_run),
        "retry": retry_note(panel, first_run=first_run),
        "matches": matches,
        "found_line": found_line(matches, consulting, panel),
        "signals": await tv.new_signals(s) if consulting else None,
        "decisions": await tv.decisions_waiting(s, track),
        "apps": await tv.applications_needing_action(s, track),
        "off_tracks": await switched_off_work(s),
        "scan_state": await scan_state(s),
        "scan_msg": scan_outcome_text(request.query_params),
        "confidential": settings.confidential_mode,
        "finish": await tv.finish_setup(s),
    }


def retry_note(panel: dict, *, first_run: bool) -> dict | None:
    """"N roles need another try ..." shown only when something needs it: never on the first-run
    Today, and never for a past run's problems (it counts the roles still waiting now)."""
    n = int(panel.get("open_failed") or 0)
    if first_run or n < 1:
        return None
    return {"n": n}


def found_line(matches: dict, consulting: bool, panel: dict) -> str:
    """The line beside "Top matches today": how many roles were found today, or why there are
    none (all done, or the next run)."""
    found = int(matches["found"])
    noun = ("opportunity" if found == 1 else "opportunities") if consulting else (
        "role" if found == 1 else "roles")
    if matches["cards"] or matches.get("pending", 0):
        return f"{found} new {noun} found today"
    if found:
        return "All caught up"
    return "Nothing found yet today"


async def strip_context(s, track: str, settings, panel: dict | None = None, *,
                        first_run: bool = False) -> dict:
    """The three segments of the status strip (D-UI-92). The run is global, one figure for every
    track. Last run cross-fades to Next run and holds on Last run while a problem exists."""
    from kestrel.web.app import current_run_mode
    from kestrel.web.pages.runs_page import _model_card

    panel = panel or await run_panel(s, track, settings)
    run = panel["run"]
    sched = await schedule_card(s, settings)
    mode = current_run_mode() or settings.llm.mode
    card = await _model_card(s, settings, None, mode in ("hosted", "both"))
    problem = bool(not first_run and (run["problem_count"] or run["failed"]))
    n_problems = int(run["problem_count"]) + (1 if run["failed"] else 0)
    state = ("paused" if run["paused"] else "active" if run["active"]
             else "last" if run["has_run"] else "none")
    pending = int(run["waiting"]) + int(run["rating_now"])
    return {
        "state": state,
        "run": run,
        "problem": problem,
        "n_problems": n_problems,
        "failed": int(run["failed"]) if not first_run else 0,
        "next": sched["next"],
        "schedule_kind": sched["kind"],
        "when": run.get("when"),
        "found": int(run["found_new"]),
        "rated": int(run["rated"]),
        "pending": pending,
        "eta": panel.get("eta_minutes"),
        "scanning_label": run["headline"],
        "model": tv.model_segment(card, mode),
        "busy": state == "active",
        "pct": _bar_pct(run),
    }


def _bar_pct(run: dict) -> dict:
    """The width of the done and in-progress parts of the strip's bar."""
    if run["stage"] == "searching":
        b = run["bars"][0]
        total = b["total"] or 0
        now = 100 / total if total and b["done"] < total else 0
        return {"done": b["pct"], "now": round(min(now, 100 - b["pct"]), 1)}
    rw = run["rate_widths"]
    return {"done": rw["scored"], "now": round(rw["running"], 1)}


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
    from kestrel.date_display import format_display
    return {
        "kind": sched.kind,
        "mode_words": mode_words,
        "next": format_display(nxt, "weekday", kind="datetime") if nxt else None,
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


# ------------------------------------------------------------------ in-place section refreshes
@router.get("/today/strip", response_class=HTMLResponse)
async def today_strip(request: Request):
    """The status strip alone, so its live counts update in place (every 5 s while a search runs
    or a role is being rated, every 30 s otherwise) without redrawing the page."""
    track = _track_param(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        panel = await run_panel(s, track, settings)
        first_run = (not panel["run"]["has_run"]) or request.query_params.get("tour") == "1"
        ctx = {
            "track": track,
            "strip": await strip_context(s, track, settings, panel, first_run=first_run),
            "retry": retry_note(panel, first_run=first_run),
            "scan_state": await scan_state(s),
            "scan_msg": None,
            "tour": request.query_params.get("tour") == "1",
        }
    return templates().TemplateResponse(request, "partials/td_strip.html", ctx)


@router.get("/today/matches", response_class=HTMLResponse)
async def today_matches(request: Request):
    """The Top matches section alone, asked again after an Undo."""
    track = _track_param(request)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        panel = await run_panel(s, track, settings)
        matches = await tv.top_matches(s, track, settings=settings)
        ctx = {
            "track": track, "consulting": track == "consulting", "matches": matches,
            "found_line": found_line(matches, track == "consulting", panel),
        }
    return templates().TemplateResponse(request, "partials/td_matches.html", ctx)


@router.get("/today/matches/new")
async def today_matches_new(request: Request, have: str = "", pending: str = ""):
    """Cards the page does not have yet (a role finished rating during a scan), each with the
    card it sits before, as rendered HTML. The page opens a gap at that position, so the view
    does not jump (live updates, D-UI-156)."""
    from fastapi.responses import JSONResponse

    track = _track_param(request)
    ids = {int(x) for x in have.split(",") if x.strip().isdigit()}
    waiting = {int(x) for x in pending.split(",") if x.strip().isdigit()}
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        data = await tv.cards_after(s, track, ids, settings, pending=waiting)
        panel = await run_panel(s, track, settings)
        matches = await tv.top_matches(s, track, settings=settings)
    tpl = templates().get_template("partials/td_card.html")
    return JSONResponse(
        {
            "new": [
                {"id": n["card"]["id"], "before": n["before"],
                 "strong": n["card"]["strong"], "title": n["card"]["title"],
                 "score": n["card"]["score"],
                 "html": tpl.module.card(n["card"], track == "consulting")}
                for n in data["new"]
            ],
            "done": [
                {"id": c["id"], "html": tpl.module.card(c, track == "consulting")}
                for c in data["done"]
            ],
            "ids": data["ids"],
            "pending": matches["pending"],
            "found_line": found_line(matches, track == "consulting", panel),
        }
    )


@router.get("/today/signals", response_class=HTMLResponse)
async def today_signals(request: Request):
    """New signals alone (Consulting), asked again after an action or an Undo."""
    async with sessionmaker()() as s:
        signals = await tv.new_signals(s)
    if not signals["rows"]:
        return HTMLResponse('<section id="td-signals" hidden></section>')
    return templates().TemplateResponse(request, "partials/td_signals.html",
                                        {"signals": signals})


@router.get("/today/decisions", response_class=HTMLResponse)
async def today_decisions(request: Request):
    track = _track_param(request)
    async with sessionmaker()() as s:
        ctx = {"track": track, "consulting": track == "consulting",
               "decisions": await tv.decisions_waiting(s, track)}
    return templates().TemplateResponse(request, "partials/td_decisions.html", ctx)


@router.get("/today/applications", response_class=HTMLResponse)
async def today_applications(request: Request):
    track = _track_param(request)
    async with sessionmaker()() as s:
        ctx = {"track": track, "consulting": track == "consulting",
               "apps": await tv.applications_needing_action(s, track),
               "off_tracks": await switched_off_work(s)}
    return templates().TemplateResponse(request, "partials/td_apps.html", ctx)


@router.get("/today/roles/{opp_id}/summary", response_class=HTMLResponse)
async def today_summary(request: Request, opp_id: int):
    """The role summary that slides in on Review: the Pipeline panel's own read of the role
    (kestrel.web.pages.pipeline.panel_context), printed for Today. Read only; nothing here
    writes, and Review alone never decides."""
    from kestrel.web.pages.pipeline import panel_context

    async with sessionmaker()() as s:
        ctx = await panel_context(s, opp_id)
    if ctx is None:
        html = templates().get_template("partials/td_summary.html").render(
            request=request, kind="missing"
        )
        return HTMLResponse(html, status_code=404)
    return templates().TemplateResponse(request, "partials/td_summary.html", ctx)


def _events(events: dict[str, Any]) -> dict[str, str]:
    import json

    return {"HX-Trigger": json.dumps(events, ensure_ascii=True), "HX-Reswap": "none"}


@router.post("/today/decisions/{pursuit_id}/applied", response_class=HTMLResponse)
async def decision_applied(pursuit_id: int):
    """"I applied" (or "I reached out" in Consulting) from Decisions waiting: the Applications
    verb, recorded as of today with the usual follow-up, with an Undo that reverts exactly this."""
    return await _verb(pursuit_id, "applied")


@router.post("/today/applications/{pursuit_id}/follow-again", response_class=HTMLResponse)
async def application_follow_again(pursuit_id: int):
    """"Follow up again": the Applications verb that moves the next follow-up a week out."""
    return await _verb(pursuit_id, "follow_again")


async def _verb(pursuit_id: int, verb: str) -> Response:
    from kestrel.db.models import Pursuit
    from kestrel.services import pursuit_board as board_svc
    from kestrel.services import pursuit_undo as undo
    from kestrel.web.pages.pursuits import _trigger, _verb_message

    async with sessionmaker()() as s:
        p = await s.get(Pursuit, pursuit_id)
        if p is None:
            return Response("", headers=_events(
                {"k-toast": {"message": "That one is gone.", "tone": "warn"}}))
        snap = await undo.snapshot(s, p)
        err = await board_svc.apply_verb(s, p, verb, {})
        if err:
            await s.rollback()
            return Response("", headers=_events({"k-toast": {"message": err, "tone": "warn"}}))
        token = await undo.save(s, snap)
        await s.commit()
        message = _verb_message(p.track, verb, p)
    import json

    toast = json.loads(_trigger(message, token))
    return Response("", headers={**_events({**toast, "td-refresh": True})})
