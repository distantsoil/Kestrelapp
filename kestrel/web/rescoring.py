"""Being scored again (issue #74): a role that already has a score and is queued or running for a
new one. One rule for the role page, the Pipeline row and side panel, and the Today card, so they
agree. The earlier score stays on screen, labeled as the earlier score, until the new evaluation
lands.

State comes from the task row alone: a leased task is "running", any other live one is "queued".
A queued role's place in line is the count of live Tier 1 and Tier 2 tasks the worker takes
before it (it leases by priority, then oldest id), plus any already running. That is a plain
count, not a promise about time. A task held for the operator's approval, or given back unstarted
because it cannot run now (`is_deferred`: an online key out of credit, a local-only hold), is not
in line and does not count ahead of anyone, and a role with nothing live is not here at all.
"Stalled" means exactly that deferral; a task that failed and was requeued is simply waiting its
turn."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Task
from kestrel.queue.queue import in_flight, is_deferred

TITLE = "Being scored again"
FIRST_TITLE = "Being scored"
EARLIER = "Earlier score"
STALLED_HINT = "Waiting to try again. Kestrel will pick it up when it can."
_TASKS = ("t1", "t2", "t3", "rescore")
_LINE_TASKS = ("t1", "t2")
_SAMPLES = 20
_MIN_SAMPLES = 3


@dataclass(frozen=True)
class Rescoring:
    state: str  # "queued" or "running"
    ahead: int = 0
    typical: str | None = None
    again: bool = True  # the role already has a Tier 2 score; False: its first one
    stalled: bool = False  # deferred: it cannot run now, so nothing to watch for

    @property
    def title(self) -> str:
        return TITLE if self.again else FIRST_TITLE

    @property
    def hint(self) -> str:
        if self.stalled:
            return STALLED_HINT
        if self.state == "running":
            return f"Scoring now. Usually {self.typical}." if self.typical else "Scoring now."
        if self.ahead <= 0:
            return "Waiting for its turn. It is next in line."
        return f"Waiting for its turn. {self.ahead} ahead of it."

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "title": self.title, "hint": self.hint,
                "stalled": self.stalled}


def typical_words(median_ms: float | None) -> str | None:
    """How long a score usually takes, in plain words, from the median of recent ones."""
    if not median_ms or median_ms <= 0:
        return None
    seconds = median_ms / 1000
    if seconds < 45:
        return "under a minute"
    minutes = round(seconds / 60)
    return "about a minute" if minutes <= 1 else f"about {minutes} minutes"


async def _typical(session: AsyncSession) -> str | None:
    rows = (
        await session.execute(
            select(Evaluation.latency_ms)
            .where(Evaluation.tier >= 2, Evaluation.latency_ms > 0)
            .order_by(Evaluation.id.desc())
            .limit(_SAMPLES)
        )
    ).scalars().all()
    if len(rows) < _MIN_SAMPLES:
        return None
    return typical_words(float(median(rows)))


async def rescoring_states(session: AsyncSession, opp_ids: list[int]) -> dict[int, Rescoring]:
    """The roles in `opp_ids` that have a live scoring task, with where each one is."""
    ids = sorted({int(i) for i in opp_ids})
    if not ids:
        return {}
    from kestrel.services.online_consent import APPROVAL_HOLD

    oid = func.json_extract(Task.payload, "$.opportunity_id")
    live = (
        await session.execute(
            select(oid, Task.id, Task.priority, Task.status, is_deferred()).where(
                Task.type.in_(_TASKS),
                in_flight(utcnow_iso()),
                oid.in_(ids),
                func.coalesce(Task.last_error, "") != APPROVAL_HOLD,
            )
        )
    ).all()
    if not live:
        return {}
    # Per role the task that decides it: running beats queued; of several queued, the first.
    pick: dict[int, tuple[int, int, str, bool]] = {}
    for o, tid, prio, status, deferred in live:
        if o is None:
            continue
        o = int(o)
        cur = pick.get(o)
        mine = (int(tid), int(prio or 0), str(status), bool(deferred))
        if cur is None or (mine[2] == "leased") > (cur[2] == "leased") or (
            (mine[2] == "leased") == (cur[2] == "leased") and mine[0] < cur[0]
        ):
            pick[o] = mine
    scored = set(
        (
            await session.execute(
                select(Evaluation.opportunity_id)
                .where(Evaluation.opportunity_id.in_(list(pick)), Evaluation.tier >= 2)
                .distinct()
            )
        ).scalars()
    )
    queued = {o: t for o, t in pick.items() if t[2] != "leased"}
    # Every queued role's place in line comes from one statement: ROW_NUMBER over the claim
    # order (priority desc, then id asc) of the tasks that are actually in line, plus whatever
    # is already running. Deferred and approval-held tasks are not in line.
    ahead: dict[int, int] = {}
    if queued:
        now = utcnow_iso()
        in_line = (
            Task.type.in_(_LINE_TASKS),
            in_flight(now),
            func.coalesce(Task.last_error, "") != APPROVAL_HOLD,
            ~is_deferred(),
        )
        ranked = (
            select(
                Task.id.label("id"),
                (func.row_number().over(order_by=(Task.priority.desc(), Task.id.asc())) - 1)
                .label("n"),
            )
            .where(*in_line, Task.status != "leased")
            .subquery()
        )
        running = select(func.count()).where(*in_line, Task.status == "leased").scalar_subquery()
        rows = (
            await session.execute(
                select(ranked.c.id, ranked.c.n, running).where(
                    ranked.c.id.in_([t[0] for t in queued.values()])
                )
            )
        ).all()
        place = {int(tid): int(n) + int(r or 0) for tid, n, r in rows}
        ahead = {o: place.get(t[0], 0) for o, t in queued.items()}
    typical = await _typical(session) if any(t[2] == "leased" for t in pick.values()) else None
    out: dict[int, Rescoring] = {}
    for o, (_tid, _prio, status, deferred) in pick.items():
        again = o in scored
        if status == "leased":
            out[o] = Rescoring("running", 0, typical, again)
        else:
            out[o] = Rescoring("queued", ahead.get(o, 0), None, again, stalled=deferred)
    return out
