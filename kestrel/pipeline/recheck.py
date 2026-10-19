"""Re-check the rules after a profile change (D-RULES-RECHECK). Tier 0 sets a role aside by the
profile as it was that day. When the operator saves a change to what the rules read (target or
excluded titles, seniority floor, pay floor, places, sponsorship, must-have and must-avoid, the
consulting terms), the roles set aside under the old profile get another look with the new one.

A `t0_recheck` task (one live per track) holds the work, so a kill mid-way resumes (hard rule 5).
The handler runs the rules only, no model, over roles at stage t0_reject that are active, and
moves each one that now passes back to `new`, where the ordinary Tier 0 sweep takes it onward
exactly as if it had just arrived. A role stays set aside if it still fails. Never touched: a role
the operator chose to score anyway, labeled, or took into a pursuit; those are theirs. The task is
idempotent: re-running changes nothing that already moved, and a save that changes none of the
inputs queues nothing (a fingerprint of the last completed inputs is kept per track)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Label, Opportunity, Org, Pursuit, Setting, Source, Task
from kestrel.pipeline.profile import _load_yaml, load_profile
from kestrel.pipeline.tier0 import evaluate_tier0
from kestrel.rules import helpers
from kestrel.services import tier0_override

log = structlog.get_logger()

TASK_TYPE = "t0_recheck"
FINGERPRINT_PREFIX = "tier0_recheck."
_LIVE = ("pending", "leased")
# The profile keys the Tier 0 rules read (filters.yml `profile.` references, geo and sponsorship).
INPUT_KEYS = (
    "employment_terms", "consulting_terms", "geo_rules", "requires_sponsorship", "must_have",
    "must_avoid",
)


def fingerprint(track: str) -> str:
    """A short hash of the track's Tier 0 inputs as they are on disk now."""
    block = _load_yaml().get(track)
    block = block if isinstance(block, dict) else {}
    inputs = {k: block.get(k) for k in INPUT_KEYS}
    text = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _eligible(track: str):
    """Set-aside roles to look at again: active, this track, at t0_reject, and not one the
    operator acted on (labeled or in a pursuit)."""
    return (
        Opportunity.track == track,
        Opportunity.stage == "t0_reject",
        Opportunity.archived_at.is_(None),
        ~select(Label.id).where(Label.opportunity_id == Opportunity.id).exists(),
        ~select(Pursuit.id).where(Pursuit.opportunity_id == Opportunity.id).exists(),
    )


async def eligible_count(session: AsyncSession, track: str) -> int:
    ids = (
        await session.execute(select(Opportunity.id).where(*_eligible(track)))
    ).scalars().all()
    anyway = await tier0_override.overridden_ids(session)
    return sum(1 for i in ids if i not in anyway)


async def queue_after_save(
    sessionmaker: async_sessionmaker[AsyncSession], track: str
) -> int:
    """Call after a profile save. When the Tier 0 inputs changed, queue the re-check and return
    how many roles it will look at (0: nothing queued). Same inputs again queue nothing."""
    fp = fingerprint(track)
    async with sessionmaker() as s:
        row = await s.get(Setting, f"{FINGERPRINT_PREFIX}{track}")
        if row is not None and row.value == fp:
            return 0  # these inputs were already re-checked to completion
        n = await eligible_count(s, track)
        if n:
            live = (
                await s.execute(
                    select(Task).where(
                        Task.type == TASK_TYPE, Task.status.in_(_LIVE),
                        func.json_extract(Task.payload, "$.track") == track,
                    ).order_by(Task.id)
                )
            ).scalars().all()
            if any((t.payload or {}).get("fingerprint") == fp for t in live):
                return 0  # a pass for exactly these inputs is already waiting or running
            pending = next((t for t in live if t.status == "pending"), None)
            payload = {"track": track, "fingerprint": fp}
            if pending is not None:
                pending.payload = payload
            else:
                # A running pass read the older inputs (or none is live): queue a pass. At most
                # one is ever pending.
                s.add(Task(type=TASK_TYPE, payload=payload))
        else:
            await _mark_done(s, track, fp)  # nothing set aside, nothing to look at again
        await s.commit()
    return n


async def _mark_done(s: AsyncSession, track: str, fp: str) -> None:
    """Remember the inputs a re-check completed for. Only a finished task records this, so a
    task that died or failed never blocks the next save from queueing another."""
    key = f"{FINGERPRINT_PREFIX}{track}"
    row = await s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=fp))
    else:
        row.value = fp
        row.updated_at = utcnow_iso()


async def recheck_track(
    sessionmaker: async_sessionmaker[AsyncSession], track: str, *, settings: Settings
) -> dict[str, int]:
    """Rules only: release the set-aside roles that now pass. Commits per role."""
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    profile = load_profile(track)
    counts = {"seen": 0, "released": 0}
    async with sessionmaker() as s:
        ids = (
            await s.execute(
                select(Opportunity.id).where(*_eligible(track)).order_by(Opportunity.id)
            )
        ).scalars().all()
        anyway = await tier0_override.overridden_ids(s)
    for oid in ids:
        if oid in anyway:
            continue
        async with sessionmaker() as s:
            opp = await s.get(Opportunity, oid)
            if opp is None or opp.stage != "t0_reject" or opp.archived_at is not None:
                continue
            org = await s.get(Org, opp.org_id) if opp.org_id else None
            source = await s.get(Source, opp.source_id) if opp.source_id else None
            result = evaluate_tier0(
                opp=opp, org=org, profile=profile, source=source, settings=settings,
                filters=filters, track=track,
            )
            counts["seen"] += 1
            if result.passed:
                opp.stage = "new"
                opp.stage_reason = "rechecked: the rules now let this role through"
                counts["released"] += 1
                await s.commit()
    return counts


async def drain(
    sessionmaker: async_sessionmaker[AsyncSession], *, settings: Settings, stop: Any = None
) -> dict[str, int]:
    """Run every pending re-check task. Called before the Tier 0 sweep, which then carries the
    released roles on. A failure is recorded on the task (retried), never raised."""
    from kestrel.queue.queue import TaskQueue

    queue = TaskQueue(sessionmaker)
    total = {"seen": 0, "released": 0}
    wid = "pipeline-recheck"
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=600)
        if task is None:
            break
        track = str((task.payload or {}).get("track") or "")
        try:
            got = await recheck_track(sessionmaker, track, settings=settings)
        except Exception as exc:  # one failed re-check must not stop the cycle
            log.warning("tier0_recheck_failed", track=track, error=str(exc))
            await queue.fail(task.id, f"{type(exc).__name__}: {exc}", worker_id=wid)
            break
        await queue.complete(task.id, worker_id=wid)
        fp = str((task.payload or {}).get("fingerprint") or "")
        if fp:
            async with sessionmaker() as s:
                await _mark_done(s, track, fp)
                await s.commit()
        for k, v in got.items():
            total[k] += v
        log.info("tier0_recheck_done", track=track, **got)
    return total
