"""Scoring progress: what is being scored right now and how the last run ended (D-UI-49).

The contract shared by Today's progress panel, the Pipeline "Being scored" group and the status
rail. One function, one dict shape; every page renders from it so the numbers agree.

`scoring_progress(session)` returns:

    {
      "active": bool,          # any scoring task pending or leased (tier1, tier2, rescore)
      "total": int,            # roles in the current batch:
                               #   scored + failed + set_aside + running + waiting
      "scored": int,           # roles whose latest evaluation is a finished Tier 2 (or Tier 3)
      "failed": int,           # roles parked in Failed scoring (stage quarantined or error)
      "set_aside": int,        # roles Tier 1 set aside in this batch, plus postings with no body
                               # left Not scored (they left the queue too)
      "running": int,          # roles with a scoring task leased right now
      "waiting": int,          # roles with a scoring task pending (and none leased)
      "model": str | None,     # the Tier 2 model in use, "name (free, online)" / "(local)"
      "eta_minutes": int | None,  # None until at least 5 roles finished in this batch; an estimate
      "last_run": {            # the most recent finished desktop Run, or None
          "ended_at": str, "status": str, "scored": int, "failed": int
      } | None,
      "open_failed": int,      # failures that need you right now (web.queries.failure_summary:
                               # Scoring failed roles plus open, unqueued failures), any run
    }

"The current batch" is defined by scoring TASKS, never by when a role was first seen: every role
with a live scoring task now, plus every role whose scoring task finished (done or dead) since the
batch began. The batch begins at the start of the open Run when there is one, else at the
earliest live scoring task's creation; with neither there is no batch and every count is zero. So
scoring 3 roles again reads "of 3", not "of 143" from a 24-hour window. A role Tier 1 set aside
stays in the batch as `set_aside`, so the denominator never shrinks mid-run. Tasks with no
opportunity id are ignored (and kept out of every NOT IN). Hard rule 6 does not apply (raw
counts), but the estimate is withheld until 5 roles have finished, so it is never shown from too
few samples.

Scoring tasks are the `t1` and `t2` task types; a score-again request (D-UI-50) is a `t2` (or
`t1`) task whose payload carries `rescore`, so it is counted here with no special case. A failed
score-again of a role already scored keeps its stage (its earlier score stands), so it counts as
scored, never failed: only a role whose stage is parked counts as failed.

`open_failed` is independent of the batch and of the last run (D-UI-56): the latest small run can
read "1 scored, 0 failed" while 8 earlier failures still wait, so Today and the rail show
"N roles failed scoring" from this count (web.queries.open_quarantine_count, D-UI-70). Polled: a
handful of small queries and no model or network call; pass `settings` when the caller already
has them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Evaluation, Opportunity, Run, Task
from kestrel.services.hidden_orgs import hidden_opportunity_ids, opp_visible_clause
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON

EMPTY: dict[str, Any] = {
    "active": False,
    "total": 0,
    "scored": 0,
    "failed": 0,
    "set_aside": 0,
    "running": 0,
    "waiting": 0,
    "model": None,
    "eta_minutes": None,
    "last_run": None,
    "open_failed": 0,
}

ETA_MIN_FINISHED = 5

SCORING_TASK_TYPES = ("t1", "t2")


def _in_flight():
    """The queue's in-flight predicate (review finding 1): held tasks are not live work."""
    from kestrel.db.base import utcnow_iso
    from kestrel.queue.queue import in_flight

    return in_flight(utcnow_iso())


_SCORED_STAGES = ("t2_done", "t3_done")
_FAILED_STAGES = ("quarantined", "error")
_SET_ASIDE_STAGES = ("t1_reject",)
_FINISHED = ("done", "dead")
# How many finished desktop runs to look through for the last one that scored anything.
_LAST_RUN_LOOKBACK = 20
_APP_RUN_NAME = "desktop"  # runlog.APP_RUN_NAME; not imported to keep this module light


def _opp_id_of_task():
    return func.json_extract(Task.payload, "$.opportunity_id")


def model_label(settings: Settings, backend: str | None, model: str | None) -> str | None:
    """A model as the operator reads it: "name (free, online)" for an OpenRouter `:free` id on
    an external backend, "name (online)" for any other hosted model, "name (local)" otherwise."""
    if not model:
        return None
    from kestrel.llm.providers import is_free_variant

    b = settings.llm.backends.get(backend or "")
    if b is not None and b.external:
        return f"{model} (free, online)" if is_free_variant(model) else f"{model} (online)"
    return f"{model} (local)"


