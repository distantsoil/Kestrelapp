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

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Task
from kestrel.pipeline.tasks import enqueue_once
from kestrel.queue.queue import RESTART_REFUND_TYPES
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
    running: list[int] = field(default_factory=list)  # a scoring task is running right now
    # Queued by turning an ordinary waiting task into the operator's Score again (a subset of
    # `queued`): the role was already in line, and now carries the operator's request.
    upgraded: list[int] = field(default_factory=list)
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
        if self.running:
            out.append(f"{_roles(len(self.running))} already running: scoring now.")
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
    (D-SRC-18) instead of leaving it Not scored. Automatic callers leave it off."""
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
    base_extra = extra
    for oid in ids:
        opp = rows.get(oid)
        if opp is None:
            result.missing.append(oid)
            continue
        extra = await _bind_corrections(session, opp, base_extra)
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
            # A live task for the role wins the insert. A pending one takes the operator's flags
            # (rescore, score_stub, requested_at, model override, an approval confirmed in THIS
            # request); a leased one is running on its own payload and is left alone.
            kind = await _upgrade_waiting(session, task_type, oid, dict(extra))
            if kind == "running":
                result.running.append(oid)
            elif kind == "upgraded":
                result.queued.append(oid)
                result.upgraded.append(oid)
            else:
                result.already.append(oid)
            continue
        result.queued.append(oid)
    return result


async def _bind_corrections(session: AsyncSession, opp: Opportunity, extra: dict) -> dict:
    """`extra` for one role. A confirmed online send also carries the digest of the correction
    lines this role's score states now (D-SCORE-3), so Tier 2 holds it, and sends nothing, when a
    correction is added, changed or removed after the confirmation, on a retry or a give-back
    too. A request with no online approval binds nothing."""
    if "online_approval" not in extra:
        return extra
    from kestrel.learning.correction_lines import role_digest
    from kestrel.services.online_consent import CORRECTIONS_KEY

    return {**extra, CORRECTIONS_KEY: await role_digest(session, opp)}


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
    from kestrel.services.online_consent import CORRECTIONS_KEY

    for key in ("online_approval", "model_override", T2_ONLY_KEY, "lane", "lane_priority",
                CORRECTIONS_KEY):
        payload.pop(key, None)
    payload.update(extra)
    changed = await session.execute(update(Task).where(
        Task.id == task.id, Task.status == "pending", Task.last_error == APPROVAL_HOLD,
        Task.payload == task.payload,
    ).values(payload=payload, last_error=None, updated_at=utcnow_iso(),
             priority=RESCORE_PRIORITY).execution_options(synchronize_session=False))
    return changed.rowcount == 1


async def _live_rows(session: AsyncSession, task_type: str, opportunity_id: int) -> list[Task]:
    """The role's live (pending or leased) tasks of `task_type`."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    return list((
        await session.execute(
            select(Task).where(Task.type == task_type, Task.status.in_(("pending", "leased")),
                               oid == opportunity_id)
        )
    ).scalars().all())


def _waiting(task: Task, now: str) -> bool:
    """Not started: pending, or leased with a lease that has run out (queue.claimable gives such
    a task to the next worker, so it is waiting, not running), but only while it can still be
    claimed: out of attempts it is about to be dead-lettered, so it reads as running (the live
    task index still holds it) and the next press, after the sweep, queues a fresh task. A
    leased task with no expiry recorded counts as running, as queue.in_flight does."""
    if task.status == "pending":
        return True
    if not (task.status == "leased" and task.lease_until is not None and task.lease_until < now):
        return False
    # queue._claim_state: an expired lease is claimed again only while attempts remain; out of
    # attempts it is dead-lettered by reclaim_expired, and never runs (refund types excepted).
    return task.type in RESTART_REFUND_TYPES or (task.attempts or 0) < (task.max_attempts or 0)


async def _upgrade_waiting(
    session: AsyncSession, task_type: str, opportunity_id: int, extra: dict
) -> str:
    """The role already has a live `task_type` task, so Score again could not insert one. Returns
    "running" when it is running (a live lease: it runs on its own payload and the operator can
    ask again once it ends), "upgraded" when a WAITING ordinary one (an automatic pass that never
    carried the request) took the operator's flags, and "again" when the waiting one already was
    a Score again (it still takes the newer model pick and `requested_at`, but the role was
    already waiting for exactly this).

    The write is one conditional UPDATE on the status, the lease and the payload exactly as read
    (like _reconfirm_held), so a worker that leases the task between the read and the write is
    never edited under: the update does not apply, the role is read again, and a task that is
    now running reads "running".

    Consent: only what `extra` carries is written. `extra` holds an `online_approval` only when
    the caller validated a confirmed dialog in THIS request, so an upgrade never adds an approval
    the operator did not give, and a stamp the task already had is never widened. The worker
    still checks the stamp against the destination when the task runs (held_for_approval)."""
    for _attempt in range(2):
        rows = await _live_rows(session, task_type, opportunity_id)
        now = utcnow_iso()
        waiting = [t for t in rows if _waiting(t, now)]
        if not waiting:
            return "running" if rows else "again"
        task = waiting[0]
        old_payload = task.payload
        payload = dict(old_payload or {})
        was_again = bool(payload.get("rescore"))
        payload.update(extra)
        priority = task.priority or 0
        if payload.pop("lane", 0):  # a lane task the operator asked for leaves the lane
            priority = int(payload.pop("lane_priority", 0) or 0)
        payload.pop("lane_priority", None)
        payload.pop(T2_ONLY_KEY, None)  # the operator's pick (or the usual models) wins
        if "model_override" not in extra:
            payload.pop("model_override", None)
        lease = Task.lease_until.is_(None) if task.lease_until is None else (
            Task.lease_until == task.lease_until)
        upd = update(Task).where(
            Task.id == task.id, Task.status == task.status, lease,
            # Minified on both sides: the stored text may come from json_set (compact).
            func.json(Task.payload) == func.json(json.dumps(old_payload)),
        ).values(payload=payload, priority=max(priority, RESCORE_PRIORITY),
                 # A Try again clears the backoff wait, so the operator's fresh request runs now
                 # (issue #153, D-QUEUE-3).
                 retry_at=None, updated_at=utcnow_iso())
        done = await session.execute(upd.execution_options(synchronize_session=False))
        if cast(CursorResult, done).rowcount == 1:
            session.expire(task)
            return "again" if was_again else "upgraded"
        session.expire(task)  # it changed under us: read it again
    return "running"


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
        # A Try again clears the backoff wait, so the operator's fresh request runs now
        # (issue #153, D-QUEUE-3), the same way the Score again path does.
        task.retry_at = None
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
