"""The pipeline worker (SPEC.md 6.6). It drains one tier fully before starting the next, because
Tier 1 and Tier 2 use different models and 16GB of VRAM cannot hold both without thrash (unload +
reload on every task). Within a tier it runs settings.concurrency.{tier1,tier2} leases in parallel.

Every unit of work is a leased Task row, so a kill mid-run resumes cleanly: reclaim_expired returns
crashed leases, handlers are idempotent on stage, and enqueue is idempotent per opportunity, so
restart neither loses nor duplicates work. A graceful stop lets in-flight tasks finish and leaves
the rest pending. Progress reports per-tier throughput and an ETA from the trailing 20 completions.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

import structlog
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import RUN_CARD_KEYS, HostedMode, Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, ScrapeRun, Setting, Source, Task
from kestrel.errors import failure_record
from kestrel.llm import credit
from kestrel.llm.client import LLMClient, tier_credit_hold
from kestrel.llm.engine_cycle import engine_unreachable
from kestrel.pipeline import lane, recheck, runlog
from kestrel.pipeline._shared import (
    HandlerOutcome,
    ModelOverrideError,
    client_for,
    settings_with_override,
)
from kestrel.pipeline.embed import enqueue_stale_embeddings, handle_embed
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tier0 import run_tier0_batch
from kestrel.pipeline.tier1 import handle_t1
from kestrel.pipeline.tier2 import handle_t2
from kestrel.queue.queue import (
    REASON_APPROVAL,
    REASON_CORRECTIONS,
    REASON_CREDIT,
    REASON_LOCAL_ONLY,
    TaskQueue,
    claimable,
    in_lane,
)
from kestrel.services.run_model import SWITCHED_TO_COMPUTER_NOTE, T2_ONLY_KEY

log = structlog.get_logger()

# A scoring task can outlive its lease. Tier 2's per-request timeout is 600s and `run_guarded`
# may issue an attempt, then a repair, then a reasoning-off re-ask, so one task can run far longer
# than any single request. Without a heartbeat the lease expires mid-call, `claimable()` reports
# the row as available, and the next `Worker.run()` reclaims and re-leases it while the first
# handler is still scoring: two handlers write duplicate Evaluations and race on `opp.stage`, so a
# role one handler wrote as `t1_reject` can lose to a `t1_pass` (hard rule 4: a wrongly rejected
# opportunity is invisible and unrecoverable), and the tokens are spent twice.
#
# Two defences, because they fail independently: the lease is longer than one request, and a
# heartbeat renews it every _RENEW_S for as long as the handler runs. Mirrors
# kestrel/interview/story_task.py, which already does this for the story build.
_TASK_LEASE_S = 900
_RENEW_S = 120
# After "database is locked" a renewal is retried this soon, doubling back up to _RENEW_S.
_RENEW_LOCK_BACKOFF_S = 5.0
# Where a drained task type's counts live on RunStats, so a crashed worker is recorded where the
# run summary and the live run panel actually read it.
_STATS_FIELD = {"t1": "tier1", "t2": "tier2", "embed": "embed"}


async def _renew_lease(queue: Any, task_id: int, worker_id: str) -> None:
    """Keep one task's lease alive while its handler runs (hard rule 5, resumability).

    Renewal is best effort and never aborts the scoring: SQLite answering "database is locked"
    backs off and tries again shortly, any other error waits for the next beat, and a lease
    actually lost (heartbeat False: reclaimed, or another worker took it) stops renewing. Cancelled
    by the worker when the handler returns."""
    from sqlalchemy.exc import OperationalError

    from kestrel.queue.queue import _is_lock_error

    delay = _RENEW_S
    backoff = _RENEW_LOCK_BACKOFF_S
    while True:
        await asyncio.sleep(delay)
        try:
            alive = await queue.heartbeat(
                task_id, worker_id, lease_seconds=_TASK_LEASE_S
            )
        except OperationalError as exc:
            if not _is_lock_error(exc):
                log.warning("pipeline_renew_error", task=task_id, error=str(exc))
                delay = _RENEW_S
                continue
            log.info("pipeline_renew_locked", task=task_id, retry_in_s=backoff)
            delay = backoff
            backoff = min(backoff * 2, _RENEW_S)
            continue
        except Exception as exc:  # noqa: BLE001 - a missed renewal must not kill the scoring
            log.warning("pipeline_renew_error", task=task_id, error=str(exc))
            delay = _RENEW_S
            continue
        if not alive:
            log.warning("pipeline_lease_lost", task=task_id, worker=worker_id)
            return
        delay, backoff = _RENEW_S, _RENEW_LOCK_BACKOFF_S

ProgressCb = Callable[[str], None]

# The tier whose model a task's `model_override` replaces, per task type. On a t1 task only a
# Score again (`rescore`) override applies to Tier 1 (D-UI-50); a run's one-run model rides on a
# t1 task as T2_ONLY_KEY instead and reaches Tier 2 alone (D-UI-72).
_OVERRIDE_TIER = {"t1": "tier1", "t2": "tier2"}
# The task types the engine preflight judges, by tier (D-WORKER-6). The embed tier runs on
# Ollama unless it is the in-process model (SPEC.md 8.1.1); it takes no per-task model.
_ENGINE_TIER = {**_OVERRIDE_TIER, "embed": "embed"}
# Every tier a drain may send to Ollama (the scoring and embed tiers, and tier3 for letters,
# follow-ups and research): a cycle probes once when any of them is on Ollama.
_PROBED_TIERS = ("tier1", "tier2", "tier3", "embed")


def _tier_external(settings: Settings, tier: str) -> bool:
    """Whether `tier` names an external backend (whatever Hosted mode says)."""
    tcfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(tcfg.backend) if tcfg else None
    return bool(backend and backend.external)


# Why scoring waits on the local AI engine, per tier ("tier1", "tier2"), while a drain finds it
# not ready (D-WORKER-6): ENGINE_DOWN when nothing answers at Ollama's address, MODEL_MISSING when
# the tier's model is not among its installed models. Set and cleared by Worker.drain_tier at the
# start of each Tier 1 and Tier 2 drain; web.unscored reads it so a waiting role says why. One
# process holds the worker and the pages, so a module-level record is the shared state.
ENGINE_DOWN = "engine_down"
MODEL_MISSING = "model_missing"
_ENGINE_WAIT: dict[str, str] = {}


def engine_wait() -> dict[str, str]:
    """{tier: reason} for every tier whose scoring is waiting on the local engine now."""
    return dict(_ENGINE_WAIT)


def engine_waiting_types() -> tuple[str, ...]:
    """The scoring task types whose tier the preflight is waiting on, by the last record. The
    cycle's work check uses Worker.engine_skip_types, which probes again first."""
    waiting = set(_ENGINE_WAIT)
    return tuple(t for t, tier in _ENGINE_TIER.items() if tier in waiting)


