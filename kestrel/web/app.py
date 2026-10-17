"""The FastAPI application (SPEC-INTERFACES 1.1, 2, 3). build_app() mounts the vendored static
assets, the page routes, and the /api routes, and — unless disabled — starts the pipeline worker
from the lifespan so it lives on uvicorn's single event loop (a second loop gives cross-loop
aiosqlite errors that look like corruption). The worker is optional so tests can drive the UI
without a live model."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kestrel import paths
from kestrel.web.auth import (
    auth_response,
    current_key,
    install_lan_auth,
    key_is_valid,
    local_addresses,
)
from kestrel.web.routes_api import router as api_router
from kestrel.web.routes_learning import router as learning_router
from kestrel.web.routes_pages import router as pages_router

log = structlog.get_logger()

# Per-page routers (SPEC-INTERFACES 2.1). Each page owns one module so the Phase 13 fan-out builds
# them independently; they are all included here.
_PAGE_MODULES = (
    # "setup" is first: it serves GET / ahead of the dashboard so a fresh install (no completed
    # onboarding, no profile.yml, no stored llm.mode) is sent to /onboarding and an unfinished
    # one gets the dashboard plus a "Finish setup" banner (D-UI-30).
    "setup", "onboarding",
    "dashboard", "chrome", "documents", "profile", "pursuits", "renders", "orgs", "stories",
    "prep", "mock", "offers", "sources", "settings", "analytics", "bench", "about",
    "escalate", "opportunity_actions", "runs_detail", "models", "backup", "applied",
    "rescore", "updates",
)

# Worker pause is a process-wide flag the tray toggles via /api/worker/pause|resume. A paused
# worker idles instead of draining, so a long run can be halted without killing the app.
_paused = asyncio.Event()


# Set while no worker cycle is running. reset_in_app pauses the worker, then waits on this so
# no cycle is writing while the tables are emptied (review finding 7).
_cycle_idle = asyncio.Event()
_cycle_idle.set()


async def wait_for_idle_cycle(timeout: float) -> bool:
    """True once no worker cycle is running, False if one is still running after `timeout`."""
    try:
        await asyncio.wait_for(_cycle_idle.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


def is_paused() -> bool:
    return _paused.is_set()


def set_paused(paused: bool) -> None:
    _paused.set() if paused else _paused.clear()


# How long a background loop waits before retrying its own SETUP after a failure. Module-level so a
# test can shorten it; the cycle-level idle waits below are separate and unchanged.
_SETUP_RETRY_SECONDS = 5.0


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep until `stop` is set or `seconds` pass, whichever is first."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


# Reranker drift (SPEC-MODULES 5.4) is checked at most this often while no Tier 1 work ran.
_DRIFT_INTERVAL_S = 900.0
_drift_last: dict[str, float] = {"at": 0.0}


def _drift_due(now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    if now - _drift_last["at"] < _DRIFT_INTERVAL_S:
        return False
    _drift_last["at"] = now
    return True


# The UI, the worker, the scheduler and every DB session share uvicorn's one event loop, so any
# synchronous work on it is a frozen click. This monitor names the stall in the log (D-UI-10):
# a tick that lands later than its due time by more than the threshold is logged with the
# measured delay, so "the app froze for a moment" can be matched to what the worker was doing.
_STALL_TICK_S = 0.5
_STALL_LOG_THRESHOLD_S = 0.25
# A request slower than this is logged with its path (the middleware in build_app).
_SLOW_REQUEST_MS = 400


async def _stall_monitor(stop: asyncio.Event, *, tick: float = _STALL_TICK_S) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        due = loop.time() + tick
        await asyncio.sleep(tick)
        late = loop.time() - due
        if late > _STALL_LOG_THRESHOLD_S:
            log.warning("event_loop_stall", stalled_ms=int(late * 1000))


def _log_task_failure(task: asyncio.Task) -> None:
    """Done callback for the lifespan's background tasks. A task that dies with an exception is
    otherwise reported only as "Task exception was never retrieved" at garbage collection, long
    after the worker has silently stopped advancing the pipeline for the life of the process."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "background_task_died",
            task=task.get_name(),
            error=str(exc),
            kind=type(exc).__name__,
        )


# Liveness rechecks (SPEC-MODULES 5.6) run on a cadence of days per item; the serve loop only needs
# to ASK once an hour, and the query itself decides what is due. Module-level so a restart asks
# again promptly rather than waiting an hour.
_LIVENESS_EVERY_S = 3600.0
_liveness_state: dict[str, float | None] = {"last": None}  # None = never asked this process


