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
#   Tier 1 (Quick sort): a reply that could not be read was already promoted and passed on (hard
#     rule 4), so that row has nothing to retry. A Quick sort task that used up its tries
#     (deadletter.py) left its role waiting at `quarantined`; hard rule 4 escalates that row to
#     Fit score, so it is tried again instead of refused (#220, see QUICK_SORT_RETRY_TASK).
#   Tier 3 (Deep read): escalation is operator-confirmed only, from the role's Escalate.
#   Every other tier: no worker task runs that step from here.
ROW_TASK: dict[int, str] = {2: "t2"}
# The one tier-1 row that can be retried, and the task its retry runs. A role at `quarantined`
# may have got there from deadletter.py: a Quick sort task that dies while the role still waits
# moves it (`_stage_move`, 101-112, 146-149); the same stage is also written by a Tier 2 refusal,
# quarantine or over-budget result (tier2.py:432, 448, 470) and by a dead Tier 2 task. In every
# case a tier-1 row is retried by escalating it to Fit score, while a promoted reply-quarantine
# stays at `t1_pass`. So the stage tells the two apart without reading the error text.
QUICK_SORT_RETRY_STAGE = "quarantined"
QUICK_SORT_RETRY_TASK = "t2"
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
    tier = int(q.tier or 0)
    if tier == 1:
        # Only the dead-letter outcome is retryable: the role is still waiting at `quarantined`
        # and hard rule 4 escalates it to Fit score. A promoted reply-quarantine (t1_pass) or a
        # role that has moved on keeps the refusal it already had.
        if opp.stage != QUICK_SORT_RETRY_STAGE:
            return RowPlan(None, KEPT)
        return RowPlan(QUICK_SORT_RETRY_TASK)
    task = ROW_TASK.get(tier)
    if task is None:
        why = {3: DEEP}.get(tier, OTHER)
        return RowPlan(None, why)
    if opp.stage not in RUNS_FROM[task]:
        return RowPlan(None, MOVED_ON)
    return RowPlan(task)
