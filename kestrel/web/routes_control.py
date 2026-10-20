"""Tray/desktop control endpoints (SPEC-INTERFACES 1.1). The tray menu drives the worker and scans
through these; /api/status feeds the tray tooltip and icon state, and the same payload feeds the
sidebar's status rail (/status/rail) and nav count badges (/status/counts) in the web shell
(docs/UI_PROPOSAL.html). All loopback-only, all internal — none of this acts externally on the
operator's behalf (hard rule 1)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from kestrel import __version__
from kestrel.analytics.progress import scoring_progress
from kestrel.analytics.run_summary import rail_line, run_summary
from kestrel.analytics.today import failing_sources, latest_scan
from kestrel.config import MODES, HostedModeError, Settings
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    ScrapeRun,
    Source,
    Task,
    VerdictOverride,
)
from kestrel.llm.profiles import ollama_needed
from kestrel.services.setting_overrides import (
    load_settings_with_db,
    peek_run_mode,
    queue_run_mode,
)
from kestrel.web.app import current_run_mode, is_paused, set_paused
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.queries import live_activity
from kestrel.web.words import PLAIN


def _model_display_name(model_name: str) -> str:
    """Return a human-readable display name for a model.

    - Kestrel derived models (kestrel-tier1/2/3/docsel) show their plain tier label.
    - Other models have trailing :latest dropped.
    """
    if model_name.startswith("kestrel-"):
        # Extract tier: kestrel-tier1, kestrel-tier2, kestrel-tier3, kestrel-docsel
        suffix = model_name[len("kestrel-"):]
        if suffix.startswith("tier"):
            digits = "".join(ch for ch in suffix if ch.isdigit())
            if digits:
                key = f"tier{digits}"
                plain = PLAIN.get(key)
                if plain:
                    return plain
        elif suffix == "docsel":
            return "Doc select"
        # Fallback: return without kestrel- prefix
        return suffix
    # Drop trailing :latest for other models
    if model_name.endswith(":latest"):
        return model_name[:-7]
    return model_name


router = APIRouter()


@router.post("/api/run/now")
async def run_now(mode: str | None = None):
    """Queue the next worker cycle in `mode` ("local" or "hosted"; default the stored
    `llm.mode`). One-shot: the serve loop consumes the request on its next cycle and later cycles
    return to the stored mode. "hosted" is refused (400, with the reason) until the disclosure is
    acknowledged and tier1 and tier2 have hosted assignments. Queues only; never calls a model
    here, never acts externally."""
    async with sessionmaker()() as s:
        chosen = mode or (await load_settings_with_db(s)).llm.mode
        if chosen not in MODES:
            return JSONResponse(
                {"queued": False, "reason": f"unknown mode {chosen!r}; use local or hosted"},
                status_code=400,
            )
        try:
            await queue_run_mode(s, chosen)
            await s.commit()
        except HostedModeError as exc:
            return JSONResponse({"queued": False, "reason": plain_error(exc)}, status_code=400)
        except StaleDataError:  # the loop consumed the row mid-request: ask again, never a 500
            return JSONResponse(
                {"queued": False, "reason": "the queue changed while queuing; try again"},
                status_code=409,
            )
    return {"queued": True, "mode": chosen}


@router.post("/api/worker/pause")
async def worker_pause():
    set_paused(True)
    return {"paused": True}


@router.post("/api/worker/resume")
async def worker_resume():
    """Resume, and stamp the moment: a window with no open run never reaches back past the
    latest resume, so tasks that sat through a pause do not stretch "this run" (R3, D-UI-59);
    an open run's window is never cut by it (finding 2). The tray and the run panel's Resume
    button both call this; `HX-Trigger: run-refresh` makes the panels redraw at once."""
    from kestrel.analytics.run_summary import RESUMED_KEY
    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Setting

    set_paused(False)
    async with sessionmaker()() as s:
        row = await s.get(Setting, RESUMED_KEY)
        if row is None:
            s.add(Setting(key=RESUMED_KEY, value=utcnow_iso()))
        else:
            row.value = utcnow_iso()
        await s.commit()
    return JSONResponse({"paused": False}, headers={"HX-Trigger": "run-refresh"})


@router.post("/api/scan/now")
async def scan_now():
    """Enqueue the due source scrapes immediately (the tray "Run scan now"). Only enqueues work;
    the worker picks it up. Never fetches here, and never acts externally."""
    from kestrel.ingest.scheduler import enqueue_due_scrapes
    from kestrel.queue.queue import TaskQueue

    sm = sessionmaker()
    async with sm() as s:
        enqueued = await enqueue_due_scrapes(s, TaskQueue(sm), operator=True)
    return {"enqueued": enqueued}


@router.post("/api/scan/cancel")
async def scan_cancel():
    """Cancel the scan in progress: pending (and expired-lease) scrapes are cancelled, a leased
    one finishes its current source, and the automatic scheduler holds off for an hour
    (D-UI-35). Internal only; nothing is sent anywhere."""
    from kestrel.ingest.scheduler import cancel_pending_scrapes

    async with sessionmaker()() as s:
        cancelled = await cancel_pending_scrapes(s)
    return {"cancelled": cancelled}


# ------------------------------------------------------------------------------- scan button
async def scan_state(s: AsyncSession, activity: dict | None = None) -> dict[str, Any]:
    """Whether a scan is running (scrape tasks pending or leased) and the latest finished scan,
    whose Run id is the event the scan-finished toast keys on (D-UI-36)."""
    activity = activity if activity is not None else await live_activity(s)
    sc = (activity.get("tasks") or {}).get("scrape", {})
    pending, leased = int(sc.get("pending", 0)), int(sc.get("leased", 0))
    last = await latest_scan(s)
    tasks = activity.get("tasks") or {}
    # Roles still queued for a quick screen or a full review: the toast at the end of the
    # search step says "Still scoring N" instead of calling the run finished (D-UI-63).
    scoring = sum(
        int(tasks.get(t, {}).get("pending", 0)) + int(tasks.get(t, {}).get("leased", 0))
        for t in ("embed", "t1", "t2")
    )
    return {
        "scoring": scoring,
        "scanning": pending + leased > 0,
        "remaining": pending + leased,
        "pending": pending,
        "leased": leased,
        "event": last["id"] if last else None,
        "new": last["new"] if last else None,
        "errors": int(last.get("errors") or 0) if last else 0,
    }


def _scan_button(request: Request, state: dict[str, Any], msg: str | None = None):
    return templates().TemplateResponse(
        request, "partials/scan_button.html", {"scan_state": state, "scan_msg": msg}
    )


@router.get("/scan/button", response_class=HTMLResponse)
async def scan_button(request: Request):
    """The Scan now / Scanning button. Polls itself every 5 s only while a scan runs; the
    rail's out-of-band scan signal refreshes it when a scan starts elsewhere."""
    async with sessionmaker()() as s:
        state = await scan_state(s)
    return _scan_button(request, state)