async def _maybe_run_liveness(sm, settings, cycle) -> dict | None:
    """Run one liveness pass if an hour has gone by. Never raises: liveness is a nicety layered on
    the pipeline and a bad network must not stop scoring."""
    import time

    from kestrel.ingest.http import HttpClient

    now = time.monotonic()
    last = _liveness_state["last"]
    if last is not None and now - last < _LIVENESS_EVERY_S:
        return None
    _liveness_state["last"] = now
    ing = settings.ingest
    try:
        async with HttpClient(
            user_agent=ing.user_agent,
            default_rate_per_sec=ing.default_rate_per_sec,
            timeout=ing.request_timeout_s,
            respect_robots=ing.respect_robots,
        ) as http:
            counts = await cycle(sm, settings, http)
        if counts.get("due"):
            log.info("liveness_cycle", **counts)
        return counts
    except Exception as exc:  # never let liveness take the worker down
        log.warning("liveness_cycle_error", error=str(exc))
        return None


# The mode ("local" or "hosted") the serve loop resolved for its current or latest cycle, for the
# status strip and /api/status. None until the first cycle of this process.
_run_mode: dict[str, str | None] = {"mode": None}


async def _rebuild_generated_sources() -> None:
    """Bring the profile-generated searches in line at every start (review finding 9): an install
    that predates a change to what is generated (D-SRC-6's neutral SEC watch) would otherwise
    keep the old row until the next profile save. Idempotent (no change, no write); each row
    keeps its own on or off. A failure is logged and never blocks startup."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.generated import rebuild_generated_sources
    from kestrel.services.profile_edit import profile_path

    try:
        if not profile_path().is_file():
            return  # no profile of the operator's yet: nothing generated to bring in line
        async with get_sessionmaker()() as s:
            result = await rebuild_generated_sources(s)
            await s.commit()
    except Exception as exc:  # an unreadable profile must not stop the app from starting
        log.warning("generated_sources_startup_rebuild_failed",
                    error=f"{type(exc).__name__}: {exc}")
        return
    if result.changed:
        log.info("generated_sources_startup_rebuild", created=len(result.created),
                 updated=len(result.updated), removed=len(result.removed))


async def _seed_shipped_sources() -> None:
    """Put the shipped sources.yml into the source table at every start (D-SRC-1). Matched by
    name and never overwriting a row, so the operator's edits survive; shipped sources are
    disabled, so nothing scrapes until the operator turns one on. Until 0.3.0 only the CLI did
    this, and a packaged install opened Sources to an empty table. First, the example sources
    retired in D-SRC-3 are deleted where the operator never enabled, edited or ran them."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.sources import (
        retire_example_sources,
        retire_linkedin_sources,
        sync_sources,
    )

    try:
        async with get_sessionmaker()() as s:
            retired = await retire_example_sources(s)
            unlinked = await retire_linkedin_sources(s)  # D-SRC-5
            await s.commit()
            created = await sync_sources(s)
    except Exception as exc:  # a bad sources.yml must not stop the app from starting
        log.warning("shipped_sources_seed_failed", error=f"{type(exc).__name__}: {exc}")
        return
    if retired:
        log.info("example_sources_retired", deleted=retired)
    if unlinked:
        log.info("linkedin_sources_retired", sources=unlinked)
    if created:
        log.info("shipped_sources_seeded", created=created)


async def _seed_shipped_layouts() -> None:
    """Put the eight shipped document layouts into the doc_layout table at every start
    (D-UI-22). Until 0.3.0 only `kestrel seed` did this, so a packaged install rendered with no
    layout at all and the review said "executive is not in the catalog"."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.docs.seed import _ensure_layouts

    try:
        async with get_sessionmaker()() as s:
            await _ensure_layouts(s)
            await s.commit()
    except Exception as exc:  # never a failed start
        log.warning("shipped_layouts_seed_failed", error=f"{type(exc).__name__}: {exc}")


async def _load_enabled_tracks() -> None:
    """Warm the enabled-tracks cache the templates read (D-UI-26)."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.tracks import keep_existing_choice, load_enabled

    try:
        async with get_sessionmaker()() as s:
            if await keep_existing_choice(s):
                await s.commit()
            await load_enabled(s)
    except Exception as exc:  # the default (employment only) stands
        log.warning("enabled_tracks_load_failed", error=f"{type(exc).__name__}: {exc}")


