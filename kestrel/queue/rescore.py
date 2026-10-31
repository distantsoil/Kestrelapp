"""Score again (D-UI-50): queue one or more roles to be scored again, with the configured model
or another one for this run only. Settings never change; the choice rides on each task row as
`model_override`, and the worker builds that task's settings from a copy
(kestrel.pipeline._shared.settings_with_override).

Everything is a task row (hard rule 5): a role already past Tier 1 gets a `t2` task, a role that
never passed Tier 1 gets a `t1` task and then flows on as usual. Enqueueing never touches the
stage: the tier handlers move it when they run (handle_t2 scores a `rescore` task from any stage,
handle_t1 runs one from `t1_reject`). So a task that dies (attempts used up, the app killed for
good) leaves the role exactly where it was, never stranded in a "waiting" stage with no task
behind it. A quarantined role leaves quarantine when the new score lands, and its quarantine rows
stay as history. A role set aside by a Tier 0 rule is refused with a plain message naming the
rule and where to change it (D-UI-73), unless the operator chose Score this role anyway
(`tier0_anyway`, D-UI-74): then it gets a `t1` task flagged `tier0_override` (or a waiting
ordinary t1 task is turned into one) that runs from `t0_reject`. The choice is recorded
(kestrel.services.tier0_override) by Tier 1 in the same commit as its result, so a later Tier 0
pass never sets it aside again, and a task that dies records nothing. The operator's rules never
change. Each new evaluation is a new row beside the old ones."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Task
from kestrel.pipeline.tasks import enqueue_once
from kestrel.services.run_model import T2_ONLY_KEY

MAX_ROLES = 200
# Ahead of the normal flow (whose priority is pre_score * 100, at most a few hundred): the
# operator asked for these.
RESCORE_PRIORITY = 1000

_TO_T2 = ("t1_pass", "t2_done", "t3_done", "quarantined", "error")
_TO_T1 = ("t0_pass", "t1_reject")


@dataclass
class RescoreResult:
    queued: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)  # a scoring task was already waiting
    rules: list[int] = field(default_factory=list)  # set aside by a Tier 0 rule: refused
    # Why each refused role was set aside (kestrel.web.fit.RuleWhy), when the caller explained.
    rule_whys: dict[int, Any] = field(default_factory=dict)
    anyway: list[int] = field(default_factory=list)  # queued despite a rule (Score anyway)
    unscreened: list[int] = field(default_factory=list)  # not through Tier 0 yet
    missing: list[int] = field(default_factory=list)

    def messages(self) -> list[str]:
        """Plain sentences for the operator, one per kind of role not queued."""
        out: list[str] = []
        if self.already:
            out.append(f"{_roles(len(self.already))} already waiting to be scored.")
        if self.rules:
            whys = [self.rule_whys[i] for i in self.rules if i in self.rule_whys]
            if len(self.rules) == 1 and whys:
                out.append(f"{whys[0].sentence} Or choose Score this role anyway.")
            else:
                counts: dict[str, int] = {}
                for w in whys:
                    counts[w.word] = counts.get(w.word, 0) + 1
                detail = (" (" + ", ".join(f"{k} {v}" for k, v in counts.items()) + ")"
                          if counts else "")
                out.append(
                    f"{_roles(len(self.rules))} set aside by your rules{detail}, so the model "
                    "never read them. Open a role to see which rule and where to change it, or "
                    "choose to score them anyway."
                )
        if self.anyway:
            out.append(
                f"{_roles(len(self.anyway))} scored despite your rules, as you asked. Your rules "
                "did not change."
            )
        if self.unscreened:
            out.append(
                f"{_roles(len(self.unscreened))} not screened yet. They are scored on the next "
                "run anyway."
            )
        if self.missing:
            out.append(f"{_roles(len(self.missing))} no longer in Kestrel.")
        return out


def _roles(n: int) -> str:
    return "1 role was" if n == 1 else f"{n} roles were"


def parse_ids(raw: str | list[str] | None) -> list[int]:
    """`opp_ids` as a comma list (or repeated fields), deduplicated in order, capped at
    MAX_ROLES. Anything not a positive integer is dropped."""
    parts: list[str] = []
    for chunk in [raw] if isinstance(raw, str) else list(raw or []):
        parts.extend(str(chunk).split(","))
    seen: dict[int, None] = {}
    for p in parts:
        p = p.strip()
        if p.isdigit() and int(p) > 0:
            seen.setdefault(int(p), None)
    return list(seen)[:MAX_ROLES]


async def enqueue_rescore(
    session: AsyncSession,
    opp_ids: list[int],
    *,
    model_override: dict | None = None,
    online_approval: dict | None = None,
    tier0_anyway: bool = False,
    score_stubs: bool = False,
    explain: Callable[[Opportunity], Awaitable[Any]] | None = None,
) -> RescoreResult:
    """Stage the score-again tasks in the CALLER's session (the caller commits). No stage is
    changed here (module docstring). `tier0_anyway` queues the roles a Tier 0 rule set aside
    too (Score this role anyway); otherwise they are refused, and `explain` (when given) says why
    for each one. `score_stubs` is the operator's explicit Score again (or Score anyway) on a
    role: the task carries `score_stub`, so Tier 2 scores a posting with next to no description
    (D-SRC-14) instead of leaving it Not scored. Automatic callers leave it off."""
    result = RescoreResult()
    ids = list(dict.fromkeys(opp_ids))[:MAX_ROLES]
    rows = {
        o.id: o
        for o in (
            await session.execute(select(Opportunity).where(Opportunity.id.in_(ids)))
        ).scalars()
    }
    # `requested`: the operator asked, so a switched-off track never holds it (finding 10).
    extra: dict = {"rescore": True, "requested": True, "requested_at": utcnow_iso()}
    if score_stubs:
        extra["score_stub"] = True
    if model_override:
        extra["model_override"] = dict(model_override)
    if online_approval:
        from kestrel.services.online_consent import read_approval

        approval = read_approval(online_approval)
        if approval:
            extra["online_approval"] = approval
    for oid in ids:
        opp = rows.get(oid)
        if opp is None:
            result.missing.append(oid)
            continue
        if opp.stage == "t0_reject" and tier0_anyway:
            # Only the intent rides on the task; Tier 1 records the override when it runs
            # (D-UI-74 amended), so a task that dies leaves no "You asked to score this".
            anyway_extra = {**extra, "tier0_override": True, "tier0_reason": opp.stage_reason}
            if await _reconfirm_held(session, "t1", oid, anyway_extra):
                result.queued.append(oid)
                result.anyway.append(oid)
                continue
            added = await enqueue_once(
                session, "t1", oid, priority=RESCORE_PRIORITY, extra=anyway_extra
            )
            if not added and not await _upgrade_waiting_t1(session, oid, anyway_extra):
                result.already.append(oid)
                continue
            result.queued.append(oid)
            result.anyway.append(oid)
            continue
        if opp.stage == "t0_reject":
            result.rules.append(oid)
            if explain is not None:
                result.rule_whys[oid] = await explain(opp)
            continue
        if opp.stage in _TO_T1:
            task_type = "t1"
        elif opp.stage in _TO_T2:
            task_type = "t2"
        else:  # "new": Tier 0 has not seen it
            result.unscreened.append(oid)
            continue
        if await _reconfirm_held(session, task_type, oid, extra):
            result.queued.append(oid)
            continue
        added = await enqueue_once(
            session, task_type, oid, priority=RESCORE_PRIORITY, extra=dict(extra)
        )
        if not added:
            result.already.append(oid)
            continue
        result.queued.append(oid)
    return result


async def _reconfirm_held(
    session: AsyncSession, task_type: str, opportunity_id: int, extra: dict,
) -> bool:
    """Replace an unstarted consent hold with the operator's newly validated scoring request.

    Never edit a lease or carry an old one-time model into a new Same model confirmation.
    Compare the observed payload as well as the state so a concurrent request cannot be lost.
    """
    from kestrel.services.online_consent import APPROVAL_HOLD

    task = (await session.execute(select(Task).where(
        Task.type == task_type, Task.status == "pending", Task.last_error == APPROVAL_HOLD,
        func.json_extract(Task.payload, "$.opportunity_id") == opportunity_id,
    ))).scalar_one_or_none()
    if task is None:
        return False
    payload = dict(task.payload or {})
    for key in ("online_approval", "model_override", T2_ONLY_KEY, "lane", "lane_priority"):
        payload.pop(key, None)
    payload.update(extra)
    changed = await session.execute(update(Task).where(
        Task.id == task.id, Task.status == "pending", Task.last_error == APPROVAL_HOLD,
        Task.payload == task.payload,
    ).values(payload=payload, last_error=None, updated_at=utcnow_iso(),
             priority=RESCORE_PRIORITY).execution_options(synchronize_session=False))
    return changed.rowcount == 1


async def _upgrade_waiting_t1(session: AsyncSession, opportunity_id: int, extra: dict) -> bool:
    """A role a Tier 0 rule set aside already has a live t1 task (an ordinary one, from a pass
    that has since been undone): turn a PENDING one into the operator's Score anyway, in the
    caller's session, so it runs from t0_reject instead of skipping. True when one was turned.
    False ("already waiting") when the waiting task already is a Score anyway, or is leased: a
    leased one is running on its own payload, and the operator can ask again once it ends."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    rows = (
        await session.execute(
            select(Task).where(Task.type == "t1", Task.status.in_(("pending", "leased")),
                               oid == opportunity_id)
        )
    ).scalars().all()
    for task in rows:
        payload = dict(task.payload or {})
        if (payload.get("tier0_override") and payload.get("rescore")) or task.status != "pending":
            continue
        payload.update(extra)
        if payload.pop("lane", 0):  # a lane task the operator asked for leaves the lane
            task.priority = int(payload.pop("lane_priority", 0) or 0)
        payload.pop(T2_ONLY_KEY, None)  # the operator's pick (or the usual models) wins
        if "model_override" not in extra:
            payload.pop("model_override", None)
        task.payload = payload
        task.priority = max(task.priority or 0, RESCORE_PRIORITY)
        task.updated_at = utcnow_iso()
        return True
    return False


async def requeue_rescore_task(
    session: AsyncSession, opportunity_id: int, payload: dict | None, task_type: str = "t2"
) -> bool:
    """Queue the SAME score-again again: the earlier task's payload (its model override with
    it) with a fresh `requested_at`, ahead of the normal flow. Never touches the stage: the
    role's earlier score stands until the new one lands. False when one is already waiting."""
    extra = {
        k: v for k, v in dict(payload or {}).items()
        if k not in ("opportunity_id", "requested_at")
    }
    extra["rescore"] = True
    extra["requested"] = True
    extra["requested_at"] = utcnow_iso()
    return await enqueue_once(
        session, task_type, opportunity_id, priority=RESCORE_PRIORITY, extra=extra
    )