# What a no-script Scan now submit reports back: a code in the URL, fixed copy here (D-UI-72).
SCAN_OUTCOMES: dict[str, str] = {
    "no_pick": "Not started: pick a model, or choose My usual models.",
    "bad_model": "Not started: that model is not available right now. Pick another.",
    "none": "No searches are on. Turn one on under Sources.",
}


def scan_outcome_text(params) -> str | None:
    """The fixed sentence for `?scan_outcome=`, or None for no or an unknown code."""
    return SCAN_OUTCOMES.get(str(params.get("scan_outcome") or ""))


@router.get("/scan/choose", response_class=HTMLResponse)
async def scan_choose(request: Request):
    """The Scan now dialog (D-UI-72): the Run now dialog for every search at once. Without script
    (`page=1`, no HX-Request) it is a page that posts back to Today."""
    import asyncio

    from kestrel.analytics.progress import tier2_label
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.pages import rescore as rs

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    local, local_note = await asyncio.to_thread(rs._local_models, settings)
    label, _name, _raw, hosted, reasons, hosted_note = await rs._hosted_models(settings)
    page = request.query_params.get("page") == "1" and not request.headers.get("HX-Request")
    ctx = {
        "form_action": "/scan/start",
        "form_target": "#scan-btn",
        "heading": "Scan all searches now",
        "cancel_href": "/",
        "scope_note": "Applies to the full review of the roles this scan finds, and only to "
                      "this scan. The quick screen always uses your usual model. Your settings "
                      "do not change, and later scans use your usual models. Company-filing and "
                      "similar searches always use your usual models.",
        "can_pick": True,
        "page": page,
        "current_label": tier2_label(settings),
        "local_models": local,
        "local_note": local_note,
        "hosted_label": label,
        "hosted_models": hosted,
        "hosted_reasons": reasons,
        "hosted_note": hosted_note,
        "hosted_prefix": rs.HOSTED_PREFIX,
        "hosted_while_local": settings.llm.mode != "hosted",
    }
    if page:
        ctx.update(title="Scan now", query=dict(request.query_params))
        return templates().TemplateResponse(request, "run_now_page.html", ctx)
    return templates().TemplateResponse(request, "partials/run_now_dialog.html", ctx)