async def _apply_keep_awake() -> None:
    """Honor the stored keep-awake choice from the loop thread (D-RUNS-2), which is the thread
    that must hold the state. A failure is logged inside apply(); never a startup error."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import keep_awake

    try:
        async with get_sessionmaker()() as s:
            await keep_awake.apply_stored(s)
    except Exception as exc:  # the setting table is not worth a failed start
        log.warning("keep_awake_startup_failed", error=f"{type(exc).__name__}: {exc}")


async def _setup_models_loop(stop: asyncio.Event) -> None:
    """Model downloads and builds the Setup buttons queue (release round R1): task rows run
    through Ollama's HTTP API on this loop, beside the pipeline worker so a long download never
    holds up scoring and a long scoring cycle never holds up a download."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import model_tasks
    from kestrel.services.setting_overrides import load_settings_with_db

    sm = get_sessionmaker()

    async def native_url() -> str:
        async with sm() as s:
            return (await load_settings_with_db(s)).llm.ollama_native_url

    await model_tasks.run_loop(stop, sm, native_url)


def current_run_mode() -> str | None:
    return _run_mode["mode"]


async def _worker_loop(stop: asyncio.Event) -> None:
    """Continuously advance the pipeline on uvicorn's loop: sweep new opportunities through Tier 0,
    then drain Tier 1 fully, then Tier 2 fully (never interleaving the two models), then idle until
    there is more to do. Every unit of work is a task row, so this is resumable across restarts.

    Each cycle resolves its settings fresh: the stored `llm.mode` decides the tier set, unless
    "Run now" queued a one-shot mode (setting_overrides.take_run_mode), which this cycle consumes
    and the next one does not see."""
    import httpx

    from kestrel.config import HostedModeError, get_settings
    from kestrel.db.engine import get_sessionmaker
    from kestrel.intelligence.liveness import run_liveness_cycle
    from kestrel.interview.story_task import drain_story_builds
    from kestrel.learning.reranker import check_drift
    from kestrel.llm.client import LLMClient
    from kestrel.pipeline import runlog
    from kestrel.pipeline.tier0 import run_tier0_batch
    from kestrel.pipeline.tier1 import handle_t1
    from kestrel.pipeline.tier2 import handle_t2
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.services.setting_overrides import load_settings_with_db, take_run_mode

    sm = get_sessionmaker()
    async with httpx.AsyncClient() as http:
        # Setup is retried, not done once. It reads settings and touches the database (reclaim),
        # and a locked file or a bad settings.yml at launch used to raise here, outside any
        # try, which killed the loop silently for the life of the process: the app looked up and
        # nothing was ever evaluated again until a restart.
        worker: Worker | None = None
        # A cycle a pause cut short keeps its Run row open through the pause and the next cycle
        # carries on in it (review finding 2), so the run's counts never drop on resume.
        carried: runlog.CycleRun | None = None
        while not stop.is_set():
            if worker is None:
                try:
                    client = LLMClient(get_settings(), http=http)
                    candidate = Worker(sm, client, get_settings(), stop=stop, paused=is_paused)
                    await candidate.reclaim()
                    # A run this app left open when it was killed is closed, not left "running"
                    # forever on Today and the Runs page (hard rule 5).
                    await runlog.close_orphaned_runs(sm)
                    worker = candidate
                except Exception as exc:  # retry until it comes up; never die at startup
                    log.warning("serve_worker_setup_error", error=str(exc))
                    await _wait(stop, _SETUP_RETRY_SECONDS)
                    continue
            cycle: runlog.CycleRun | None = None
            try:
                if _paused.is_set():  # the tray paused the worker: idle without draining
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
                    continue
                _cycle_idle.clear()
                # Re-read settings each cycle from the setting-table layer so an accepted weight
                # calibration (or any UI setting change) takes effect with no restart. A queued
                # one-shot mode is consumed here, by exactly one cycle; one that can no longer
                # run (the acknowledgement or an assignment went away since it was queued) is
                # logged and the stored mode is used instead.
                async with sm() as s:
                    requested = await take_run_mode(s)
                    try:
                        settings = await load_settings_with_db(s, mode=requested)
                    except HostedModeError as exc:
                        log.warning("run_mode_unavailable", requested=requested, reason=str(exc))
                        settings = await load_settings_with_db(s)
                worker.apply_settings(settings)
                _run_mode["mode"] = settings.llm.mode
                # A Run row per cycle that has work (D-UI-34), so Today and Runs see what this
                # loop does; an idle cycle writes nothing.
                cycle, carried = carried or runlog.CycleRun(sm, mode=settings.llm.mode), None
                if await runlog.has_pipeline_work(sm):
                    await cycle.ensure_open()
                # Scheduled scrapes first: the scheduler, tray and MCP only enqueue them, and
                # Tier 0 below is what turns their new postings into pipeline work.
                scr = await worker.drain_scrapes()
                await cycle.record_scrapes(scr)
                counts = await run_tier0_batch(sm, settings=settings)
                await cycle.record("tier0", counts, did_work=counts.get("seen", 0) > 0)
                emb, t1, t2 = TierStats(), TierStats(), TierStats()
                # Embed first: the Tier 1 reranker gate and Tier 2 few-shot both look up the
                # opportunity's vector and degrade silently without one.
                await worker.drain_embed(emb)
                await cycle.record("embed", runlog.tier_counts(emb), did_work=emb.completed > 0)
                await worker.drain_tier("t1", settings.concurrency.tier1, handle_t1, t1)
                await cycle.record("tier1", runlog.tier_counts(t1), did_work=t1.completed > 0)
                await worker.drain_tier("t2", settings.concurrency.tier2, handle_t2, t2)
                await cycle.record("tier2", runlog.tier_counts(t2), did_work=t2.completed > 0)
                # Stories the operator asked to build from the My stories page (queued, never run
                # on the request thread).
                # A pause mid-cycle (finding 2): each drain above finished its leased task and
                # claimed no more; nothing further starts until the operator resumes.
                if not _paused.is_set():
                    await drain_story_builds(sm, client, settings, stop=stop)
                # Drift is a rolling window over the last 50 labels; checking it every idle
                # cycle (5 s) loaded both reranker pickles and every Org and Source row on the
                # UI's event loop for nothing. Once per interval, or after any Tier 1 work.
                if t1.completed or _drift_due():
                    for track in ("employment", "consulting"):
                        async with sm() as s:
                            await check_drift(s, track, settings)
                if not _paused.is_set():
                    await _maybe_run_liveness(sm, settings, run_liveness_cycle)
                if _paused.is_set() and cycle.run_id is not None and not stop.is_set():
                    carried = cycle  # paused mid-run: the run stays open until resumed
                else:
                    await cycle.finish("stopping" if stop.is_set() else "done")
                idle = (
                    counts["pass"] == 0
                    and scr.completed == 0
                    and emb.completed == 0
                    and t1.completed == 0
                    and t2.completed == 0
                )
            except Exception as exc:  # a serve worker must never die on one bad cycle
                log.warning("serve_worker_cycle_error", error=str(exc))
                idle = True
                if cycle is not None:
                    try:
                        await cycle.finish("failed", notes=f"{type(exc).__name__}: {exc}")
                    except Exception as close_exc:  # the restart sweep closes it instead
                        log.warning("cycle_run_close_failed", error=str(close_exc))
            finally:
                _cycle_idle.set()
            if idle:
                await _wait(stop, 5.0)
        if carried is not None:  # quit while paused: close the run this loop kept open
            try:
                await carried.finish("stopping")
            except Exception as close_exc:  # the restart sweep closes it instead
                log.warning("cycle_run_close_failed", error=str(close_exc))


