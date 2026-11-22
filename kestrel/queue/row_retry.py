"""What a retry of one failed row runs (#206 round 11, D-UI-790).

Every action that tries a Needs another try row again (Try again, the Runs card, Try all again,
Try with a different model, Score it anyway on a role no rule set aside, Score again naming the
row, and the banner's group fix) asks `plan` first and queues exactly the task it names, through
`enqueue_rescore(task_type=...)` or the plain re-queue. The task comes from the stored row only
(`Quarantine.tier`), never from the role's current stage, a group label or anything the client
sent. The role's stage can only refuse: a role that has moved on since the failure is not re-run
behind the row. A refusal queues nothing and spends no retry (hard rule 4: the row stays listed)."""

from __future__ import annotations

from dataclasses import dataclass

from kestrel.db.models import Opportunity, Quarantine
from kestrel.queue.rescore import _TO_T2

# The failed row's tier to the task a retry of it runs. Only Fit score is run again from a row:
#   Tier 1 (Quick sort): the failure already kept the role and queued Fit score (hard rule 4). A
#     Quick sort re-run could set aside a role that rule saved, so there is nothing to retry.
#   Tier 3 (Deep read): escalation is operator-confirmed only, from the role's Escalate.
#   Every other tier: no worker task runs that step from here.
ROW_TASK: dict[int, str] = {2: "t2"}
# The stages each task may run from. Outside them the role has moved on since the row failed.
RUNS_FROM: dict[str, tuple[str, ...]] = {"t2": _TO_T2}

NO_ROLE = ("This did not come from scoring a role, so it cannot be tried again here. "
           "Run it again where it started, or remove it from this list.")
ROLE_GONE = "This role no longer exists, so there is nothing to try again. Nothing was queued."
KEPT = ("Quick sort already kept this role and passed it on to Fit score, so there is nothing to "
        "try again here. Nothing was queued. Use Score again on the role to score it now.")
DEEP = ("Deep read runs again only when you choose Escalate on the role: escalation is "
        "operator-confirmed. Nothing was queued.")
OTHER = "This step can't be run again from here. Nothing was queued."
MOVED_ON = ("This role has moved on since this failed, so trying that step again here could undo "
            "that. Nothing was queued. Use Score again on the role to score it now.")


@dataclass(frozen=True)
class RowPlan:
    """`task` is the task type to queue, or None with `why` saying, in plain words, why not."""

    task: str | None
    why: str | None = None


def plan(q: Quarantine, opp: Opportunity | None) -> RowPlan:
    """The one decision of what retrying `q` runs, read from the row and its role only."""
    if q.opportunity_id is None:
        return RowPlan(None, NO_ROLE)
    if opp is None or opp.id != q.opportunity_id:
        return RowPlan(None, ROLE_GONE)
    task = ROW_TASK.get(int(q.tier or 0))
    if task is None:
        why = {1: KEPT, 3: DEEP}.get(int(q.tier or 0), OTHER)
        return RowPlan(None, why)
    if opp.stage not in RUNS_FROM[task]:
        return RowPlan(None, MOVED_ON)
    return RowPlan(task)
