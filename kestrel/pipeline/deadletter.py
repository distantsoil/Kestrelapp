"""A scoring task that dies is quarantined, never dropped (D-WORKER-5, hard rules 3 and 4).

A Tier 1 or Tier 2 task that uses up its tries (the model could not be reached, was not
installed, a key was refused, a crash) ends `dead` in the queue. Before this module the role was
left waiting with nothing behind it: absent from Needs another try, while the run notice counted
it there. Now the newest dead scoring task of an active role, with nothing live behind it, is
turned into a quarantine row at that tier carrying the task's full last error, and the task is
marked so it is never turned into a second one.

What is kept: the task row itself stays as it is (status `dead`, its payload and `last_error`),
so the raw state is preserved; the quarantine row carries the error (prefixed DEAD_TASK_MARK, so
the reader says why in plain words) and the attempts used. There is no model reply to keep: a
reply the model did send that failed validation was already quarantined by the handler's own hook.

Where the role goes: a role still waiting on scoring (new, t0_pass, t1_pass, or a t0_reject the
operator chose to score anyway) moves to stage `quarantined`, so it reads Scoring failed with
Score again, which sends it on to Tier 2 (queue.rescore; a quarantined Tier 1 item is escalated,
never rejected). A role that already has a reading (a score, a quick-screen or rules verdict, a
parked stage) keeps its stage and the row is marked as a failed score-again
(rescore_failure_prefix), as Tier 2 does for its own failures.

Idempotent and safe to run at any time: the mark is set by a conditional UPDATE on the task (dead
and not yet marked) in the same commit as the quarantine row, and a lost race writes nothing.
Hidden roles are left alone (nothing scores them; their row reads that).
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Quarantine, Task
from kestrel.pipeline._shared import rescore_failure_prefix
from kestrel.web.fit import DEAD_TASK_MARK

log = structlog.get_logger()

# The task payload key set once the task's quarantine row is written.
QUARANTINED_KEY = "quarantined_at"
SCORING_TYPES = ("t1", "t2")
_TIER = {"t1": 1, "t2": 2}
_LIVE_SCORING = ("t1", "t2", "t3", "rescore")
# Stages that still wait on scoring: the role moves to `quarantined` (Scoring failed).
_WAITING_STAGES = ("new", "t0_pass", "t1_pass")
# Stages already parked as failed: the row is added as is, the stage stays.
_PARKED = ("quarantined", "error")


def _unmarked() -> Any:
    return func.json_extract(Task.payload, f"$.{QUARANTINED_KEY}").is_(None)


def _no_live_task() -> Any:
    """Nothing scoring is pending or leased for the task's role (a Score again, a re-queue)."""
    other = aliased(Task)
    return ~exists().where(
        other.opportunity_id == Task.opportunity_id,
        other.type.in_(_LIVE_SCORING),
        other.status.in_(("pending", "leased")),
    )


def _no_newer_task() -> Any:
    """No later Tier 1 or Tier 2 task for the task's role, whatever its status: a Score again
    that already ran (a CLI run, say) has moved the role on, and this dead task is history."""
    other = aliased(Task)
    return ~exists().where(
        other.opportunity_id == Task.opportunity_id,
        other.type.in_(SCORING_TYPES),
        other.id > Task.id,
    )


def _role_active() -> Any:
    """The task's role exists and is not hidden: nothing is quarantined for a hidden or deleted
    role (nothing scores it, and its row says so)."""
    return exists().where(Opportunity.id == Task.opportunity_id, Opportunity.archived_at.is_(None))


def _candidates(task_ids: list[int] | None) -> Any:
    """The newest t1/t2 task of each active role, when it is dead, not yet quarantined, and
    nothing scoring is live for the role."""
    # Runs every cycle, so it starts from the few dead, unmarked scoring tasks and checks each
    # one's role through the indexed task.opportunity_id (0030), never a scan of every task. A
    # hidden or deleted role's dead task is filtered here, so it is never read again (D-WORKER-5
    # amended).
    stmt = select(Task).where(
        Task.type.in_(SCORING_TYPES), Task.status == "dead", Task.opportunity_id.is_not(None),
        _unmarked(), _no_newer_task(), _no_live_task(), _role_active(),
    )
    if task_ids is not None:
        stmt = stmt.where(Task.id.in_(task_ids))
    return stmt


def _stage_move(opp: Opportunity, payload: dict) -> str:
    """"move" (the role waits on scoring: it goes to Scoring failed), "parked" (already there)
    or "keep" (it has a reading of its own: the failure is a failed score-again)."""
    if opp.stage in _WAITING_STAGES:
        return "move"
    if opp.stage == "t0_reject" and payload.get("tier0_override"):
        return "move"  # Score this role anyway (D-UI-74): it waited on Tier 1
    if opp.stage in _PARKED:
        return "parked"
    return "keep"


async def _one(s: AsyncSession, task: Task, model_for: Any) -> bool:
    now = utcnow_iso()
    # Every condition is checked again at the write (D-WORKER-5 amended): a Score again or a
    # hide committed after the read above leaves this task alone, never quarantined over it.
    marked = await s.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "dead", _unmarked(), _no_newer_task(),
               _no_live_task(), _role_active())
        .values(payload=func.json_set(Task.payload, f"$.{QUARANTINED_KEY}", now),
                updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if (cast(CursorResult, marked).rowcount or 0) != 1:
        return False  # another sweep took it, or the role moved on since the read
    # Read after the write lock is held, so the stage judged is the stage now.
    # The write's own guard (_role_active) held under its lock, so the row exists.
    opp = cast(Opportunity, await s.get(Opportunity, task.opportunity_id, populate_existing=True))
    payload = dict(task.payload or {})
    tier = _TIER[task.type]
    move = _stage_move(opp, payload)
    error = (task.last_error or "").strip() or "no error was recorded"
    prefix = rescore_failure_prefix(task.id) if move == "keep" else ""
    override = payload.get("model_override") or {}
    model = (override.get("model") if isinstance(override, dict) else None) or model_for(task.type)
    s.add(Quarantine(
        opportunity_id=opp.id,
        tier=tier,
        model=model,
        prompt=None,
        raw_response=None,
        error=(prefix + DEAD_TASK_MARK + error)[:4000],
        attempts=int(task.attempts or 0),
    ))
    if move == "move":
        opp.stage = "quarantined"
        opp.stage_reason = f"t{tier} scoring stopped after its tries: {error}"[:500]
    return True


async def quarantine_dead(
    sm: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[int] | None = None,
    model_for: Any = None,
) -> list[int]:
    """Quarantine every eligible dead scoring task (module docstring), or only `task_ids`.
    Returns the role ids quarantined. `model_for(task_type)` names the configured model for the
    row's model column."""
    model_for = model_for or (lambda _t: None)
    done: list[int] = []
    async with sm() as s:
        tasks = list((await s.execute(_candidates(task_ids))).scalars().all())
        for task in tasks:
            if await _one(s, task, model_for):
                done.append(int(task.opportunity_id))
        await s.commit()
    if done:
        log.warning("dead_scoring_quarantined", count=len(done), opportunity_ids=done[:50])
    return done


def tier_model_for(settings: Any) -> Any:
    """model_for over a Settings object: the configured model of the task's tier."""
    def model_for(task_type: str) -> str | None:
        tier = {"t1": "tier1", "t2": "tier2"}.get(task_type)
        tcfg = settings.llm.tiers.get(tier) if tier else None
        return str(tcfg.model) if tcfg is not None else None

    return model_for