async def _graceful_worker_shutdown(task: asyncio.Task, stop: asyncio.Event) -> None:
    """Stop the worker and RELEASE ITS LEASES (SPEC.md 1.3, FINDINGS F8). Signal the stop event so
    the drain loop exits after its in-flight tasks finish, wait up to 30s for that, then release any
    still-leased task back to pending so a restart resumes immediately rather than waiting for the
    lease to expire. Runs on uvicorn's loop (where the DB sessions live), never a second loop."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.queue.queue import TaskQueue

    stop.set()  # let the current drain finish its in-flight tasks and exit
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=30.0)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    try:
        released = await TaskQueue(get_sessionmaker()).release_all_leased()
        if released:
            log.info("released_leases_on_shutdown", count=released)
    except Exception as exc:  # shutdown must never raise; a stuck lease self-heals via reclaim
        log.warning("lease_release_failed", error=str(exc))


async def _delivery_loop(stop: asyncio.Event) -> None:
    """Push undelivered notifications on a cadence, batching to at most one per window (SPEC 6)."""
    from kestrel.config import get_settings
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services.delivery import deliver_pending

    sm = get_sessionmaker()
    while not stop.is_set():
        # Settings are read INSIDE the try: a bad settings.yml used to raise here, outside it, and
        # end the loop for the life of the process with nothing logged.
        wait_seconds = 60.0
        try:
            settings = get_settings()
            wait_seconds = float(max(5, settings.notify.batch_seconds))
            async with sm() as s:
                await deliver_pending(s, settings)
        except Exception as exc:  # a delivery tick must never take down the app
            log.warning("delivery_tick_error", error=str(exc))
        await _wait(stop, wait_seconds)


async def _scheduler_loop(stop: asyncio.Event) -> None:
    """Enqueue due source scrapes on a cadence (SPEC.md 5.3). Sources default disabled, so a fresh
    install never scrapes unattended. Runs on uvicorn's loop like the worker."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.ingest.scheduler import enqueue_due_scrapes
    from kestrel.queue.queue import TaskQueue
    from kestrel.services.run_schedule import fire_if_due

    sm = get_sessionmaker()
    queue = TaskQueue(sm)
    while not stop.is_set():
        if _paused.is_set():  # paused (the tray, or an in-app reset): enqueue nothing
            await _wait(stop, 5.0)
            continue
        try:
            async with sm() as s:
                await enqueue_due_scrapes(s, queue)
        except Exception as exc:  # a scheduler tick must never take down the app
            log.warning("scheduler_tick_error", error=str(exc))
        try:
            # The operator's daily or weekly run (SPEC.md 6.5): every enabled source, once per
            # local day or week, whatever its cadence says. Its own try: a failing cadence
            # pass (one bad source row) must not block the schedule.
            async with sm() as s:
                fired = await fire_if_due(s, queue)
            if fired is not None:
                log.info("scheduled_run_fired", enqueued=fired)
        except Exception as exc:
            log.warning("run_schedule_tick_error", error=str(exc))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60.0)


