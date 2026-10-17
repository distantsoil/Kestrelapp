"""Orphaned roles: waiting on a scoring step with nothing queued to take it (D-UI-56).

A role at `t0_pass` waits on Tier 1 and one at `t1_pass` waits on Tier 2. Each stage change
enqueues its next task in the same commit (pipeline.tasks.enqueue_once), so a role at one of
those stages with no live scoring task, no newer evaluation and no quarantine is an orphan: its
task died (attempts used up by crashes) or was cancelled. Left alone it reads "waiting" forever.

At worker startup (beside the expired-lease reclaim) every orphan is re-enqueued ONCE, logged,
and recorded in the `pipeline.orphan_requeue` setting row ({"<opp id>:<stage>": when}). An orphan
found at a later startup that is already in that record was re-queued and still came back
unscored, so it is not re-queued again (that would loop forever): the Pipeline shows it as
"stuck" with Score again (web.queries.scoring_states reads stuck_ids). A record is dropped once
the role is scored or its stage changes, so a later orphaning gets its one retry again. The
record is keyed by (role, stage), not by role alone: a role re-queued at t0_pass that moved on to
t1_pass and was orphaned there gets its own retry at t1_pass (release round review, finding 3).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Opportunity, Quarantine, Setting, Task
from kestrel.pipeline.tasks import enqueue_once
from kestrel.services import run_model, tier0_override

log = structlog.get_logger()

ORPHAN_KEY = "pipeline.orphan_requeue"
# Waiting stage -> (the task that takes it next, the tier whose evaluation would end the wait).
# A t0_reject role the operator chose to score anyway (a recorded tier0_override row) also waits
# on Tier 1, and is re-queued WITH the override; every other t0_reject role waits on nothing.
OVERRIDE_STAGE = "t0_reject"
NEXT_STEP: dict[str, tuple[str, int]] = {
    "t0_pass": ("t1", 1), "t1_pass": ("t2", 2), OVERRIDE_STAGE: ("t1", 1),
}
_SCORING_TASKS = ("t1", "t2", "t3", "rescore")
_LIVE = ("pending", "leased")


def _unscored_waiting():
    """Active roles at a waiting stage without the evaluation that stage waits for and without
    a quarantine at that tier (a quarantine belongs to Scoring failed, never here)."""
    clauses = []
    for stage, (_task, tier) in NEXT_STEP.items():
        if stage == OVERRIDE_STAGE:
            continue  # only overridden roles, selected by id in requeue_orphans
        has_eval = exists().where(
            Evaluation.opportunity_id == Opportunity.id, Evaluation.tier >= tier
        )
        has_q = exists().where(
            Quarantine.opportunity_id == Opportunity.id, Quarantine.tier >= tier
        )
        clauses.append(and_(Opportunity.stage == stage, ~has_eval, ~has_q))
    return and_(Opportunity.archived_at.is_(None), or_(*clauses))


def _live_task():
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    return exists().where(
        Task.type.in_(_SCORING_TASKS), Task.status.in_(_LIVE), oid == Opportunity.id
    )


async def _record(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(Setting, ORPHAN_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def record_key(opportunity_id: int, stage: str) -> str:
    return f"{opportunity_id}:{stage}"


async def stuck_ids(session: AsyncSession) -> set[int]:
    """Roles the startup sweep already re-queued once at their current waiting stage. With no
    live task they show "stuck". An entry from another stage does not count."""
    out: set[int] = set()
    record = await _record(session)
    if not record:
        return out
    ids: dict[int, str] = {}
    for key in record:
        oid, sep, stage = str(key).partition(":")
        if not sep:
            continue  # a pre-(role, stage) entry: no stage, so it is not evidence of anything
        try:
            ids[int(oid)] = stage
        except ValueError:
            continue
    if not ids:
        return out
    rows = (
        await session.execute(
            select(Opportunity.id, Opportunity.stage).where(Opportunity.id.in_(list(ids)))
        )
    ).all()
    for oid, stage in rows:
        if record_key(oid, stage) in record:
            out.add(oid)
    return out


async def requeue_orphans(sm: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Re-enqueue every orphan once (module docstring). Returns {"requeued", "stuck"}."""
    async with sm() as s:
        await run_model.sweep_stale(s)
        waiting = list(
            (
                await s.execute(
                    select(Opportunity.id, Opportunity.stage, Opportunity.pre_score, _live_task())
                    .where(_unscored_waiting())
                )
            ).all()
        )
        # Score this role anyway (D-UI-74) recorded, yet the role is back at t0_reject with no
        # Tier 1 task behind it (a reset raced the override): it waits on Tier 1 too.
        overridden = await tier0_override.overridden_ids(s)
        if overridden:
            waiting += (
                await s.execute(
                    select(Opportunity.id, Opportunity.stage, Opportunity.pre_score,
                           _live_task())
                    .where(Opportunity.id.in_(list(overridden)),
                           Opportunity.stage == OVERRIDE_STAGE,
                           Opportunity.archived_at.is_(None))
                )
            ).all()
        prev = await _record(s)
        # Keep only entries for a role still waiting at the SAME stage: a stage change (or a
        # score) drops the entry, and a legacy id-only key never matches.
        still = {record_key(oid, stage) for oid, stage, *_ in waiting}
        record = {k: v for k, v in prev.items() if k in still}
        now = utcnow_iso()
        requeued: list[int] = []
        stuck: list[int] = []
        for oid, stage, pre, live in waiting:
            if live:
                continue
            key = record_key(oid, stage)
            if key in record:
                stuck.append(oid)
                continue
            task_type = NEXT_STEP[stage][0]
            # Configured models: a run's one-run pick rode only on the task that died (D-UI-72).
            # A role the operator chose to score despite a rule re-queues that choice live.
            extra: dict[str, Any] = {"orphan_requeue": now}
            if stage == OVERRIDE_STAGE:
                extra.update({"rescore": True, "requested": True, "requested_at": now,
                              "tier0_override": True})
            added = await enqueue_once(
                s, task_type, oid, priority=int((pre or 0.0) * 100), extra=extra,
            )
            if added:
                requeued.append(oid)
                record[key] = now
        if record != prev:
            row = await s.get(Setting, ORPHAN_KEY)
            if row is None:
                s.add(Setting(key=ORPHAN_KEY, value=record))
            else:
                row.value = record
                row.updated_at = now
        await s.commit()
    if requeued:
        log.info("orphans_requeued", count=len(requeued), opportunity_ids=requeued[:50])
    if stuck:
        log.warning("orphans_stuck", count=len(stuck), opportunity_ids=stuck[:50])
    return {"requeued": len(requeued), "stuck": len(stuck)}