def _ollama_model(settings: Settings, tier: str) -> str | None:
    """The model `tier` would ask Ollama for, or None when the tier does not run on Ollama (an
    online backend, the in-process engine, or another local server at another address, which
    the Ollama probe cannot speak for)."""
    tcfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(tcfg.backend) if tcfg else None
    if tcfg is None or backend is None or backend.engine != "http":
        return None
    from kestrel.llm.engine_cycle import same_engine

    if not same_engine(backend.base_url, settings.llm.ollama_native_url):
        return None  # localhost, 127.0.0.1 and ::1 are one host (round 6)
    return str(tcfg.model)


def probe_engine(
    settings: Settings, *, fresh: bool = False
) -> tuple[str | None, list[str]] | None:
    """The Ollama probe the preflight judges by: setup_state.probe_ollama, the one Setup and
    Models use (short connect timeout; cached a few seconds unless `fresh`). Sync: called via
    to_thread. A module-level seam so the test suite, which has no Ollama, can stand it down."""
    from kestrel.services.setup_state import probe_ollama

    return probe_ollama(settings, fresh=fresh)


async def begin_engine_cycle(settings: Settings) -> None:
    """Take this cycle's one engine answer (llm.engine_cycle, D-WORKER-6 amended, rounds 4 and
    5): a fresh probe when any engine tier or drain tier runs on Ollama, none otherwise (an
    all-online setup never touches Ollama). The work check and every drain read it; a call that
    cannot connect turns it to "down" for the rest of the cycle."""
    from kestrel.llm import engine_cycle

    if not any(_ollama_model(settings, t) for t in _PROBED_TIERS):
        engine_cycle.start(None)
        return
    probe = await asyncio.to_thread(probe_engine, settings, fresh=True)
    engine_cycle.start(settings.llm.ollama_native_url, probe)


def end_engine_cycle() -> None:
    """Forget the cycle's answer and any loss, so nothing outside a cycle reads a stale one."""
    from kestrel.llm import engine_cycle

    engine_cycle.stop()


async def _engine_probe_for(settings: Settings) -> tuple[str | None, list[str]] | None:
    """The cycle's answer for this Ollama address when a cycle holds one, else a probe now."""
    from kestrel.llm import engine_cycle

    held, probe = engine_cycle.answer(settings.llm.ollama_native_url)
    if held:
        return probe
    return await asyncio.to_thread(probe_engine, settings)


def engine_problem(
    settings: Settings, tier: str, probe: tuple[str | None, list[str]] | None
) -> str | None:
    """Why `tier` cannot run on Ollama now, from a setup_state.probe_ollama result, or None
    (ready, not an Ollama tier, or not probed)."""
    model = _ollama_model(settings, tier)
    if model is None or probe is None:
        return None
    version, tags = probe
    if version is None:
        return ENGINE_DOWN
    from kestrel.setup import _tag_present

    return None if _tag_present(model, tags) else MODEL_MISSING


def _note_engine(tier: str, problem: str | None, model: str | None) -> None:
    """Record why `tier` waits on the local engine (or that it no longer does) and log a change
    once: the one place the record changes, for the scoring drains and every other drain."""
    before = _ENGINE_WAIT.get(tier)
    if problem is None:
        _ENGINE_WAIT.pop(tier, None)
        if before is not None:
            log.info("engine_ready", tier=tier)
        return
    _ENGINE_WAIT[tier] = problem
    if before != problem:
        log.warning("engine_wait", tier=tier, reason=problem, model=model)


async def local_engine_ready(settings: Settings, tier: str) -> bool:
    """The engine check every drain that sends to Ollama makes before its first lease
    (D-WORKER-6 amended, siblings): story builds, likely questions, title suggestions, company
    summaries, follow-up drafts and letter drafts. False while `tier` runs on Ollama and Ollama
    is not answering or lacks the tier's model: the drain leases nothing, so no task spends a
    try, and the next cycle asks again. True for a tier that does not run on Ollama (an online
    model, the in-process engine, another local server). Same probe and record as the scoring
    preflight."""
    model = _ollama_model(settings, tier)
    if model is None:
        _note_engine(tier, None, None)  # moved off Ollama: an old wait no longer applies
        return True
    probe = await _engine_probe_for(settings)
    problem = engine_problem(settings, tier, probe)
    _note_engine(tier, problem, model)
    return problem is None


def engine_lost_for(settings: Settings, tier: str) -> bool:
    """Checked before every lease by each drain on Ollama (round 6): once a call in this cycle
    could not reach Ollama, a drain whose tier runs on it leases nothing more this cycle. A tier
    that does not run on Ollama (the online lane) is never stopped by it."""
    from kestrel.llm import engine_cycle

    return engine_cycle.lost() and _ollama_model(settings, tier) is not None


async def ollama_answering(settings: Settings) -> bool:
    """Whether Ollama answers at all, by the cycle's one answer (or a probe now outside a cycle).
    For a drain whose model is chosen per task (the model check), where only "nothing answers"
    is certain before the task runs. True when the suite stood the probe down."""
    probe = await _engine_probe_for(settings)
    return probe is None or probe[0] is not None


def held_types(settings: Settings, types: tuple[str, ...] = ("t1", "t2")) -> tuple[str, ...]:
    """The task types among `types` whose tier routes to a backend on hold as out of credit."""
    return tuple(
        t for t in types if t in _OVERRIDE_TIER and tier_credit_hold(settings, _OVERRIDE_TIER[t])
    )


@dataclass
class TierStats:
    completed: int = 0
    errors: int = 0
    # Leased then deferred this drain (#154 measurement): a held task going back unstarted.
    # Zero is the healthy state after the queue-level hold fix; a steady non-zero count means
    # held tasks are still being leased.
    held: int = 0
    elapsed_s: float = 0.0
    # The roles this tier finished, so a run's "N roles rated" counts each role once even when
    # it went through both tiers (a role rated by t1 and t2 is one role, not two).
    roles: set[int] = field(default_factory=set)

    @property
    def per_minute(self) -> float:
        return (self.completed / self.elapsed_s * 60.0) if self.elapsed_s > 0 else 0.0