async def _update_loop(stop: asyncio.Event) -> None:
    """Ask the static feed whether a newer Kestrel exists (SPEC-INTERFACES 1.5). Never downloads.
    Settings are re-read every pass so the Settings toggle applies without a restart."""
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import updater
    from kestrel.services.setting_overrides import load_settings_with_db

    sm = get_sessionmaker()
    first_wait = float(updater.FIRST_CHECK_DELAY_SECONDS)
    try:
        async with sm() as s:
            hours = (await load_settings_with_db(s)).updates.check_hours
            first_wait = updater.seconds_until_next_check(
                await updater.load_state(s), hours, datetime.now(UTC)
            )
    except Exception as exc:  # fall back to the plain startup delay
        log.warning("update_wait_error", error_type=type(exc).__name__, error=str(exc))
    await _wait(stop, first_wait)
    while not stop.is_set():
        wait_seconds = 24 * 3600.0
        try:
            async with sm() as s:
                # The toggle lives in the setting table, which get_settings() does not read.
                cfg = (await load_settings_with_db(s)).updates
                wait_seconds = float(max(1, cfg.check_hours)) * 3600.0
                if cfg.enabled:
                    await updater.check_now(s, feed_url=cfg.feed_url)
        except Exception as exc:  # an update check must never take down the app
            log.warning("update_check_error", error_type=type(exc).__name__, error=str(exc))
        await _wait(stop, wait_seconds)


async def _mark_update_seen() -> None:
    from kestrel.db.engine import get_sessionmaker
    from kestrel.services import updater

    try:
        async with get_sessionmaker()() as s:
            previous = await updater.mark_seen(s)
            if previous:  # a real upgrade: drives the one-time "Here's what changed" card
                state = await updater.load_state(s)
                state.upgraded_from = previous
                await updater.save_state(s, state)
    except Exception as exc:  # recording the running version is a convenience only
        log.warning("update_mark_seen_failed", error_type=type(exc).__name__, error=str(exc))


# Loopback hostnames the UI can legitimately be served from. The port deliberately is NOT pinned:
# the app moves along the 8765 ladder, so the browser's Origin will carry whichever rung it took.
# urlsplit().hostname strips brackets, so an IPv6 literal arrives as "::1", never "[::1]".
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Browser-extension origins, allowlisted by name. An earlier cut trusted ANY non-http(s) scheme,
# which silently extended trust to data:, blob:, file: and about:. Browsers serialize those as
# "null" today, so it was not exploitable, but it depended on an unstated browser invariant. Naming
# the three extension schemes says what was actually meant.
_EXTENSION_SCHEMES = frozenset({"chrome-extension", "moz-extension", "safari-web-extension"})

