"""Outcome-driven calibration (SPEC-MODULES 5.6): the honest feedback nothing else provides. Scores
measure predicted fit; outcomes measure whether that prediction meant anything. Response rate by
score band, source, org size/ownership, remote type, and track; furthest stage by band; and the
time-to-first-response distribution that sets the ghosting threshold empirically.

IF THE SCORE IS NOT DISCRIMINATING, SAY SO PLAINLY AND FIRST. Every rate under five observations is
labeled insufficient rather than rendered as if solid (hard rule 6)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity, Org, Outcome, Source

_MIN_N = 5  # under five observations is insufficient (hard rule 6)
_SCORE_BANDS = [
    (0.0, 3.0, "<3.0"),
    (3.0, 4.0, "3.0-4.0"),
    (4.0, 4.5, "4.0-4.5"),
    (4.5, 5.01, "4.5+"),
]
_NONDISCRIMINATION_DELTA = 0.05  # top bands within 5 pts of response rate => not discriminating
# The score→response correlation must clear this to count as discriminating. Below it, band-rate
# spread is just sampling noise (a coin-flip response at ~30/band swings several points by chance).
_MIN_CORRELATION = 0.15


@dataclass
class BandStat:
    label: str
    n: int
    responded: int

    @property
    def rate(self) -> float | None:
        return self.responded / self.n if self.n else None

    @property
    def insufficient(self) -> bool:
        return self.n < _MIN_N


@dataclass
class OutcomeReport:
    track: str
    n_outcomes: int
    discriminating: bool | None  # None = not enough evidence to say either way (hard rule 6)
    headline: str
    by_score_band: list[BandStat] = field(default_factory=list)
    by_source: dict = field(default_factory=dict)
    by_size: dict = field(default_factory=dict)
    by_ownership: dict = field(default_factory=dict)
    by_remote: dict = field(default_factory=dict)
    furthest_stage_by_band: dict = field(default_factory=dict)
    response_times: list[int] = field(default_factory=list)
    suggested_ghosting_days: int | None = None


def _responded(o: Outcome) -> bool:
    return o.days_to_first_response is not None


def _band(score: float | None) -> str | None:
    if score is None:
        return None
    for lo, hi, label in _SCORE_BANDS:
        if lo <= score < hi:
            return label
    return None


def _rate_map(pairs: list[tuple[str | None, bool]]) -> dict[str, dict]:
    agg: dict[str, list[int]] = {}
    for key, responded in pairs:
        if key is None:
            continue
        bucket = agg.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += int(responded)
    return {
        k: {
            "n": n,
            "responded": r,
            "rate": (r / n if n else None),
            "insufficient": n < _MIN_N,
        }
        for k, (n, r) in agg.items()
    }


async def analyze_outcomes(session: AsyncSession, track: str) -> OutcomeReport:
    rows = (
        await session.execute(
            select(Outcome, Evaluation, Opportunity, Org, Source)
            .join(Opportunity, Opportunity.id == Outcome.opportunity_id)
            .join(
                Evaluation,
                (Evaluation.opportunity_id == Opportunity.id) & (Evaluation.tier == 2),
                isouter=True,
            )
            .join(Org, Org.id == Opportunity.org_id, isouter=True)
            .join(Source, Source.id == Opportunity.source_id, isouter=True)
            .where(Opportunity.track == track)
        )
    ).all()

    # keep one (latest) evaluation per outcome
    seen: dict[int, tuple] = {}
    for o, ev, opp, org, src in rows:
        prev = seen.get(o.id)
        if prev is None or (ev and (prev[1] is None or ev.id > prev[1].id)):
            seen[o.id] = (o, ev, opp, org, src)
    records = list(seen.values())
    n = len(records)

    bands = {label: BandStat(label, 0, 0) for _, _, label in _SCORE_BANDS}
    stage_by_band: dict[str, dict[str, int]] = {label: {} for _, _, label in _SCORE_BANDS}
    src_pairs, size_pairs, own_pairs, remote_pairs = [], [], [], []
    resp_times: list[int] = []
    score_resp: list[tuple[float, bool]] = []  # (score, responded) for the correlation test

    for o, ev, opp, org, src in records:
        score = ev.score if ev else None
        band = _band(score)
        responded = _responded(o)
        if score is not None:
            score_resp.append((score, responded))
        if band:
            bands[band].n += 1
            bands[band].responded += int(responded)
            if o.furthest_stage:
                stage_by_band[band][o.furthest_stage] = (
                    stage_by_band[band].get(o.furthest_stage, 0) + 1
                )
        src_pairs.append((src.name if src else None, responded))
        size_pairs.append((org.size_band if org else None, responded))
        own_pairs.append((org.ownership if org else None, responded))
        remote_pairs.append((opp.remote_type, responded))
        if o.days_to_first_response is not None:
            resp_times.append(o.days_to_first_response)

    band_stats = [bands[label] for _, _, label in _SCORE_BANDS]
    discriminating, headline = _discrimination(band_stats, score_resp)
    ghosting = _suggested_ghosting(resp_times)

    return OutcomeReport(
        track=track,
        n_outcomes=n,
        discriminating=discriminating,
        headline=headline,
        by_score_band=band_stats,
        by_source=_rate_map(src_pairs),
        by_size=_rate_map(size_pairs),
        by_ownership=_rate_map(own_pairs),
        by_remote=_rate_map(remote_pairs),
        furthest_stage_by_band=stage_by_band,
        response_times=sorted(resp_times),
        suggested_ghosting_days=ghosting,
    )


def _point_biserial(pairs: list[tuple[float, bool]]) -> float:
    """Correlation between score and whether the opportunity got a response. ~0 when score does not
    predict response. 0.0 when there is no variance to correlate."""
    if len(pairs) < _MIN_N:
        return 0.0
    x = np.asarray([p[0] for p in pairs], dtype=float)
    y = np.asarray([1.0 if p[1] else 0.0 for p in pairs], dtype=float)
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _discrimination(
    bands: list[BandStat], score_resp: list[tuple[float, bool]]
) -> tuple[bool | None, str]:
    """Lead with the verdict. The score discriminates only when it actually CORRELATES with getting
    a response (not merely a noisy band-rate spread that a coin-flip response produces at small n)
    AND the higher bands convert materially better. `None` when there is not enough evidence to say
    either way (never claim the score is working from no data — hard rule 6)."""
    usable = [b for b in bands if not b.insufficient and b.rate is not None]
    corr = _point_biserial(score_resp)
    if len(usable) < 2:
        return (
            None,
            "Insufficient outcome data to judge whether the score discriminates (n<5 per band).",
        )
    top, bottom = usable[-1], usable[0]
    spread = max(b.rate for b in usable) - min(b.rate for b in usable)
    discriminates = corr >= _MIN_CORRELATION and top.rate >= bottom.rate and (
        spread >= _NONDISCRIMINATION_DELTA
    )
    if not discriminates:
        return False, (
            f"The score is NOT discriminating: score-vs-response correlation is only {corr:+.2f} "
            f"and response rate varies just {spread * 100:.0f} points across bands "
            f"({bottom.label} {bottom.rate * 100:.0f}% vs {top.label} {top.rate * 100:.0f}%). "
            "The prompt or the weights need work."
        )
    return True, (
        f"The score discriminates (correlation {corr:+.2f}): {top.label} converts at "
        f"{top.rate * 100:.0f}% vs {bottom.label} at {bottom.rate * 100:.0f}%."
    )


def _suggested_ghosting(resp_times: list[int]) -> int | None:
    """The 90th percentile of time-to-first-response: beyond it, no reply is effectively ghosting.
    None (fall back to the configured default) under five observations (hard rule 6)."""
    if len(resp_times) < _MIN_N:
        return None
    s = sorted(resp_times)
    idx = min(len(s) - 1, int(round(0.9 * (len(s) - 1))))
    return int(s[idx])