@dataclass
class RunStats:
    tier0: dict = field(default_factory=dict)
    embed: TierStats = field(default_factory=TierStats)
    tier1: TierStats = field(default_factory=TierStats)
    tier2: TierStats = field(default_factory=TierStats)


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: LLMClient,
        settings: Settings,
        *,
        stop: asyncio.Event | None = None,
        progress: ProgressCb | None = None,
        paused: Callable[[], bool] | None = None,
    ) -> None:
        self._sm = sessionmaker
        # The app's pause flag (review finding 2): every drain checks it between tasks, so a
        # pause finishes the leased task, releases nothing mid-task and claims no more.
        self._paused = paused or (lambda: False)
        self._client = client
        self._settings = settings
        self._online_approval: dict | None = None
        self._mid_run_switch_note: str | None = None
        self._queue = TaskQueue(sessionmaker)
        self._stop = stop or asyncio.Event()
        self._progress = progress or (lambda _m: None)
        self._profiles = {t: load_profile(t) for t in ("employment", "consulting")}
        # The Ollama probe this drain judges against (engine_problem); None: not probed.
        self._engine_probe: tuple[str | None, list[str]] | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def _halted(self) -> bool:
        """Stop requested or worker paused: claim nothing more (finding 2)."""
        return self._stop.is_set() or self._paused()

    def apply_settings(self, settings: Settings, *, online_approval: dict | None = None) -> None:
        """Swap in freshly resolved settings for the worker AND its client. The client resolves
        each tier's backend and model from its own `settings`, so updating only the worker's copy
        (what the serve loop used to do) left the client on the settings it was built with and
        a Settings-page change to a tier never reached the model call."""
        self._settings = settings
        self._online_approval = online_approval
        if hasattr(self._client, "settings"):
            self._client.settings = settings
        self.refresh_profiles()

    def refresh_profiles(self) -> None:
        """Re-read profile.yml when it has changed on disk (the Profile page writes it, D-UI-21),
        so a saved threshold reaches the next cycle without a restart. Called from
        apply_settings, once per cycle: a drain already running finishes on the old profile."""
        from kestrel.services.profile_edit import profile_path

        try:
            stamp = profile_path().stat().st_mtime_ns
        except OSError:
            stamp = 0
        if stamp != getattr(self, "_profile_stamp", None):
            self._profile_stamp = stamp
            self._profiles = {t: load_profile(t) for t in ("employment", "consulting")}

    async def resolve_settings(self, mode: str | None) -> Settings:
        """Settings for one run: the setting-table layer plus the requested mode ("local" or
        "hosted"), resolved once so the whole run scores on one tier set. None keeps the settings
        the worker already holds, whose own `llm.mode` is then what the run records."""
        if mode is None:
            return self._settings
        from kestrel.services.setting_overrides import load_settings_with_db

        async with self._sm() as s:
            return await load_settings_with_db(s, mode=mode)

    async def reclaim(self) -> int:
        """Return any crashed (expired) leases to the queue before draining, quarantine the dead
        scoring tasks (pipeline.deadletter), then re-enqueue orphans once (requeue_orphans).
        Called once per worker startup; `sweep` repeats the scoring part every cycle."""
        n = await self._queue.reclaim_expired()
        await self.quarantine_dead()
        await self.requeue_orphans()
        return n

    async def sweep(self) -> int:
        """Each cycle, before anything is leased (D-WORKER-7): a Tier 1 or Tier 2 task whose
        lease expired was claimed by a worker that is gone (a killed process, a crashed drain),
        so it goes back to the queue, or to dead-letter when out of tries; dead scoring tasks are
        quarantined; orphans get their one re-queue. Only expired leases are touched: a live
        drain renews its lease (_renew_lease), so nothing running is taken from under it, and
        every step is idempotent. Never fatal to the cycle."""
        from kestrel.pipeline.deadletter import SCORING_TYPES

        try:
            n = await self._queue.reclaim_expired(types=SCORING_TYPES)
        except SQLAlchemyError as exc:
            log.warning("cycle_reclaim_failed", error=str(exc))
            n = 0
        if n:
            log.info("cycle_reclaimed", count=n)
        await self.quarantine_dead()
        await self.requeue_orphans()
        return n

    async def quarantine_dead(self, task_ids: list[int] | None = None) -> list[int]:
        """A dead scoring task puts its role in Needs another try (pipeline.deadletter,
        D-WORKER-5). Never fatal: a failure is logged and the next cycle's sweep retries it."""
        from kestrel.pipeline.deadletter import quarantine_dead, tier_model_for

        try:
            return await quarantine_dead(
                self._sm, task_ids=task_ids, model_for=tier_model_for(self._settings)
            )
        except SQLAlchemyError as exc:
            log.warning("dead_scoring_quarantine_failed", error=str(exc))
            return []

    async def requeue_orphans(self) -> dict[str, int]:
        """A role waiting on a scoring step with nothing queued gets its task back, once
        (pipeline.orphans, D-UI-56). Never fatal to startup: a failure is logged and the role
        keeps reading "waiting" until the next start."""
        from kestrel.pipeline.orphans import requeue_orphans

        try:
            return await requeue_orphans(self._sm)
        except SQLAlchemyError as exc:
            log.warning("orphan_requeue_failed", error=str(exc))
            return {"requeued": 0, "stuck": 0}

    async def _has_claimable(self, task_type: str) -> bool:
        """Whether lease() could return anything for the type (queue.claimable)."""
        async with self._sm() as s:
            n = (
                await s.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.type == task_type, claimable(utcnow_iso()))
                )
            ).scalar_one()
        return n > 0

    async def _pending_count(self, task_type: str) -> int:
        async with self._sm() as s:
            return (
                await s.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.type == task_type, Task.status == "pending", ~in_lane())
                )
            ).scalar_one()

    async def release_lane(self) -> int:
        """At the end of a cycle, once every normal tier has drained: free the day's share of
        the background lane (pipeline.lane). Never starts a model; the freed tasks then go
        through the usual tier-by-tier drain."""
        if self._halted():
            return 0
        try:
            return await lane.release_due(
                self._sm, cap=self._settings.pipeline.lane_daily_cap,
                hosted=lane.scores_on_hosted(self._settings),
            )
        except SQLAlchemyError as exc:
            log.warning("lane_release_failed", error=str(exc))
            return 0

    async def drain_embed(self, stats: TierStats) -> None:
        """The embed tier: first enqueue every opportunity whose vector is missing or came from
        another model (once per embed model, D-EMBED-4), then drain. A failed sweep is logged and
        the drain still runs; new postings were enqueued at ingestion regardless."""
        try:
            await enqueue_stale_embeddings(self._sm, self._settings)
        except SQLAlchemyError as exc:
            log.warning("embed_sweep_failed", error=str(exc))
        await self.drain_tier("embed", self._settings.concurrency.embed, handle_embed, stats)

    async def _unload_previous_tier(self, task_type: str) -> None:
        """When the drain moves to another tier, unload the previous tier's local model first
        (#112). Ollama can crash loading a second model while the first is resident; the drain
        rule (never alternate tiers) makes this safe: nothing is about to use the old one. Memory
        only, nothing is deleted. A tier on a hosted backend, or the same model, unloads nothing."""
        tier = _OVERRIDE_TIER.get(task_type)
        if tier is None or await self._pending_count(task_type) == 0:
            return  # only a tier with queued work counts as the next tier
        cfg = self._settings.llm.tiers[tier]
        backend = self._settings.llm.backends.get(cfg.backend)
        model = cfg.model if backend is not None and not backend.external else None
        previous = getattr(self, "_last_local_model", None)
        previous_type = getattr(self, "_last_drained_type", None)
        self._last_drained_type = task_type
        self._last_local_model = model or previous
        if model is None or previous is None or previous_type == task_type or previous == model:
            return
        import httpx

        from kestrel.llm.client import unload_model

        async with httpx.AsyncClient() as http:
            await unload_model(http, self._settings.llm.ollama_native_url, previous)
        log.info("worker_tier_switch_unload", previous=previous, next=model)

    async def drain_tier(
        self,
        task_type: str,
        concurrency: int,
        handler: Callable[..., Awaitable[HandlerOutcome]],
        stats: TierStats,
    ) -> None:
        """Run up to `concurrency` leases in parallel until the tier's queue is empty or a stop is
        requested. Completions feed a trailing-window ETA.

        Tier 2 drains one model at a time (CLAUDE.md: model swap thrash): its claimable tasks are
        grouped by the model they will run on (the configured Tier 2 model, or a task's
        `model_override`), the configured model's group first, then the largest, and each group
        is drained to empty before the next starts. A mixed queue never alternates models."""
        # Preflight (D-WORKER-6): with the engine down or the model missing, the lease filter
        # below leaves out every task that would reach it, so none spends a try.
        await self._engine_wait(task_type)
        if task_type == "embed" and "embed" in _ENGINE_WAIT:
            return  # every embed task would reach the missing engine: none spends a try
        await self._unload_previous_tier(task_type)
        total_at_start = await self._pending_count(task_type)
        started = time.perf_counter()
        window: deque[float] = deque(maxlen=20)
        worker_id = f"pipeline-{task_type}"
        done_lock = asyncio.Lock()
        group: list[Any] = [None]  # the lease filter of the model group being drained
        # Tasks held this drain for an unapproved destination go back to pending; excluding them
        # keeps the drain from leasing the same held task again in a loop.
        held_ids: set[int] = set()

        async def one_worker(idx: int) -> None:
            wid = f"{worker_id}-{idx}"
            while not self._halted():
                # Work that would run on a backend held as out of credit is not leased at all
                # (D-LLM-8 amended): the hold is judged per task from the model it will resolve
                # to, so a held override cannot loop lease/defer and a held configured backend
                # cannot starve an override that resolves to a local model.
                from kestrel.llm import engine_cycle

                if engine_cycle.lost():
                    # A call this cycle could not reach Ollama (round 7): from now on the lease
                    # filter judges each task by the model it will run on, its Score again
                    # model included, so nothing more reaches Ollama and an online one still runs.
                    if task_type not in _OVERRIDE_TIER:
                        if engine_lost_for(self._settings, _ENGINE_TIER.get(task_type, "")):
                            return  # embed on Ollama takes no per-task model: no more
                    else:
                        self._engine_probe = (None, [])
                excl = await self._hold_exclusion(task_type)
                where = group[0] if excl is None else (
                    excl if group[0] is None else and_(group[0], excl)
                )
                if held_ids:
                    skip = Task.id.not_in(held_ids)
                    where = skip if where is None else and_(where, skip)
                task = await self._queue.lease(
                    wid, types=[task_type], lease_seconds=_TASK_LEASE_S, where=where
                )
                if task is None:
                    return
                async with done_lock:
                    leased[0] += 1
                # Held for the life of the handler, so a Tier 2 call that outlives the lease is
                # renewed rather than reclaimed underneath itself (see _TASK_LEASE_S).
                beat = asyncio.create_task(_renew_lease(self._queue, task.id, wid))
                try:
                    await _score_one(task, wid, task_type, task.payload or {})
                finally:
                    beat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await beat

        async def _score_one(task: Any, wid: str, task_type: str, payload: dict) -> None:
            """Run one leased task to a terminal queue state. Never raises: a handler crash and a
            failed send both go back to the queue (the page shows the reason and Try again) and the
            worker keeps draining."""
            opp_id = payload.get("opportunity_id")
            local_only = bool(payload.get(lane.LOCAL_ONLY) or "lane" in payload)
            if task_type in ("t1", "t2"):
                async with self._sm() as session:
                    opp = await session.get(Opportunity, opp_id) if opp_id else None
                    local_only = local_only or bool(
                        opp and lane.LANE_REASON in (opp.stage_reason or "")
                    )
                    if opp_id:
                        local_only = local_only or await lane.prior_lane(session, opp_id)
                    if local_only and opp and await lane.reject_mismatch(session, opp):
                        stored = await session.get(Task, task.id)
                        if stored and stored.status == "leased" and stored.worker_id == wid:
                            stored.status = "done"
                            stored.worker_id = None
                            stored.lease_until = None
                            stored.updated_at = utcnow_iso()
                        await session.commit()
                        return
                    if local_only:
                        payload = {**payload, lane.LOCAL_ONLY: True}
                        stored = await session.get(Task, task.id)
                        if stored and stored.status == "leased" and stored.worker_id == wid:
                            stored.payload = payload
                            await session.commit()
                if local_only:
                    if lane.scores_on_hosted(self._settings):
                        held_ids.add(task.id)
                        await self._queue.defer(
                            task.id,
                            "held: background roles use local models only",
                            worker_id=wid,
                            reason=REASON_LOCAL_ONLY,
                        )
                        stats.held += 1
                        return
            if self._online_approval is not None and "online_approval" not in payload:
                payload = {**payload, "online_approval": self._online_approval}
                async with self._sm() as session:
                    stored = await session.get(Task, task.id)
                    if stored is not None and stored.status == "leased" and stored.worker_id == wid:
                        stored.payload = payload
                        await session.commit()
            settings, client = await self._task_settings(task_type, payload)
            from kestrel.services.online_consent import held_for_approval

            tier = "tier1" if task_type == "t1" else "tier2"
            client_settings = getattr(client, "settings", settings)
            if local_only and (
                lane.scores_on_hosted(settings) or lane.scores_on_hosted(client_settings)
            ):
                held_ids.add(task.id)
                await self._queue.defer(
                    task.id,
                    "held: background roles use local models only",
                    worker_id=wid,
                    reason=REASON_LOCAL_ONLY,
                )
                stats.held += 1
                return
            if held_for_approval(settings, (tier,), payload) or held_for_approval(
                client_settings, (tier,), payload
            ):
                held_ids.add(task.id)
                await self._queue.defer(
                    task.id,
                    "held: online destination changed or was not confirmed",
                    worker_id=wid,
                    reason=REASON_APPROVAL,
                )
                stats.held += 1
                return
            kwargs: dict = {}
            from kestrel.services.online_consent import CORRECTIONS_KEY

            if CORRECTIONS_KEY in payload:
                # The correction lines the operator confirmed (D-SCORE-3): Tier 2 holds the task
                # when the lines it would send now differ; Tier 1 hands them on unread.
                bound = str(payload[CORRECTIONS_KEY] or "")
                if task_type == "t2":
                    kwargs["corrections_digest"] = bound
                else:
                    kwargs["t2_carry"] = {CORRECTIONS_KEY: bound}
            if task_type == "t2":
                kwargs["changed"] = bool(payload.get("changed"))
                if payload.get("requested") or payload.get("rescore"):
                    kwargs["requested"] = True  # the operator asked: an empty end is reported
                if payload.get("rescore"):
                    kwargs["rescore"] = True
                    kwargs["requested_at"] = payload.get("requested_at")
                    kwargs["task_id"] = task.id
                    if payload.get("score_stub"):
                        kwargs["score_stub"] = True  # the operator's explicit Score again
            if task_type == "t1":
                if local_only:
                    kwargs["lane_local_only"] = True
                if "online_approval" in payload:
                    kwargs["online_approval"] = payload["online_approval"]
                if payload.get("rescore"):
                    # A score-again of a role Tier 1 set aside: the stage stays as it was
                    # until this task runs (queue.rescore), so the handler is told.
                    kwargs["rescore"] = True
                    kwargs["requested_at"] = payload.get("requested_at")
                    if payload.get("tier0_override"):
                        # Score this role anyway (D-UI-74): runs from t0_reject.
                        kwargs["tier0_override"] = True
                    if payload.get("model_override"):
                        # Score again on another model (D-UI-50): Tier 1 ran on it (or fell back,
                        # logged), and the Tier 2 task carries it on; that task's own settings
                        # re-check it when it runs, so a pick that can no longer run falls back
                        # there too (hard rule 4).
                        kwargs["model_override"] = payload.get("model_override")
                    if payload.get("score_stub"):
                        # Score again on a stub: Tier 1 passes the flag to Tier 2 (#128).
                        kwargs["score_stub"] = True
                if payload.get(T2_ONLY_KEY):
                    # A run's one-run model (D-UI-72): Tier 1 stays on the configured model;
                    # only the Tier 2 task Tier 1 enqueues carries the pick.
                    kwargs["t2_model_override"] = payload.get(T2_ONLY_KEY)
            try:
                outcome = await handler(
                    self._sm,
                    client,
                    settings,
                    opportunity_id=opp_id,
                    profiles=self._profiles,
                    **kwargs,
                )
            except Exception as exc:  # unexpected handler crash: retry the task, keep draining
                log.error("handler_crash", task=task.id, type=task_type, error=str(exc))
                status = await self._queue.fail(
                    task.id, failure_record(exc), worker_id=wid, retry=True,
                    engine_wait=engine_unreachable(exc),
                )
                if status == "dead":  # out of tries: Needs another try, never dropped
                    await self.quarantine_dead([task.id])
                async with done_lock:
                    stats.errors += 1
                return

            if outcome.action == "held":
                # Something the operator confirmed changed before the send (D-SCORE-3): nothing
                # was sent, and the task waits for a new confirmation like any approval hold.
                held_ids.add(task.id)
                await self._queue.defer(
                    task.id, outcome.error or "", worker_id=wid, reason=REASON_CORRECTIONS
                )
                stats.held += 1
                return

            if outcome.action == "error" and outcome.out_of_credit:
                # Not a failure of the role: the key has no credit. The task goes back unstarted
                # (resumable, hard rule 5); the hold keeps every other send from this backend.
                await self._queue.defer(
                    task.id, outcome.error or "", worker_id=wid, reason=REASON_CREDIT
                )
                stats.held += 1
                return

            if outcome.action == "error":
                status = await self._queue.fail(
                    task.id, outcome.error or "", worker_id=wid, retry=True,
                    engine_wait=outcome.engine_unreachable,
                )
                if status == "dead":  # out of tries: Needs another try, never dropped
                    await self.quarantine_dead([task.id])
                async with done_lock:
                    stats.errors += 1
                return

            # `complete()` is False when this worker no longer owns the task: its lease was
            # reclaimed and someone else took it. The work itself is done and the row is already
            # terminal by that worker's hand, so this is not an error -- but counting it here
            # would report one task as completed twice on the same Run row and skew the progress
            # figures the live run panel polls.
            if not await self._queue.complete(task.id, worker_id=wid):
                log.warning("pipeline_complete_not_owned", task=task.id, worker=wid)
                return
            async with done_lock:
                stats.completed += 1
                if opp_id is not None:
                    stats.roles.add(int(opp_id))
                window.append(time.perf_counter())
                self._emit_progress(task_type, stats.completed, total_at_start, window)

        leased = [0]
        if task_type != "t2":
            await self._gather_workers(one_worker, concurrency, task_type, stats)
        else:
            while not self._halted():
                groups = await self._model_groups(task_type, exclude_ids=held_ids)
                if not groups:
                    break
                group[0] = groups[0]
                leased[0] = 0
                await self._gather_workers(one_worker, concurrency, task_type, stats)
                if not leased[0]:
                    break  # counted but not claimable after all (a lost race): next cycle
        stats.elapsed_s = time.perf_counter() - started

    async def _gather_workers(
        self, one_worker: Any, concurrency: int, task_type: str, stats: Any
    ) -> int:
        """Run the sibling workers to completion, whatever one of them does.

        Plain `asyncio.gather` re-raises the first exception immediately but does NOT cancel the
        others, so one worker failing on `lease()`, `complete()` or an unretried OperationalError
        unwound `drain_tier` with three coroutines still running unsupervised: still leasing and
        scoring, mutating the `stats` and `window` already written to the closed Run row, with
        their own exceptions then swallowed as "Task exception was never retrieved". Every
        sibling is awaited here.

        A crashed worker is COUNTED and then re-raised (review). Collecting the exceptions
        without surfacing them was its own defect: `drain_tier` returned normally, the outer
        `except` that writes a `failed` Run row was never reached, and the run was recorded as
        `done` for a drain that silently lost a worker. Cancellation is not a crash here: the outer
        drain owns shutdown, so it is counted and re-raised the same way (the caller's `except`
        treats CancelledError as cancellation, not failure).
        """
        results = await asyncio.gather(
            *(one_worker(i) for i in range(max(1, concurrency))), return_exceptions=True
        )
        crashed: list[BaseException] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                log.error(
                    "pipeline_worker_crashed",
                    type=task_type,
                    worker=i,
                    error=f"{type(r).__name__}: {r}",
                )
                crashed.append(r)
        if crashed:
            # Counted on the drained tier's own stats, where the run summary and the live run panel
            # read them. No `await` between the read and the write, so this is safe on the single
            # loop the worker and every DB session share.
            #
            # The crash is NOT re-raised (review): re-raising skipped Tier 2 and embed
            # entirely, so one worker dying mid-drain turned a partial run into no run at all. A
            # degraded tier is already a normal outcome -- a 429 from a job board produces `partial`
            # and the next tier still runs -- and this now behaves the same way. The count on
            # `stats.errors` is what keeps the run from being reported as a clean `done`.
            tier_stats = getattr(stats, _STATS_FIELD.get(task_type, ""), None)
            if tier_stats is not None:
                tier_stats.errors += len(crashed)
        return len(crashed)

    async def _engine_wait(self, task_type: str) -> None:
        """Preflight before a Tier 1 or Tier 2 drain (D-WORKER-6). When the tier runs on Ollama,
        probe it (probe_engine: the probe Setup and Models use, cached a few seconds). Engine
        down or the tier's model missing: record why (engine_wait, read by the Pipeline and the
        role page), log it once, and keep the probe so the lease filter (_group_held,
        _hold_exclusion) leaves out every task that would reach the engine: nothing is leased
        and no try is spent. A Score again on a model that runs elsewhere still drains. The
        embed tier is judged the same way when it runs on Ollama; it has no per-task model, so
        while it waits its drain leases nothing (drain_tier)."""
        tier = _ENGINE_TIER.get(task_type)
        if tier is None:
            return
        self._engine_probe = None
        problem = None
        if _ollama_model(self._settings, tier) is not None:
            probe = await _engine_probe_for(self._settings)
            problem = engine_problem(self._settings, tier, probe)
            # Kept only while something is wrong, so a healthy drain's lease filter stays the
            # plain one (no extra query per lease).
            self._engine_probe = probe if problem is not None else None
        _note_engine(tier, problem, _ollama_model(self._settings, tier))

    async def engine_skip_types(self) -> tuple[str, ...]:
        """The scoring task types a cycle must not open a Run row for (D-WORKER-6 amended twice),
        decided fresh before the cycle's work check: each tier the last drain left waiting is
        probed again (the probe is cached a few seconds), so the cycle in which Ollama comes back,
        or a pulled model is chosen, opens its Run and holds its settings before anything is
        leased. A type still waiting is skipped only when the lease filter would let none of its
        claimable tasks through, so a Score again on a model that runs elsewhere opens the Run
        too. Leaves no probe behind: the drain probes for itself."""
        skip: list[str] = []
        for task_type in engine_waiting_types():
            # Probe again; once the engine is back the lease filter is the plain one, so any
            # claimable task means the cycle has work for this type.
            await self._engine_wait(task_type)
            if task_type == "embed":
                if "embed" in _ENGINE_WAIT:  # no per-task model: nothing can run elsewhere
                    skip.append(task_type)
            elif not await self._claimable_where(
                task_type, await self._hold_exclusion(task_type)
            ):
                skip.append(task_type)
        self._engine_probe = None
        return tuple(skip)

    async def _claimable_where(self, task_type: str, where: Any) -> bool:
        async with self._sm() as s:
            stmt = select(Task.id).where(Task.type == task_type, claimable(utcnow_iso()))
            if where is not None:
                stmt = stmt.where(where)
            return (await s.execute(stmt.limit(1))).first() is not None

    def _resolved_blocked(self, resolved: Settings, tier: str) -> bool:
        """Whether `resolved` sends `tier` to a backend that cannot take it now: held as out of
        credit, or the local engine down or missing the model (D-WORKER-6)."""
        if credit.any_held() and tier_credit_hold(resolved, tier) is not None:
            return True
        return engine_problem(resolved, tier, self._engine_probe) is not None

    async def _model_groups(
        self, task_type: str, *, exclude_ids: set[int] | None = None
    ) -> list[Any]:
        """Lease filters for the tier's claimable tasks, one per model they will run on, in
        drain order: the configured model first, then by task count (largest first), then by
        name, so the order is stable. An override naming the configured model joins its group."""
        tier = _OVERRIDE_TIER[task_type]
        tcfg = self._settings.llm.tiers.get(tier)
        cfg = (str(tcfg.backend), str(tcfg.model)) if tcfg is not None else ("", "")
        backend = func.coalesce(func.json_extract(Task.payload, "$.model_override.backend"), "")
        model = func.json_extract(Task.payload, "$.model_override.model")
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(backend, model, func.count())
                    .where(
                        Task.type == task_type, claimable(utcnow_iso()),
                        Task.id.not_in(exclude_ids or ()),
                    )
                    .group_by(backend, model)
                )
            ).all()
        counts: dict[tuple[str, str] | None, int] = {}
        for b, m, n in rows:
            key = None if (not m or (str(b), str(m)) == cfg) else (str(b), str(m))
            counts[key] = counts.get(key, 0) + int(n)
        order = sorted(counts, key=lambda k: (k is not None, -counts[k], k or ("", "")))
        # A group whose model resolves to a backend held as out of credit is not drained: nothing
        # of it is leased until the hold clears (D-LLM-8 amended).
        order = [k for k in order if not self._group_held(tier, k)]
        out: list[Any] = []
        for key in order:
            if key is None:
                out.append(or_(model.is_(None), model == "",
                               and_(backend == cfg[0], model == cfg[1])))
            else:
                out.append(and_(backend == key[0], model == key[1]))
        return out

    def _group_held(self, tier: str, key: tuple[str, str] | None) -> bool:
        """Whether the model group `key` (None: the configured model) would be sent to a backend
        on hold as out of credit. An override that cannot run falls back to the configured model,
        so it is judged there, exactly as `_task_settings` resolves it."""
        if not credit.any_held() and self._engine_probe is None:
            return False
        if key is not None:
            try:
                resolved = settings_with_override(
                    self._settings, tier, {"backend": key[0], "model": key[1]}
                )
            except ModelOverrideError:
                resolved = self._settings
            return self._resolved_blocked(resolved, tier)
        return self._resolved_blocked(self._settings, tier)

    async def _hold_exclusion(self, task_type: str) -> Any:
        """A lease filter that leaves out the tasks which would run on a held backend, or None
        when no backend is held (the common case: no query is made)."""
        tier = _OVERRIDE_TIER.get(task_type)
        if tier is None or not (credit.any_held() or self._engine_probe is not None):
            return None
        tcfg = self._settings.llm.tiers.get(tier)
        cfg = (str(tcfg.backend), str(tcfg.model)) if tcfg is not None else ("", "")
        backend = func.coalesce(func.json_extract(Task.payload, "$.model_override.backend"), "")
        model = func.coalesce(func.json_extract(Task.payload, "$.model_override.model"), "")
        # Tier 1 honors an override only on a Score again (see _task_settings).
        applies = model != ""
        if task_type == "t1":
            applies = and_(applies, func.coalesce(
                func.json_extract(Task.payload, "$.rescore"), 0) == 1)
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(backend, model)
                    .where(Task.type == task_type, claimable(utcnow_iso()), applies)
                    .distinct()
                )
            ).all()
        cfg_held = self._group_held(tier, None)
        clauses: list[Any] = []
        on_cfg: list[Any] = [not_(applies), and_(backend == cfg[0], model == cfg[1])]
        for b, m in rows:
            key = (str(b), str(m))
            if key == cfg:
                continue
            try:
                settings_with_override(self._settings, tier, {"backend": key[0], "model": key[1]})
            except ModelOverrideError:
                on_cfg.append(and_(backend == key[0], model == key[1]))  # falls back to configured
                continue
            if self._group_held(tier, key):
                clauses.append(and_(applies, backend == key[0], model == key[1]))
        if cfg_held:
            clauses.append(or_(*on_cfg))
        return not_(or_(*clauses)) if clauses else None

    async def _task_settings(self, task_type: str, payload: dict) -> tuple[Settings, LLMClient]:
        """The settings and client for one task: the worker's own, or, for a score-again task
        that names another model (`model_override`, D-UI-50), a deep copy with that tier's model
        replaced for this task only. An override that can no longer run is logged and the task
        runs on the configured model instead: the role still gets scored (hard rule 4).

        `self._settings` is refreshed only between cycles, so a task that would reach an external
        backend first re-reads Hosted mode and the run card from the setting rows (_hosted_now,
        one small read) and asks the one gate for online sends (setup_state.online_send_held,
        D-PRIV-798). Turned off, its disclosure no longer acknowledged, or On this computer
        chosen since the cycle began: an override
        falls back to the configured model, and a configured hosted tier gets the live Hosted
        mode so the client refuses the send exactly as when it is off (Privacy: nothing leaves
        the machine after the operator turned that off). Local tasks pay no extra read."""
        override = payload.get("model_override")
        tier = _OVERRIDE_TIER.get(task_type)
        if tier is None:
            return self._settings, self._client
        if task_type == "t1" and not payload.get("rescore"):
            override = None  # Tier 1 runs a one-off model only for Score again (D-UI-72 amended)
        settings = self._settings
        if override:
            try:
                settings = settings_with_override(self._settings, tier, override)
            except ModelOverrideError as exc:
                log.warning("model_override_unavailable", type=task_type, reason=str(exc))
        if _tier_external(settings, tier):
            from kestrel.services.setup_state import online_send_held

            now = await self._settings_now(settings)
            live = now.llm.hosted_mode
            if online_send_held(now) is not None:
                if settings is not self._settings:
                    log.warning(
                        "model_override_unavailable", type=task_type,
                        reason=(
                            "Hosted mode was turned off since this run began" if not live.active
                            else "On this computer was chosen since this run began"
                        ),
                    )
                    settings = self._settings
                if _tier_external(settings, tier):
                    log.warning("hosted_mode_off_mid_run", type=task_type, tier=tier)
                    if self._mid_run_switch_note is None:
                        self._mid_run_switch_note = SWITCHED_TO_COMPUTER_NOTE
                    settings = settings.model_copy(deep=True)
                    settings.llm.hosted_mode = live
        if settings is self._settings:
            return self._settings, self._client
        return settings, client_for(self._client, settings)

    async def _settings_now(self, settings: Settings) -> Settings:
        """`settings` as a shallow copy whose Hosted mode and run card are what the setting rows
        hold NOW (_hosted_now), for asking the one gate for online sends mid-cycle."""
        live = await self._hosted_now()
        return settings.model_copy(
            update={"llm": settings.llm.model_copy(update={"hosted_mode": live})}
        )

    async def _hosted_now(self) -> HostedMode:
        """Hosted mode as the setting rows say it is NOW: the cycle's copy with every
        `llm.hosted_mode.<field>` row laid over it (the Settings > Hosted switch and the
        disclosure acknowledgement are written there), carrying the run card's rows as they are
        now (D-PRIV-798: On this computer chosen mid-cycle holds the send like the switch does).
        A row that no longer validates reads as
        off: when unsure, nothing is sent. Limitation: a row DELETED mid-cycle keeps the cycle's
        value until the next cycle (the switch writes False, it does not delete)."""
        prefix = "llm.hosted_mode."
        try:
            async with self._sm() as s:
                rows = (
                    await s.execute(
                        select(Setting.key, Setting.value).where(
                            or_(Setting.key.like(prefix + "%"), Setting.key.in_(RUN_CARD_KEYS))
                        )
                    )
                ).all()
        except SQLAlchemyError as exc:
            log.warning("hosted_mode_read_failed", error=str(exc))
            return HostedMode(enabled=False)
        from kestrel.services.setup_state import hosted_mode_from_rows

        return hosted_mode_from_rows(
            self._settings.llm.hosted_mode, {k: v for k, v in rows}
        )

    def _emit_progress(self, task_type: str, done: int, total: int, window: deque[float]) -> None:
        eta = ""
        if len(window) >= 2:
            per_item = (window[-1] - window[0]) / (len(window) - 1)
            remaining = max(0, total - done)
            eta = f", eta ~{int(per_item * remaining)}s" if remaining else ""
        rpm = ""
        if len(window) >= 2:
            per_item = (window[-1] - window[0]) / (len(window) - 1)
            if per_item > 0:
                rpm = f", {60.0 / per_item:.1f}/min"
        self._progress(f"[{task_type}] {done}/{total}{rpm}{eta}")

    async def drain_scrapes(self, stats: TierStats | None = None) -> TierStats:
        """Execute every pending `scrape` task, one source at a time (each source rate-limits its
        own host; parallel sources would only multiply that). The scheduler, the tray's "scan
        now", and MCP `trigger_scan` all ENQUEUE these; this is their only consumer. A source
        that fails records its own scrape_run and the task completes: retrying a scrape through
        the queue would re-hit a public site that already answered with an error."""
        from kestrel.ingest.http import HttpClient
        from kestrel.ingest.runner import run_source
        from kestrel.ingest.signal_runner import run_signal_source
        from kestrel.ingest.signal_sources import SIGNAL_SOURCE_KINDS

        stats = stats or TierStats()
        started = time.perf_counter()
        wid = "pipeline-scrape"
        if self._halted() or not await self._has_claimable("scrape"):
            # Nothing queued: skip building the HTTP client. Its TLS context (the CA bundle
            # parse) is synchronous work on the shared event loop, and this runs every 5 s.
            stats.elapsed_s = time.perf_counter() - started
            return stats
        ing = self._settings.ingest
        async with HttpClient(
            user_agent=ing.user_agent,
            default_rate_per_sec=ing.default_rate_per_sec,
            timeout=ing.request_timeout_s,
            respect_robots=ing.respect_robots,
        ) as http:
            held_scrapes: set[int] = set()  # held this cycle; never leased again in a loop
            while not self._halted():
                task = await self._queue.lease(
                    wid, types=["scrape"], lease_seconds=1800,
                    where=Task.id.not_in(held_scrapes) if held_scrapes else None,
                )
                if task is None:
                    break
                source_id = (task.payload or {}).get("source_id")
                attempt_started = utcnow_iso()
                previous_run_id = 0
                try:
                    payload = task.payload or {}
                    if self._online_approval is not None and "online_approval" not in payload:
                        payload = {**payload, "online_approval": self._online_approval}
                        async with self._sm() as session:
                            stored = await session.get(Task, task.id)
                            if stored is not None and stored.status == "leased":
                                stored.payload = payload
                                await session.commit()
                    async with self._sm() as s:
                        src = await s.get(Source, source_id) if source_id is not None else None
                        kind = src.kind if src else None
                        previous_run_id = await s.scalar(
                            select(func.max(ScrapeRun.id)).where(ScrapeRun.source_id == source_id)
                        ) or 0
                    if kind is None:
                        raise ValueError(f"source {source_id!r} not found")
                    if kind in SIGNAL_SOURCE_KINDS:
                        from kestrel.services.online_consent import held_for_approval

                        # The cycle's settings are from its start: a classifier on an online
                        # model is judged, and run, with Hosted mode and the run card as they
                        # are now (D-PRIV-798), like a Tier 1 or Tier 2 task.
                        run_settings, run_client = self._settings, self._client
                        client_settings = getattr(self._client, "settings", self._settings)
                        if _tier_external(self._settings, "trigger") or _tier_external(
                            client_settings, "trigger"
                        ):
                            run_settings = await self._settings_now(self._settings)
                            client_settings = await self._settings_now(client_settings)
                            run_client = client_for(self._client, client_settings)
                        if (
                            held_for_approval(run_settings, ("trigger",), payload)
                            or held_for_approval(client_settings, ("trigger",), payload)
                        ):
                            held_scrapes.add(task.id)
                            await self._queue.defer(
                                task.id,
                                "held: online destination changed or was not confirmed",
                                worker_id=wid,
                                reason=REASON_APPROVAL,
                            )
                            stats.held += 1
                            continue
                        outcome = await run_signal_source(
                            self._sm, cast(int, source_id), http=http, client=run_client,
                            settings=run_settings,
                        )
                    else:
                        outcome = await run_source(
                            self._sm, source_id, http=http,
                            model_override=payload.get("model_override"),
                            model_label=payload.get("model_label"),
                            online_approval=payload.get("online_approval"),
                        )
                except Exception as exc:  # the source's own run recorded what it could
                    log.error("scrape_task_crash", task=task.id, source=source_id, error=str(exc))
                    await self._queue.fail(task.id, failure_record(exc), worker_id=wid, retry=False)
                    stats.errors += 1
                    try:
                        async with self._sm() as s:
                            src = await s.get(Source, source_id) if source_id is not None else None
                            if src is not None:
                                finished = utcnow_iso()
                                run = await s.scalar(
                                    select(ScrapeRun).where(
                                        ScrapeRun.source_id == source_id,
                                        ScrapeRun.id > previous_run_id,
                                        ScrapeRun.started_at >= attempt_started,
                                    ).order_by(ScrapeRun.id.desc()).limit(1)
                                )
                                if run is None:
                                    run = ScrapeRun(
                                        source_id=source_id, started_at=attempt_started,
                                        status="running", counts={},
                                    )
                                    s.add(run)
                                if run.status == "running":
                                    run.status = "failed"
                                    run.finished_at = finished
                                    run.error = f"{type(exc).__name__}: {exc}"
                                    run.counts = {**(run.counts or {}), "errors": 1}
                                src.last_run_at = finished
                                await s.commit()
                    except SQLAlchemyError as history_error:
                        log.error("scrape_crash_history_failed", task=task.id,
                                  source=source_id, error=str(history_error))
                    continue
                await self._queue.complete(task.id, worker_id=wid)
                stats.completed += 1
                if outcome.status != "ok":
                    stats.errors += 1
                self._progress(f"[scrape] source {source_id}: {outcome.status} {outcome.counts}")
        stats.elapsed_s = time.perf_counter() - started
        return stats

    async def run(
        self, *, track: str | None = None, limit: int | None = None, mode: str | None = None
    ) -> RunStats:
        """Full pass: reclaim crashed leases, Tier 0 sweep, then drain Tier 1, then Tier 2. A crash
        anywhere marks the run row `failed` with the error and re-raises; a run is never left
        `running` after the process that owned it has moved on. `mode` ("local" or "hosted")
        resolves the tier set for this whole run and is recorded on the Run row; an explicit
        "hosted" that cannot run raises config.HostedModeError before any row is written."""
        self.apply_settings(await self.resolve_settings(mode))
        self._mid_run_switch_note = None
        stats = RunStats()
        run_id = await runlog.open_run(
            self._sm, name="pipeline", track=track, mode=self._settings.llm.mode
        )
        await begin_engine_cycle(self._settings)  # one engine answer for this run (round 4)
        try:
            return await self._run_tiers(stats, run_id, track=track, limit=limit)
        except Exception as exc:
            await runlog.finish_run(
                self._sm, run_id, status="failed",
                counts={"error": f"{type(exc).__name__}: {exc}"[:500]},
                notes=self._mid_run_switch_note,
            )
            raise
        finally:
            end_engine_cycle()

    async def _notify_run_finished(self, stats: RunStats, run_id: int | None = None) -> None:
        """Raise the run's notifications (kestrel.services.run_notify): ONE outcome (the "Run
        finished" summary, or a warning under Something needs attention when roles need another
        try) and, when the run found new roles worth a look, ONE consolidated alert. Never one
        per role. A failure here is logged with context and swallowed: the run is done."""
        from kestrel.services.run_notify import notify_run_finished

        try:
            await notify_run_finished(
                self._sm, run_id=run_id, rated=len(stats.tier1.roles | stats.tier2.roles)
            )
        except Exception as exc:  # noqa: BLE001
            # The one sanctioned broad catch: the run is already marked finished, and a notifier,
            # a toast backend or a summary query failing for ANY reason must not turn that into a
            # failed run. It is logged with context, never silently dropped.
            log.warning("run_finished_notification_failed", run_id=run_id,
                        error=type(exc).__name__, detail=str(exc)[:200], exc_info=True)

    async def _run_tiers(
        self, stats: RunStats, run_id: int, *, track: str | None, limit: int | None
    ) -> RunStats:
        await self.reclaim()

        # Roles the rules set aside under an older profile get another look first (rules only);
        # those that now pass go back to `new` and the sweep below carries them on.
        await recheck.drain(self._sm, settings=self._settings, stop=self._stop)
        stats.tier0 = await run_tier0_batch(
            self._sm, settings=self._settings, track=track, limit=limit
        )
        self._progress(f"[t0] {stats.tier0}")

        # Embed FIRST. Tier 1's reranker gate and Tier 2's few-shot selection both look up the
        # opportunity's vector and silently degrade without one, so draining embed after them would
        # leave the first pass through every batch running blind.
        if not self._stop.is_set():
            await self.drain_embed(stats.embed)
        if not self._stop.is_set():
            await self.drain_tier("t1", self._settings.concurrency.tier1, handle_t1, stats.tier1)
        if not self._stop.is_set():
            await self.drain_tier("t2", self._settings.concurrency.tier2, handle_t2, stats.tier2)
        # Idle: the day's share of roles from found company boards (the background lane), which
        # then drain tier by tier like anything else.
        if not self._stop.is_set() and await self.release_lane():
            if not self._stop.is_set():
                await self.drain_tier(
                    "t1", self._settings.concurrency.tier1, handle_t1, stats.tier1
                )
            if not self._stop.is_set():
                await self.drain_tier(
                    "t2", self._settings.concurrency.tier2, handle_t2, stats.tier2
                )

        await runlog.finish_run(
            self._sm, run_id,
            status="stopping" if self._stop.is_set() else "done",
            counts=runlog.stats_counts(stats),
            notes=self._mid_run_switch_note,
        )
        # Notify the operator that the run is done (#109, #110, #111, #125 to #127). A stopped
        # run says nothing. Building the notice never fails a finished run.
        if not self._stop.is_set():
            await self._notify_run_finished(stats, run_id)
            # The first run's one automatic story build (D-STORY-700), the CLI path of the same
            # rule the serve loop applies after its tiers. Never fails a finished run.
            try:
                from kestrel.interview.first_run import queue_first_run_stories

                await queue_first_run_stories(self._sm, produced_tier2=stats.tier2.completed > 0)
            except Exception as exc:  # noqa: BLE001 - the next run asks again
                log.warning("first_run_stories_error", error_type=type(exc).__name__)
        return stats