@router.post("/scan/start", response_class=HTMLResponse)
async def scan_start(request: Request):
    """Scan now: queue a scrape for every enabled search, due or not (D-UI-35). Only enqueues;
    the worker loop runs them. The form may choose one model for this scan only (D-UI-72),
    validated as Run now and Score again validate it; it rides on each scrape task. A refused
    choice queues nothing. Without script the answer is a redirect to Today with a fixed code.
    The tray and MCP use /api/scan/now, which has no choice and uses the usual models."""
    from fastapi.responses import RedirectResponse

    from kestrel.ingest.scheduler import enqueue_all_scrapes
    from kestrel.queue.queue import TaskQueue
    from kestrel.web.pages.sources import _run_choice

    form = await request.form()
    # Only the no-script dialog page sends `next`; every other caller gets the button back.
    hx = bool(request.headers.get("HX-Request")) or form.get("next") is None
    sm = sessionmaker()
    async with sm() as s:
        override, label, refused = await _run_choice(s, None, form)
    if refused is not None:
        code, text = refused
        text = SCAN_OUTCOMES.get(code, text.replace("Not run:", "Not started:"))
        if not hx:
            return RedirectResponse(f"/?scan_outcome={code}", status_code=303)
        async with sm() as s:
            state = await scan_state(s)
        return _scan_button(request, state, text)
    async with sm() as s:
        queued = await enqueue_all_scrapes(
            s, TaskQueue(sm), model_override=override, model_label=label
        )
    async with sm() as s:
        state = await scan_state(s)
    msg = None
    if not queued and not state["scanning"]:
        msg = SCAN_OUTCOMES["none"]
    if not hx:
        return RedirectResponse("/" + ("?scan_outcome=none" if msg else ""), status_code=303)
    return _scan_button(request, state, msg)


@router.post("/scan/cancel", response_class=HTMLResponse)
async def scan_cancel_button(request: Request):
    """Cancel scan from the button. The search already running finishes its current source."""
    from kestrel.ingest.scheduler import cancel_pending_scrapes

    sm = sessionmaker()
    async with sm() as s:
        cancelled = await cancel_pending_scrapes(s)
    async with sm() as s:
        state = await scan_state(s)
    msg = f"Cancelled {cancelled} search{'es' if cancelled != 1 else ''}."
    if state["leased"]:
        msg += " The one in progress finishes first."
    return _scan_button(request, state, msg)