# GET routes that CHANGE STATE, and so must be guarded like a POST despite the method.
# /renders/new creates a DocRender row and launches Edge through Playwright to write a PDF. Left
# under the GET exemption, a hostile page could drive it with a bare <img src=...> in a loop:
# unbounded headless-browser spawns and disk writes on the operator's machine, which is precisely
# the drive-by this guard exists to stop. The right long-term fix is to make it a POST; guarding it
# here closes the hole without breaking the existing links that reach it.
_UNSAFE_GETS = frozenset({"/renders/new"})


def _is_extension_origin(origin: str) -> bool:
    try:
        return urlsplit(origin).scheme in _EXTENSION_SCHEMES
    except ValueError:
        return False


def _is_own_origin(
    origin: str, *, bind_all: bool = False, public_hostnames: tuple[str, ...] = ()
) -> bool:
    """True if this Origin is Kestrel's own UI rather than a third-party page.

    An http(s) origin must be on a loopback host, unless the operator has turned on bind_all, in
    which case the UI is legitimately reached over the LAN and its Origin carries the LAN address.
    A malformed or "null" Origin is refused rather than guessed at. Extension origins are NOT own
    origins: they are handled separately by _extension_refusal, because the scheme alone names a
    whole class of software, not the one extension the operator installed.
    """
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.scheme == "https" and parts.hostname in public_hostnames:
        # Published through a tunnel (docs/WEBAPP.md): the browser's Origin is the public
        # hostname the operator listed. An exact, operator-declared name, https only, so a
        # DNS-rebound attacker's hostname is still refused.
        return True
    if bind_all:
        # LAN mode: the phone reaches the UI on this machine's LAN address, so that Origin has to
        # be accepted. Deliberately NOT "any hostname": accepting anything would mean an attacker's
        # own origin counted as ours and the guard would be off exactly when the app is most
        # exposed. Only addresses this machine actually answers on are allowed.
        return parts.hostname in set(local_addresses())
    return parts.hostname in _LOOPBACK_HOSTS


# Sec-Fetch-Site values that mean "another site initiated this". `same-origin` is our own UI's
# links, `none` is a typed URL or bookmark, and both are the operator acting deliberately.
_FOREIGN_FETCH_SITES = frozenset({"cross-site", "same-site"})


async def _extension_refusal(request: Request) -> Response | None:
    """The response that refuses an extension-origin state change, or None to let it through.

    A chrome-extension:// Origin used to be trusted by scheme, which trusted EVERY installed
    extension with a localhost host permission, not the one the operator installed on purpose. The
    Kestrel extension identifies itself with the LAN shared key in X-Kestrel-Key, so that key is
    required here even on loopback, where the LAN middleware is (correctly) exempt. Header only: a
    cookie is exactly the ambient credential a hostile extension could ride on.
    """
    expected = await current_key()
    if not expected:
        return JSONResponse(
            {
                "detail": (
                    "extension request refused: Kestrel has no shared secret yet. Generate one "
                    "under Settings > LAN, then paste it into the Kestrel Capture extension's "
                    "options page."
                )
            },
            status_code=403,
        )
    if not key_is_valid(request.headers.get("X-Kestrel-Key"), expected):
        return JSONResponse(
            {
                "detail": (
                    "extension request refused: the X-Kestrel-Key header is missing or wrong. "
                    "Paste the shared secret from Settings > LAN into the Kestrel Capture "
                    "extension's options page."
                )
            },
            status_code=403,
        )
    return None


async def _start_mcp_if_enabled() -> None:
    """Start the MCP server when `mcp.enabled` is on (the Settings switch row wins over YAML).
    A failure to read the settings or bind the port is logged; it never stops the app."""
    from kestrel.config import get_settings
    from kestrel.mcp import runtime as mcp_runtime

    try:
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        async with get_sessionmaker()() as s:
            settings = await load_settings_with_db(s)
    except Exception as exc:  # the DB can be mid-migration on a first start: fall back to YAML
        log.warning("mcp_settings_read_failed", error=f"{type(exc).__name__}: {exc}")
        settings = get_settings()
    if not settings.mcp.enabled:
        return
    # bind_all is read exactly as the web app's own guard reads it (get_settings at build), so
    # the MCP port is never more exposed than the web port.
    problem = await mcp_runtime.start(settings.mcp.port, bind_all=get_settings().server.bind_all)
    if problem:
        log.warning("mcp_not_started", reason=problem)


