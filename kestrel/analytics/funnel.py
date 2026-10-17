"""The funnel, per track (SPEC-MODULES 7). Conversion at each step from ingested through to offer,
each step carrying the sample size it was computed from so no ratio is read as solid when it rests
on a handful of items (constraint 6).

The eleven steps map onto data the pipeline already records: the opportunity `stage` for the tier
gates, a tier-2 `evaluation` row for "evaluated", and the `pursuit` status (furthest reached, using
the `outcome` when a terminal status has hidden how far it got) for shortlisted onward. The pursuit
status vocabulary is track-specific, so the status->step map is per track."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity, Outcome, Pursuit

STEPS = (
    "ingested",
    "tier0_pass",
    "tier1_pass",
    "tier2_evaluated",
    "shortlisted",
    "pursued",
    "first_response",
    "screening",
    "interview",
    "final",
    "offer",
)

# Opportunity.stage -> how far through the tier gates it got (0 = did not pass tier 0).
_STAGE_RANK = {
    "new": 0, "t0_reject": 0, "quarantined": 0, "error": 0,
    "t0_pass": 1, "t1_reject": 1,
    "t1_pass": 2,
    "t2_done": 3, "t3_done": 3,
}

# pursuit.status -> the highest funnel step index it proves the pursuit reached. Terminal negatives
# (rejected/ghosted/...) only prove "pursued"; the Outcome.furthest_stage refines them when present.
_PURSUIT_STEP = {
    "employment": {
        "shortlisted": 4, "preparing": 4, "applied": 5,
        "screening": 7, "interviewing": 8, "final": 9, "offer": 10, "accepted": 10,
        "rejected": 5, "withdrawn": 5, "ghosted": 5,
    },
    "consulting": {
        "shortlisted": 4, "researching": 4, "outreach_sent": 5, "conversation": 6,
        "scoping": 7, "proposal_sent": 9, "negotiating": 9, "won": 10,
        "lost": 5, "no_response": 5,
    },
}
# Outcome.furthest_stage -> step index, to recover how far a now-terminal pursuit actually reached.
_FURTHEST_STEP = {
    "shortlisted": 4, "applied": 5, "pursued": 5, "response": 6, "first_response": 6,
    "screening": 7, "interview": 8, "interviewing": 8, "final": 9, "offer": 10,
}


@dataclass
class FunnelStep:
    name: str
    count: int
    sample_size: int  # the count of the PREVIOUS step, i.e. the denominator of the conversion
    conversion: float | None


@dataclass
class Funnel:
    track: str
    steps: list[FunnelStep]

    def as_dict(self) -> dict:
        return {
            "track": self.track,
            "steps": [
                {"name": s.name, "count": s.count, "sample_size": s.sample_size,
                 "conversion": s.conversion}
                for s in self.steps
            ],
        }


def _furthest_step(status: str | None, furthest_stage: str | None, responded: bool,
                   track: str) -> int:
    step = _PURSUIT_STEP.get(track, {}).get(status or "", 4)
    if furthest_stage:
        step = max(step, _FURTHEST_STEP.get(furthest_stage, 0))
    if responded:  # a recorded first response proves at least the first_response step
        step = max(step, 6)
    return step


async def funnel(session: AsyncSession, track: str) -> Funnel:
    """Compute the funnel for one track. Opportunity steps come from stage/evaluation; pursuit steps
    from the furthest step each pursuit reached. Each conversion is count / previous count.

    The pursuit steps (shortlisted onward) are sourced independently of the opportunity steps, so a
    manually-shortlisted pursuit (one with no tier-2 evaluation) can make shortlisted exceed
    tier2_evaluated, giving a conversion above 1.0. That is real and left visible, not clamped: it
    means items entered a later step without being recorded at the earlier one."""
    opps = (
        (await session.execute(select(Opportunity).where(Opportunity.track == track)))
        .scalars()
        .all()
    )
    counts = [0] * len(STEPS)
    counts[0] = len(opps)
    for o in opps:
        rank = _STAGE_RANK.get(o.stage or "new", 0)
        if rank >= 1:
            counts[1] += 1
        if rank >= 2:
            counts[2] += 1

    counts[3] = (
        await session.execute(
            select(func.count(func.distinct(Evaluation.opportunity_id))).where(
                Evaluation.tier == 2, Evaluation.track == track
            )
        )
    ).scalar_one()

    pursuits = (
        (
            await session.execute(
                select(Pursuit, Outcome)
                .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
                .outerjoin(Outcome, Outcome.pursuit_id == Pursuit.id)
                .where(Opportunity.track == track)
            )
        )
        .all()
    )
    for pursuit, outcome in pursuits:
        responded = outcome is not None and outcome.days_to_first_response is not None
        furthest_stage = outcome.furthest_stage if outcome is not None else None
        reached = _furthest_step(pursuit.status, furthest_stage, responded, track)
        for step in range(4, reached + 1):
            counts[step] += 1

    steps: list[FunnelStep] = []
    for i, name in enumerate(STEPS):
        prev = counts[i - 1] if i > 0 else counts[0]
        conv = (counts[i] / prev) if (i > 0 and prev > 0) else (1.0 if i == 0 else None)
        steps.append(FunnelStep(name=name, count=counts[i],
                                sample_size=(prev if i > 0 else counts[0]), conversion=conv))
    return Funnel(track=track, steps=steps)