# ------------------------------------------------------------------------------ status payload
# Ollama's /api/ps answers in milliseconds when it is up and refuses instantly when it is not; a
# hung daemon is the one case the 2 s cap guards. Cached so the rail (every 10 s per open tab) and
# the tray (every few seconds) never stack probes.
_PS_TIMEOUT_S = 2.0
_PS_TTL_S = 8.0
# Windows takes about 2 s to refuse a loopback connect; a live Ollama accepts in milliseconds. A
# short connect timeout and a longer memory of "down" keep a stopped Ollama off every poll
# (D-UI-15).
_PS_CONNECT_TIMEOUT_S = 0.5
_PS_TTL_DOWN_S = 15.0
_ps_cache: dict[str, Any] = {"at": 0.0, "url": "", "result": None}


def reset_status_caches() -> None:
    """Forget the cached Ollama probe. Process-global state, so the test suite calls this between
    tests; nothing in the app needs it. The Setup probe's cache goes with it."""
    from kestrel.services.setup_state import forget_ollama_probe

    _ps_cache.update(at=0.0, url="", result=None)
    forget_ollama_probe()


async def _probe_ollama(native_url: str) -> dict[str, Any]:
    """{"up": bool, "loaded": [model names]} for the running Ollama, cached for a few seconds."""
    now = time.monotonic()
    cached = _ps_cache["result"]
    if cached is not None and _ps_cache["url"] == native_url:
        ttl = _PS_TTL_S if cached["up"] else _PS_TTL_DOWN_S
        if now - float(_ps_cache["at"]) < ttl:
            return cached
    result: dict[str, Any]
    try:
        timeout = httpx.Timeout(_PS_TIMEOUT_S, connect=_PS_CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{native_url.rstrip('/')}/api/ps")
            r.raise_for_status()
            body = r.json()
        loaded = [
            str(m.get("name") or m.get("model") or "")
            for m in (body.get("models") or [])
            if isinstance(m, dict)
        ]
        result = {"up": True, "loaded": [m for m in loaded if m]}
    except (httpx.HTTPError, ValueError):  # down, slow, or not JSON: the rail says so
        result = {"up": False, "loaded": []}
    _ps_cache.update(at=now, url=native_url, result=result)
    return result


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ago(value: str | None, now: datetime | None = None) -> str | None:
    """"2 min ago" style relative time for the rail, or None when the timestamp is unusable."""
    dt = _parse_iso(value)
    if dt is None:
        return None
    delta = (now or datetime.now(UTC)) - dt
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def _hhmm_local(value: str | None) -> str | None:
    dt = _parse_iso(value)
    return dt.astimezone().strftime("%H:%M") if dt else None


async def _worker_row(
    s: AsyncSession, activity: dict, mode: str, settings: Settings | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = activity.get("tasks") or {}
    t1 = tasks.get("t1", {})
    t2 = tasks.get("t2", {})
    n1 = t1.get("pending", 0) + t1.get("leased", 0)
    n2 = t2.get("pending", 0) + t2.get("leased", 0)
    latest = activity.get("latest_run")
    last_cycle = None
    if latest is not None:
        last_cycle = latest.finished_at or latest.started_at
    sc = tasks.get("scrape", {})
    n_scrape = sc.get("pending", 0) + sc.get("leased", 0)
    progress = await scoring_progress(s, settings)
    # The current step in words (D-UI-58, R1): the very string Today's run panel prints, from
    # the same global run summary, so the rail and the panel cannot disagree.
    summary = summary if summary is not None else await run_summary(s, None)
    step = rail_line(summary)
    if is_paused():
        # "Pausing: finishing N" while leased tasks finish, then "Paused" (finding 2).
        state, label = "warn", step or "Paused"
    elif step:
        state, label = "good", step
    elif n_scrape > 0:
        # A scan is work too; "idle" while searches run read as "nothing is happening" (D-UI-33).
        state = "good"
        label = f"Scanning · {n_scrape} search{'es' if n_scrape != 1 else ''} to go"
    elif progress["active"] and progress["total"]:
        # The same numbers as Today's progress panel (D-UI-49).
        done = (
            int(progress["scored"]) + int(progress["failed"])
            + int(progress.get("set_aside") or 0)
        )
        state, label = "good", f"Scoring · {done} of {progress['total']}"
    elif n1 + n2 > 0:
        tier = PLAIN["tier1"] if n1 else PLAIN["tier2"]
        state, label = "good", f"{tier} · {n1 + n2} waiting on {mode} models"
    elif progress.get("last_run"):
        # Idle after a run says how it ended instead of going quiet (D-UI-49).
        lr = progress["last_run"]
        state = "idle"
        label = (
            f"Scoring finished · {int(lr.get('scored') or 0)} scored, "
            f"{int(lr.get('failed') or 0)} failed"
        )
    else:
        state, label = "idle", PLAIN["worker_idle"]
        ago = _ago(last_cycle)
        if ago:
            label += f" · {ago}"
    # Open failures are shown on their own rail line, whatever the latest run said (D-UI-56).
    open_failed = int(progress.get("open_failed") or 0)
    return {
        "state": state, "label": label, "t1": n1, "t2": n2, "last_cycle_at": last_cycle,
        "open_failed": open_failed,
    }


async def _model_row(settings: Settings, mode: str) -> dict[str, Any]:
    if mode == "hosted":
        tier2 = settings.llm.hosted_tiers.get("tier2") or settings.llm.tiers.get("tier2")
        name = tier2.model if tier2 else "hosted"
        displayed = _model_display_name(name)
        return {"state": "good", "label": f"Hosted mode · {displayed}", "loaded": [], "up": None}
    if not ollama_needed(settings):  # no tier on Ollama: its absence is not a fault (D-SETUP-8)
        return {"state": "idle", "label": "Ollama not needed", "loaded": [], "up": None}
    ps = await _probe_ollama(settings.llm.ollama_native_url)
    if not ps["up"]:
        return {"state": "bad", "label": PLAIN["ollama_unreachable"], "loaded": [], "up": False}
    if ps["loaded"]:
        shown = ps["loaded"][0]
        extra = f" +{len(ps['loaded']) - 1}" if len(ps["loaded"]) > 1 else ""
        displayed = _model_display_name(shown)
        label = f"Ollama · {displayed}{extra} loaded"
        return {"state": "good", "label": label, "loaded": ps["loaded"],
                "up": True}
    return {"state": "idle", "label": "Ollama up · no model loaded", "loaded": [], "up": True}


async def _scan_row(s: AsyncSession) -> dict[str, Any]:
    last = (
        await s.execute(select(func.max(ScrapeRun.started_at)))
    ).scalar_one_or_none()
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    new_24h = (
        await s.execute(
            select(func.count()).select_from(Opportunity).where(Opportunity.first_seen_at >= since)
        )
    ).scalar_one()
    failing = (
        await s.execute(
            select(func.count()).select_from(Source).where(*failing_sources())
        )
    ).scalar_one()
    if failing:
        # A failed search must be visible without opening Sources (D-UI-33).
        return {"state": "bad",
                "label": f"{failing} search{'es' if failing != 1 else ''} failing · see Sources",
                "last_at": last, "new_24h": new_24h, "failing": failing}
    scan = await latest_scan(s)
    if scan and scan.get("errors"):
        # A scan whose searches crashed records no scrape_run and no source failure, so it
        # must be raised from the run record itself (review finding: a silent failed scan).
        e = int(scan["errors"])
        at_s = str(scan.get("at") or last or "")
        at = _hhmm_local(at_s) or at_s[:16]
        return {"state": "warn",
                "label": f"Last scan {at} had errors · {e} search{'es' if e != 1 else ''} failed",
                "last_at": scan.get("at") or last, "new_24h": new_24h, "errors": e}
    if last is None:
        return {"state": "idle", "label": "No scan yet", "last_at": None, "new_24h": new_24h}
    at = _hhmm_local(last) or last[:16]
    ago = _ago(last)
    stale = ago is not None and ago.endswith("d ago")
    return {
        "state": "warn" if stale else "idle",
        "label": f"Last scan {at} · {new_24h} new in 24 h",
        "last_at": last,
        "new_24h": new_24h,
    }


def _hosted_row(settings: Settings) -> dict[str, Any]:
    from kestrel.llm import providers as prov

    external = prov.external_backends(settings)
    present = [
        name for name, b in external.items()
        if bool(b.api_key) or prov.secret_present(b.api_key_env)
    ]
    if present:
        return {"state": "good", "label": f"Hosted key present · {present[0]}",
                "key_present": True}
    return {"state": "warn", "label": "Online deeper-read key: not set", "key_present": False}


async def _nav_counts(s: AsyncSession, track: str | None = None) -> dict[str, int]:
    """Cheap counts for the sidebar badges, each counting exactly the rows of the page or chip
    it opens, on the active tab's track (R8; None, the tray's /api/status: every track).

    - `shortlist` (Today): the Worth a look count, the one shared query.
    - `unrated` (Pipeline, D-UI-61): roles not yet rated, the Pipeline's own Being scored
      group (queries._group_expr), so the badge and that group's header always agree.
    - `calibrate`: Calibrate's candidate pool (learning.active.rank_uncertain before ranking):
      roles on the track with a Tier 2 evaluation, a description, not hidden (active.calibratable,
      #32) and no label or override on the track.
      Ranking loads embeddings, which is too much for a 30 s poll, so the pool is counted."""
    from kestrel.analytics.today import worth_a_look
    from kestrel.learning.active import calibratable
    from kestrel.web.fit import GROUP_NEW
    from kestrel.web.queries import GROUP_ORDER, OppFilters, _base_stmt, _group_expr, _with_operator

    shortlist = (await worth_a_look(s, track))["n"]
    unrated = (
        await s.execute(
            _with_operator(_base_stmt(OppFilters(track=track), func.count())).where(
                _group_expr() == GROUP_ORDER.index(GROUP_NEW)
            )
        )
    ).scalar_one()
    labeled = select(Label.opportunity_id)
    overridden = select(VerdictOverride.opportunity_id)
    if track:
        labeled = labeled.where(Label.track == track)
    pool = (
        select(func.count(func.distinct(Evaluation.opportunity_id)))
        .select_from(Evaluation)
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .where(
            Evaluation.tier == 2,
            calibratable(),
            Evaluation.opportunity_id.not_in(labeled.union(overridden)),
        )
    )
    if track:
        pool = pool.where(Opportunity.track == track)
    calibrate = (await s.execute(pool)).scalar_one()
    # No needs_review count (review finding 12): no nav badge shows it, so its out-of-band span
    # had no target and its query was wasted on every 30 s poll.
    out = {"shortlist": shortlist, "unrated": unrated, "calibrate": calibrate}
    out.update(await track_badges(s))
    return out


async def run_active(s: AsyncSession) -> bool:
    """Whether a run is active, cheaply, for the nav ring (D-UI-61): the worker is not paused and
    a scrape, quick screen or full review waits or runs on a track that is switched on. The same
    rule as run_summary's `active`, without the summary."""
    from kestrel.analytics.run_summary import _live_where

    if is_paused():
        return False
    found = (
        await s.execute(select(Task.id).where(_live_where("scrape", "t1", "t2")).limit(1))
    ).scalar_one_or_none()
    return found is not None


async def track_badges(s: AsyncSession) -> dict[str, int]:
    """The track tabs' badges (D-UI-57), keyed `track-<track>`: what waits for the operator on
    that track, which is its Worth a look count (the one shared query) plus its follow-ups due.
    Empty when only one track is in use: there are no tabs to badge."""
    from kestrel.analytics.today import act_summary, worth_a_look
    from kestrel.services.tracks import enabled_tracks_cached

    enabled = tuple(enabled_tracks_cached())
    if len(enabled) < 2:
        return {}
    out: dict[str, int] = {}
    for track in enabled:
        worth = (await worth_a_look(s, track))["n"]
        due = (await act_summary(s, track))["follow_up"]
        out[f"track-{track}"] = int(worth) + int(due)
    return out


async def _status_payload(s: AsyncSession, *, probe: bool = True) -> dict[str, Any]:
    activity = await live_activity(s)
    settings = await load_settings_with_db(s)
    mode = current_run_mode() or settings.llm.mode
    pending = await peek_run_mode(s)
    worker = await _worker_row(s, activity, mode, settings)
    # The model row follows the STORED mode, not the worker's last cycle: an idle worker keeps the
    # mode it last ran on, so after switching to Hosted the rail kept probing a stopped Ollama.
    model = await _model_row(settings, settings.llm.mode) if probe else None
    scan = await _scan_row(s)
    hosted = _hosted_row(settings)
    counts = await _nav_counts(s)
    scanning = await scan_state(s, activity)
    return {
        "version": __version__,
        "paused": is_paused(),
        "activity": activity,
        "mode": mode,
        "pending_mode": pending,
        "worker": worker,
        "model": model,
        "loaded_model": (model["loaded"][0] if model and model["loaded"] else None),
        "scan": scan,
        "hosted": hosted,
        "counts": counts,
        "scan_state": scanning,
    }


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    """The Run row in `activity` is an ORM object; the tray only reads the task counts."""
    activity = dict(payload["activity"])
    latest = activity.pop("latest_run", None)
    activity["latest_run"] = (
        {"id": latest.id, "status": latest.status, "started_at": latest.started_at,
         "finished_at": latest.finished_at}
        if latest is not None else None
    )
    return {**payload, "activity": activity}


@router.get("/api/status")
async def status():
    """Run progress + paused state + version + run mode for the tray tooltip and icon, plus the
    rail rows (worker, model, scan, hosted) and the nav counts. `mode` is what the worker is
    running on (the stored setting until its first cycle); `pending_mode` is a queued one-shot
    request not yet consumed; `loaded_model` is the first model Ollama reports loaded, or null."""
    async with sessionmaker()() as s:
        payload = await _status_payload(s)
    return _json_safe(payload)


@router.get("/status/rail", response_class=HTMLResponse)
async def status_rail(request: Request):
    """The sidebar status rail: four rows with a state dot each, polled by HTMX every 10 s."""
    async with sessionmaker()() as s:
        payload = await _status_payload(s)
    return templates().TemplateResponse(request, "partials/status_rail.html", payload)


@router.get("/status/counts", response_class=HTMLResponse)
async def status_counts(request: Request):
    """Sidebar count badges as HTMX out-of-band swaps, polled every 30 s. A zero renders empty
    so the badge hides (CSS :empty). The Pipeline badge follows the active tab; each track tab
    has its own badge (D-UI-57)."""
    from kestrel.web.deps import active_track

    async with sessionmaker()() as s:
        counts = await _nav_counts(s, active_track(request))
        running = await run_active(s)
    titles = {"unrated": "Roles not yet rated"}

    def title(key: str) -> str:
        # R12: the track tab badge keeps its tooltip through the out-of-band swap.
        t = "Waiting for you on this track" if key.startswith("track-") else titles.get(key)
        return f' title="{t}"' if t else ""

    spans = "".join(
        f'<span id="nav-count-{key}" class="n" hx-swap-oob="true"{title(key)}>'
        f'{n if n else ""}</span>'
        for key, n in counts.items()
    )
    # The ring beside the Pipeline badge, only while a run is active (D-UI-61).
    spans += (
        '<span id="nav-ring" class="ring" hx-swap-oob="true" title="A run is active"'
        f'{"" if running else " hidden"}></span>'
    )
    return HTMLResponse(spans)