def tier2_label(settings: Settings) -> str | None:
    tcfg = settings.llm.tiers.get("tier2")
    return model_label(settings, tcfg.backend, tcfg.model) if tcfg else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


async def batch_start(session: AsyncSession) -> str | None:
    """Start of the current batch: the open Run's start, else the earliest live scoring task's
    creation, else None (nothing is being scored, so there is no batch)."""
    started = (
        await session.execute(
            select(Run.started_at)
            .where(Run.status == "running")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if started:
        return started
    return (
        await session.execute(
            select(func.min(Task.created_at)).where(
                Task.type.in_(SCORING_TASK_TYPES),
                _in_flight(),
                _opp_id_of_task().is_not(None),
            )
        )
    ).scalar_one_or_none()


async def tier2_pace_seconds(session: AsyncSession, since: str | None = None) -> float | None:
    """Seconds per role from the Tier 2 completions since `since` (only with at least
    ETA_MIN_FINISHED of them); without `since`, the measured pace of the last finished run that
    completed that many. None when there is not enough to go on."""
    if since is not None:
        n, first, last = (
            await session.execute(
                select(
                    func.count(), func.min(Evaluation.created_at), func.max(Evaluation.created_at)
                ).where(Evaluation.tier == 2, Evaluation.created_at >= since)
            )
        ).one()
        if n < ETA_MIN_FINISHED:
            return None
        a, b = _parse(first), _parse(last)
        if a is None or b is None:
            return None
        return max(0.0, (b - a).total_seconds() / (n - 1))
    rows = (
        await session.execute(
            select(Run.counts)
            .where(Run.name == _APP_RUN_NAME, Run.status != "running")
            .order_by(Run.id.desc())
            .limit(_LAST_RUN_LOOKBACK)
        )
    ).scalars().all()
    for counts in rows:
        t2 = (counts or {}).get("tier2") or {}
        done = int(t2.get("completed", 0) or 0)
        per_min = float(t2.get("per_minute", 0) or 0)
        if done >= ETA_MIN_FINISHED and per_min > 0:
            return 60.0 / per_min
    return None


async def _last_run(session: AsyncSession) -> dict[str, Any] | None:
    """The newest finished desktop run that scored anything, else the newest finished one."""
    rows = (
        await session.execute(
            select(Run)
            .where(
                Run.name == _APP_RUN_NAME,
                Run.status != "running",
                Run.finished_at.is_not(None),
            )
            .order_by(Run.finished_at.desc(), Run.id.desc())
            .limit(_LAST_RUN_LOOKBACK)
        )
    ).scalars().all()
    if not rows:
        return None

    def scoring(run: Run) -> tuple[int, int]:
        t2 = (run.counts or {}).get("tier2") or {}
        return int(t2.get("completed", 0) or 0), int(t2.get("errors", 0) or 0)

    chosen = next((r for r in rows if sum(scoring(r)) > 0), rows[0])
    scored, failed = scoring(chosen)
    return {
        "ended_at": chosen.finished_at,
        "status": chosen.status,
        "scored": scored,
        "failed": failed,
    }


async def model_in_use_detail(
    session: AsyncSession, settings: Settings, active: bool
) -> tuple[str | None, bool]:
    """(the model the full review runs on, whether it is a one-off choice). While a run is
    active: a leased Tier 2 task's own `model_override`, else a leased Tier 1 task's pick for
    the Tier 2 task it will enqueue (Score again's `model_override`, or a run's one-run
    `t2_model_override`, D-UI-50, D-UI-72), checked the way the worker checks it
    (settings_with_override for Tier 2), so a pick that would fall back to the configured model
    is never named. Otherwise the configured Tier 2 model."""
    import json

    from kestrel.pipeline._shared import ModelOverrideError, settings_with_override
    from kestrel.services.run_model import T2_ONLY_KEY

    def _parse(raw: Any) -> dict | None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        return raw if isinstance(raw, dict) and raw.get("model") else None

    if active:
        candidates: list[dict] = []
        for task_type, path in (("t2", "$.model_override"), ("t1", f"$.{T2_ONLY_KEY}"),
                                ("t1", "$.model_override")):
            where = [
                Task.type == task_type,
                Task.status == "leased",
                func.json_extract(Task.payload, path).is_not(None),
            ]
            if path == "$.model_override" and task_type == "t1":
                where.append(func.json_extract(Task.payload, "$.rescore").is_not(None))
            rows = (
                await session.execute(
                    select(func.json_extract(Task.payload, path)).where(*where).limit(5)
                )
            ).scalars().all()
            candidates += [o for o in (_parse(r) for r in rows) if o is not None]
        for override in candidates:
            try:
                settings_with_override(settings, "tier2", override)
            except ModelOverrideError:
                continue  # would fall back to the configured model: not what runs
            return model_label(settings, override.get("backend"), override.get("model")), True
    return tier2_label(settings), False


async def _model_in_use(session: AsyncSession, settings: Settings, active: bool) -> str | None:
    """The model the run panel names: "<model> (this run only)" while a task runs on a one-run
    choice, else the configured Tier 2 model."""
    label, one_run = await model_in_use_detail(session, settings, active)
    return f"{label} (this run only)" if (label and one_run) else label


async def scoring_progress(
    session: AsyncSession, settings: Settings | None = None
) -> dict[str, Any]:
    """The progress dict (module docstring). `settings` names the Tier 2 model; when omitted it
    is resolved from the setting-table layer."""
    out = dict(EMPTY)
    oid = _opp_id_of_task()
    live = (
        await session.execute(
            select(oid, Task.status).where(
                Task.type.in_(SCORING_TASK_TYPES), _in_flight(), oid.is_not(None),
                oid.not_in(hidden_opportunity_ids()),  # a hidden company is not counted
            )
        )
    ).all()
    running_ids = {int(o) for o, st in live if st == "leased"}
    waiting_ids = {int(o) for o, _st in live} - running_ids
    out["running"] = len(running_ids)
    out["waiting"] = len(waiting_ids)
    out["active"] = bool(out["running"] or out["waiting"])

    start = await batch_start(session)
    if start is not None:
        live_ids = (
            select(oid)
            .where(
                Task.type.in_(SCORING_TASK_TYPES), _in_flight(), oid.is_not(None)
            )
            .scalar_subquery()
        )
        finished_ids = (
            select(oid)
            .where(
                Task.type.in_(SCORING_TASK_TYPES),
                Task.status.in_(_FINISHED),
                Task.updated_at >= start,
                oid.is_not(None),
            )
            .scalar_subquery()
        )

        def _n(stages: tuple[str, ...]):
            return func.coalesce(func.sum(case((Opportunity.stage.in_(stages), 1), else_=0)), 0)

        # A posting with no body is left at t1_pass, Not scored (D-SRC-14): it left the queue
        # without a score, so it counts with the roles set aside, never out of the total.
        no_body = func.coalesce(
            func.sum(
                case(
                    (and_(Opportunity.stage == "t1_pass",
                          Opportunity.stage_reason == EMPTY_POSTING_STAGE_REASON), 1),
                    else_=0,
                )
            ),
            0,
        )
        scored, failed, set_aside, unscored = (
            await session.execute(
                select(
                    _n(_SCORED_STAGES), _n(_FAILED_STAGES), _n(_SET_ASIDE_STAGES), no_body
                ).where(
                    Opportunity.id.in_(finished_ids), Opportunity.id.not_in(live_ids),
                    opp_visible_clause(),
                )
            )
        ).one()
        out["scored"], out["failed"] = int(scored), int(failed)
        out["set_aside"] = int(set_aside) + int(unscored)
    out["total"] = (
        out["scored"] + out["failed"] + out["set_aside"] + out["running"] + out["waiting"]
    )

    if out["active"] and start is not None:
        pace = await tier2_pace_seconds(session, since=start)
        if pace is not None:
            remaining = out["running"] + out["waiting"]
            out["eta_minutes"] = max(1, round(remaining * pace / 60.0))

    out["last_run"] = await _last_run(session)
    from kestrel.web.queries import open_quarantine_count

    out["open_failed"] = await open_quarantine_count(session)  # the one "needs you" count
    if settings is None:
        from kestrel.services.setting_overrides import load_settings_with_db

        settings = await load_settings_with_db(session)
    out["model"] = await _model_in_use(session, settings, out["active"])
    return out
