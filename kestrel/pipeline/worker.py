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
from typing import Any

import structlog
from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import HostedMode, Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, ScrapeRun, Setting, Source, Task
from kestrel.errors import failure_record
from kestrel.llm import credit
from kestrel.llm.client import LLMClient, tier_credit_hold
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
from kestrel.queue.queue import TaskQueue, claimable, in_lane
from kestrel.services.run_model import T2_ONLY_KEY

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


def _tier_external(settings: Settings, tier: str) -> bool:
    """Whether `tier` names an external backend (whatever Hosted mode says)."""
    tcfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(tcfg.backend) if tcfg else None
    return bool(backend and backend.external)


def held_types(settings: Settings, types: tuple[str, ...] = ("t1", "t2")) -> tuple[str, ...]:
    """The task types among `types` whose tier routes to a backend on hold as out of credit."""
    return tuple(
        t for t in types if t in _OVERRIDE_TIER and tier_credit_hold(settings, _OVERRIDE_TIER[t])
    )


@dataclass
class TierStats:
    completed: int = 0
    errors: int = 0
    elapsed_s: float = 0.0

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
        self._queue = TaskQueue(sessionmaker)
        self._stop = stop or asyncio.Event()
        self._progress = progress or (lambda _m: None)
        self._profiles = {t: load_profile(t) for t in ("employment", "consulting")}

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
        """Return any crashed (expired) leases to the queue before draining, then re-enqueue
        orphans once (requeue_orphans). Called once per worker startup."""
        n = await self._queue.reclaim_expired()
        await self.requeue_orphans()
        return n

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
                        prior_lane = await session.scalar(
                            select(exists().where(
                                Task.type.in_(("t1", "t2")),
                                func.json_extract(Task.payload, "$.opportunity_id") == opp_id,
                                or_(
                                    func.json_type(Task.payload, "$.lane").is_not(None),
                                    func.json_extract(
                                        Task.payload, f"$.{lane.LOCAL_ONLY}"
                                    ) == 1,
                                ),
                            ))
                        )
                        local_only = local_only or bool(prior_lane)
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
                            task.id, "held: background roles use local models only", worker_id=wid
                        )
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
                    task.id, "held: background roles use local models only", worker_id=wid
                )
                return
            if held_for_approval(settings, (tier,), payload) or held_for_approval(
                client_settings, (tier,), payload
            ):
                held_ids.add(task.id)
                await self._queue.defer(
                    task.id,
                    "held: online destination changed or was not confirmed",
                    worker_id=wid,
                )
                return
            kwargs: dict = {}
            if task_type == "t2":
                kwargs["changed"] = bool(payload.get("changed"))
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
                await self._queue.fail(task.id, failure_record(exc), worker_id=wid, retry=True)
                async with done_lock:
                    stats.errors += 1
                return

            if outcome.action == "error" and outcome.out_of_credit:
                # Not a failure of the role: the key has no credit. The task goes back unstarted
                # (resumable, hard rule 5); the hold keeps every other send from this backend.
                await self._queue.defer(task.id, outcome.error or "", worker_id=wid)
                return

            if outcome.action == "error":
                await self._queue.fail(
                    task.id, outcome.error or "", worker_id=wid, retry=True
                )
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
        if not credit.any_held():
            return False
        if key is not None:
            try:
                resolved = settings_with_override(
                    self._settings, tier, {"backend": key[0], "model": key[1]}
                )
            except ModelOverrideError:
                resolved = self._settings
            return tier_credit_hold(resolved, tier) is not None
        return tier_credit_hold(self._settings, tier) is not None

    async def _hold_exclusion(self, task_type: str) -> Any:
        """A lease filter that leaves out the tasks which would run on a held backend, or None
        when no backend is held (the common case: no query is made)."""
        tier = _OVERRIDE_TIER.get(task_type)
        if tier is None or not credit.any_held():
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
        backend first re-reads Hosted mode from the setting rows (_hosted_now, one small read).
        Turned off, or its disclosure no longer acknowledged, since the cycle began: an override
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
            live = await self._hosted_now()
            if not live.active:
                if settings is not self._settings:
                    log.warning(
                        "model_override_unavailable", type=task_type,
                        reason="Hosted mode was turned off since this run began",
                    )
                    settings = self._settings
                if _tier_external(settings, tier):
                    log.warning("hosted_mode_off_mid_run", type=task_type, tier=tier)
                    settings = settings.model_copy(deep=True)
                    settings.llm.hosted_mode = live
        if settings is self._settings:
            return self._settings, self._client
        return settings, client_for(self._client, settings)

    async def _hosted_now(self) -> HostedMode:
        """Hosted mode as the setting rows say it is NOW: the cycle's copy with every
        `llm.hosted_mode.<field>` row laid over it (the Settings > Hosted switch and the
        disclosure acknowledgement are written there). A row that no longer validates reads as
        off: when unsure, nothing is sent. Limitation: a row DELETED mid-cycle keeps the cycle's
        value until the next cycle (the switch writes False, it does not delete)."""
        prefix = "llm.hosted_mode."
        try:
            async with self._sm() as s:
                rows = (
                    await s.execute(
                        select(Setting.key, Setting.value).where(Setting.key.like(prefix + "%"))
                    )
                ).all()
        except SQLAlchemyError as exc:
            log.warning("hosted_mode_read_failed", error=str(exc))
            return HostedMode(enabled=False)
        data = self._settings.llm.hosted_mode.model_dump()
        for key, value in rows:
            field_name = key[len(prefix):]
            if "." not in field_name:
                data[field_name] = value
        try:
            return HostedMode.model_validate(data)
        except ValueError as exc:
            log.warning("hosted_mode_row_invalid", error=str(exc))
            return HostedMode(enabled=False)

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

                        client_settings = getattr(self._client, "settings", self._settings)
                        if (
                            held_for_approval(self._settings, ("trigger",), payload)
                            or held_for_approval(client_settings, ("trigger",), payload)
                        ):
                            held_scrapes.add(task.id)
                            await self._queue.defer(
                                task.id,
                                "held: online destination changed or was not confirmed",
                                worker_id=wid,
                            )
                            continue
                        outcome = await run_signal_source(
                            self._sm, source_id, http=http, client=self._client,
                            settings=self._settings,
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
        stats = RunStats()
        run_id = await runlog.open_run(
            self._sm, name="pipeline", track=track, mode=self._settings.llm.mode
        )
        try:
            return await self._run_tiers(stats, run_id, track=track, limit=limit)
        except Exception as exc:
            await runlog.finish_run(
                self._sm, run_id, status="failed",
                counts={"error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            raise

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
        )
        return stats