def build_app(*, worker: bool = True, scheduler: bool = False) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task: asyncio.Task | None = None
        sched: asyncio.Task | None = None
        mcp_task: asyncio.Task | None = None
        deliver: asyncio.Task | None = None
        stall: asyncio.Task | None = None
        models: asyncio.Task | None = None
        updates: asyncio.Task | None = None
        # Every startup, worker or not (finding 7): a CLI or UI-only start must not drop the
        # consulting track an existing install uses.
        await _load_enabled_tracks()
        if worker:
            await _seed_shipped_sources()
            await _rebuild_generated_sources()
            await _seed_shipped_layouts()
            await _apply_keep_awake()
            task = asyncio.create_task(_worker_loop(stop), name="worker")
            deliver = asyncio.create_task(_delivery_loop(stop), name="delivery")
            stall = asyncio.create_task(_stall_monitor(stop), name="stall-monitor")
            models = asyncio.create_task(_setup_models_loop(stop), name="setup-models")
            # Gated on `worker` like the delivery loop: tests build the app with worker=False, so
            # the update check (a network request) never runs there.
            await _mark_update_seen()
            updates = asyncio.create_task(_update_loop(stop), name="update-check")
        if scheduler:
            sched = asyncio.create_task(_scheduler_loop(stop), name="scheduler")
        for created in (task, deliver, sched, stall, models, updates):
            if created is not None:
                created.add_done_callback(_log_task_failure)
        # The MCP server is a separate SSE port, started only when enabled (default off). Guard on
        # `worker` so the UI/test app (worker=False) never opens the MCP port.
        # The switch is the setting-table layer over YAML (Settings > LAN, D-MCP-1), and
        # kestrel.mcp.runtime owns the task so the switch can start and stop it live.
        if worker:
            from kestrel.mcp import runtime as mcp_runtime

            mcp_runtime.set_live(True)
            await _start_mcp_if_enabled()
        try:
            yield
        finally:
            # Each shutdown step is guarded on its own (review finding 4): a failure in one is
            # logged and never skips the next, above all the worker's graceful stop, which
            # releases its leases.
            stop.set()
            if worker:
                from kestrel.mcp import runtime as mcp_runtime

                try:
                    await mcp_runtime.stop()
                except Exception as exc:
                    log.warning("shutdown_step_failed", step="mcp",
                                error=f"{type(exc).__name__}: {exc}")
                mcp_runtime.set_live(False)
            for extra in (sched, mcp_task, deliver, stall, models, updates):
                if extra is not None:
                    extra.cancel()
                    try:
                        await extra
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        log.warning("shutdown_step_failed", step=extra.get_name(),
                                    error=f"{type(exc).__name__}: {exc}")
            if task is not None:
                await _graceful_worker_shutdown(task, stop)

    app = FastAPI(title="Kestrel", lifespan=lifespan, docs_url="/docs")

    from kestrel.config import get_settings

    bind_all = get_settings().server.bind_all
    public_hostnames = tuple(h.strip().lower() for h in get_settings().server.public_hostnames if h)

    @app.middleware("http")
    async def _time_requests(request: Request, call_next: Callable) -> Response:
        """Name a slow answer in the log (D-UI-10). A click that waits about a second is either
        the server (this line shows it, with the path) or the window and its proxy settings
        (this line is absent); the event_loop_stall line beside it says whether the loop was
        blocked meanwhile. Paths only, never query strings or bodies."""
        started = time.monotonic()
        response = await call_next(request)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms >= _SLOW_REQUEST_MS and not request.url.path.startswith("/static/"):
            log.warning(
                "slow_request", path=request.url.path, method=request.method, ms=elapsed_ms
            )
        # Every page and partial reflects files and rows that change between two visits (a
        # saved profile, a finished import). Without this, WebView2 served a tab visited
        # before a save from its cache, with the old warning on it (D-UI-24). Static assets
        # keep their own caching.
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")
    async def _reject_cross_origin(request: Request, call_next: Callable) -> Response:
        """Refuse state-changing requests initiated by a third-party web page.

        The app listens on loopback with no authentication, and since AFTERACTION 3.9 it binds a
        DETERMINISTIC port (the 8765 ladder) so the browser extension can find it. That
        predictability is also what makes a drive-by request practical: any page the operator has
        open could otherwise POST to /api/scan/now or /sources/{id}/run-now and drive real outbound
        scrapes against job boards and SEC EDGAR from the operator's IP, which is hard rule 7.

        Keyed on Origin for unsafe METHODS because browsers send Origin on every cross-origin
        unsafe request, including form posts and no-cors sends. The state-changing GETs in
        _UNSAFE_GETS also consult Sec-Fetch-Site, because browsers OMIT Origin on a no-cors GET
        (<img src>, <link>, a navigation) and that is exactly how a hostile page would drive
        /renders/new. An extension origin must carry the shared key (see _extension_refusal). A
        request with neither Origin nor Sec-Fetch-Site (curl, the MCP server, tests) is not
        browser-driven and is left alone. The LAN shared-key middleware is a different control:
        it exempts loopback, so it does not cover this.
        """
        unsafe_get = request.url.path in _UNSAFE_GETS
        if request.method not in ("GET", "HEAD", "OPTIONS") or unsafe_get:
            origin = request.headers.get("origin")
            if origin:
                if _is_extension_origin(origin):
                    refusal = await _extension_refusal(request)
                    if refusal is not None:
                        return refusal
                elif not _is_own_origin(
                    origin, bind_all=bind_all, public_hostnames=public_hostnames
                ):
                    return JSONResponse(
                        {"detail": "cross-origin request refused"},
                        status_code=403,
                    )
            elif unsafe_get:
                # No Origin, so this is either a no-cors browser GET or a non-browser client.
                # Every browser that omits Origin here still sends Sec-Fetch-Site, so the
                # values that name another site are refused. `same-origin` (our own links),
                # `none` (typed URL, bookmark) and absent (no browser at all) are the operator.
                # `same-site` is refused too: on loopback it means another local server's page,
                # which is not ours.
                site = request.headers.get("sec-fetch-site", "").strip().lower()
                if site in _FOREIGN_FETCH_SITES:
                    return JSONResponse(
                        {"detail": "cross-site request refused"},
                        status_code=403,
                    )
        return await call_next(request)

    # LAN shared key: the third control, and the only one that stops an unauthenticated DEVICE.
    # Installed unconditionally, not just when bind_all is set, so that flipping bind_all can never
    # expose an unprotected app: the middleware exempts loopback anyway, so it is inert on a
    # loopback-only install. Reads the key per request so Settings can rotate it with no restart.
    @app.exception_handler(OverflowError)
    async def _id_out_of_range(request: Request, exc: OverflowError) -> Response:
        """An id larger than SQLite can store is a 404, not a server error.

        FastAPI parses `{id}` as a Python int, which is unbounded, while SQLite INTEGER is 64-bit,
        so `session.get(Model, 10**20)` raises OverflowError deep in the driver. That reached the
        operator as a 500 on 37 route/id combinations, and the per-route "never 500" handlers could
        not help: their error path re-renders the page, which re-runs the same failing lookup
        outside the guard. Handled once here, where it covers every route including the paginator's
        OFFSET. Such a row cannot exist, so "not found" is the honest answer.
        """
        log.info("id_out_of_range", path=request.url.path)
        return JSONResponse({"detail": "not found"}, status_code=404)

    install_lan_auth(app)

    @app.get("/auth")
    async def auth(request: Request, key: str = "", next: str = "/"):
        """One-scan join for a phone: validates the key, sets the cookie, and redirects, so the key
        never lingers in the address bar of every later page."""
        return auth_response(key, await current_key(), next)

    # Host validation, the other half of the same problem. The origin guard above stops a hostile
    # page WRITING; it cannot stop it READING, because DNS rebinding makes the attacker's own
    # document same-origin and the responses then become readable. That matters here: /settings
    # renders the LAN shared secret in cleartext, and the download routes return the operator's
    # resume and prep-pack PDFs. Pinning Host is what breaks rebinding, since the attacker cannot
    # forge it. Same control the MCP server already applies (kestrel/mcp/server.py sse_app), widened
    # the same way when the operator opts into serving beyond loopback.
    # NOT ["*"] under bind_all: that would re-open DNS rebinding at exactly the moment the app is
    # most exposed, and since a rebound request arrives on loopback the shared key does not backstop
    # it. Pinned to the addresses this machine actually answers on.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            *([*local_addresses(), "[::1]"] if bind_all else ["127.0.0.1", "localhost", "[::1]"]),
            *public_hostnames,  # the tunnel forwards the public Host header (docs/WEBAPP.md)
        ],
    )

    app.mount("/static", StaticFiles(directory=paths.web_static_dir()), name="static")
    app.include_router(api_router)
    app.include_router(learning_router)
    app.include_router(pages_router)
    from kestrel.web.routes_control import router as control_router

    app.include_router(control_router)
    import importlib

    for name in _PAGE_MODULES:
        module = importlib.import_module(f"kestrel.web.pages.{name}")
        app.include_router(module.router)
    return app
