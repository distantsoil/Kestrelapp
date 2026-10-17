"""Metric assembly for the analytics page (SPEC-MODULES 7). Gathers the funnel, the score-vs-outcome
discrimination and breakdowns (reusing the learning loop's outcome analysis, which already computes
them with sample sizes), plus cost/quality/time counters, into one JSON-able dict. Every rate
carries the count it came from; nothing reads as solid below the small-sample threshold (rule 6).

This is pure aggregation over the operator's own data. It draws no conclusion on its own; the honest
summary (summary.py, P12) turns these numbers into a narrative, and only there."""

from __future__ import annotations

from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.analytics.funnel import funnel
from kestrel.db.models import Evaluation, Opportunity, Pursuit, Quarantine, VerdictOverride
from kestrel.learning.outcomes import analyze_outcomes

_MIN_N = 5  # constraint 6: a rate over fewer than five is labeled insufficient
_EVAL_TIERS = (1, 2)  # only tier-1/2 quarantines are eval-family; P12/doc/research quarantines
#                       (track-less or other tiers) must not pollute a per-track validity rate


async def _count(session: AsyncSession, stmt) -> int:
    return (await session.execute(stmt)).scalar_one()


async def quality_metrics(session: AsyncSession, track: str) -> dict:
    """Override rate (how often the operator disagrees), quarantine rate, and tokens per tier. JSON
    validity/repair are inferred from quarantine vs total calls; a quarantine is a call that failed
    schema validation after the one repair attempt."""
    n_eval = await _count(
        session, select(func.count()).select_from(Evaluation).where(Evaluation.track == track)
    )
    # Count overrides by the opportunity's track (evaluation_id is nullable, so joining on it would
    # drop overrides not tied to a specific evaluation).
    n_override = await _count(
        session,
        select(func.count())
        .select_from(VerdictOverride)
        .join(Opportunity, Opportunity.id == VerdictOverride.opportunity_id)
        .where(Opportunity.track == track),
    )
    # Count only eval-family quarantines for THIS track: a global count would fold in the other
    # track, other tiers, and track-less rows (research, doc-select, even the P12 summary's own
    # quarantine) into a per-track validity rate that is then permanently and wrongly depressed.
    n_quarantine = await _count(
        session,
        select(func.count())
        .select_from(Quarantine)
        .join(Opportunity, Opportunity.id == Quarantine.opportunity_id)
        .where(Opportunity.track == track, Quarantine.tier.in_(_EVAL_TIERS)),
    )
    tier_rows = (
        await session.execute(
            select(
                Evaluation.tier,
                func.sum(Evaluation.prompt_tokens + Evaluation.completion_tokens),
                func.count(),
            )
            .where(Evaluation.track == track)
            .group_by(Evaluation.tier)
        )
    ).all()
    tokens_by_tier = {int(t): {"tokens": int(tok or 0), "calls": int(c)} for t, tok, c in tier_rows}
    total_calls = n_eval + n_quarantine
    return {
        "evaluations": n_eval,
        "override_rate": (n_override / n_eval if n_eval else None),
        "override_n": n_override,
        "quarantine_rate": (n_quarantine / total_calls if total_calls else None),
        "quarantine_n": n_quarantine,
        "json_validity_rate": (n_eval / total_calls if total_calls else None),
        "tokens_by_tier": tokens_by_tier,
        "total_tokens": sum(v["tokens"] for v in tokens_by_tier.values()),
        "insufficient": n_eval < _MIN_N,
    }


async def time_metrics(session: AsyncSession, track: str) -> dict:
    """Applications and response timing. Applications per week is the applied count over the active
    span; time to first response is the median of recorded response days."""
    applied = (
        (
            await session.execute(
                select(Pursuit.applied_at).where(
                    Pursuit.track == track, Pursuit.applied_at.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    dates = sorted(a[:10] for a in applied if a)
    # A rate over fewer than five applications is not a rate (hard rule 6): it stays None and the
    # template shows "insufficient" beside the n rather than a number that reads as solid.
    apps_per_week = None
    if len(dates) >= _MIN_N:
        from datetime import date

        span_days = max((date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days, 1)
        apps_per_week = round(len(dates) / (span_days / 7), 2)
    report = await analyze_outcomes(session, track)
    first_response_days = (
        median(report.response_times) if len(report.response_times) >= _MIN_N else None
    )
    return {
        "applications": len(dates),
        "applications_n": len(dates),
        "applications_per_week": apps_per_week,
        "applications_per_week_insufficient": len(dates) < _MIN_N,
        "median_first_response_days": first_response_days,
        "first_response_n": len(report.response_times),
    }


async def collect_metrics(session: AsyncSession, track: str) -> dict:
    """Everything the analytics page and the honest summary need for one track, as a JSON-able dict
    with sample sizes throughout. The score-vs-outcome discrimination is included verbatim from the
    outcome analysis, so the honest summary cannot paper over a non-discriminating score."""
    fn = await funnel(session, track)
    report = await analyze_outcomes(session, track)
    return {
        "track": track,
        "funnel": fn.as_dict(),
        "score_vs_outcome": {
            "n_outcomes": report.n_outcomes,
            "discriminating": report.discriminating,  # True / False / None (insufficient)
            "headline": report.headline,
            "by_score_band": [
                {"band": b.label, "n": b.n, "responded": b.responded, "rate": b.rate,
                 "insufficient": b.insufficient}
                for b in report.by_score_band
            ],
        },
        "breakdowns": {
            "by_source": report.by_source,
            "by_size": report.by_size,
            "by_ownership": report.by_ownership,
            "by_remote": report.by_remote,
        },
        "quality": await quality_metrics(session, track),
        "time": await time_metrics(session, track),
    }
