"""Active learning (SPEC-MODULES 5.5): rank the unlabeled, evaluated opportunities by how much a
label on each would teach the system, so ten minutes a week of labeling improves everything
downstream. Uncertainty combines proximity to the 4.0 decision threshold, holistic-vs-weighted
divergence, distance from the existing labeled set in embedding space (to cover unrepresented
regions), and recency. (The reranker-prediction-variance component of 5.5 is deferred: HGBR exposes
no per-tree variance, and the other four signals already surface the highest-information items.)"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    OpportunityEmbedding,
    VerdictOverride,
)
from kestrel.llm.embeddings import cosine, current_model, from_bytes
from kestrel.rules.helpers import age_days


@dataclass
class UncertainItem:
    opportunity: Opportunity
    evaluation: Evaluation
    uncertainty: float
    components: dict


def calibratable() -> ColumnElement[bool]:
    """The one predicate for what Calibrate may ask about (#32): the role has a description to
    read (non-blank after trimming every kind of whitespace) and is not hidden. Shared with the
    sidebar badge (routes_control._nav_counts) so the count always equals the page."""
    return and_(
        func.length(func.trim(Opportunity.description_md, " \t\r\n")) > 0,
        Opportunity.archived_at.is_(None),
    )


async def _latest_evals(session: AsyncSession, track: str) -> dict[int, Evaluation]:
    rows = (
        (
            await session.execute(
                select(Evaluation)
                .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
                .where(Opportunity.track == track, Evaluation.tier == 2, calibratable())
                .order_by(Evaluation.id)
            )
        )
        .scalars()
        .all()
    )
    return {ev.opportunity_id: ev for ev in rows}  # ascending id -> latest wins


async def _labeled_ids(session: AsyncSession, track: str) -> set[int]:
    lab = {
        i
        for (i,) in (
            await session.execute(select(Label.opportunity_id).where(Label.track == track))
        ).all()
    }
    ov = {
        i
        for (i,) in (
            await session.execute(
                select(VerdictOverride.opportunity_id)
                .join(Opportunity, Opportunity.id == VerdictOverride.opportunity_id)
                .where(Opportunity.track == track)
            )
        ).all()
    }
    return lab | ov


async def _embeddings(
    session: AsyncSession, opp_ids: list[int], model: str
) -> dict[int, np.ndarray]:
    """Current-model vectors only (D-EMBED-4); a stale row reads as no vector (neutral novelty)."""
    if not opp_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(OpportunityEmbedding).where(
                    OpportunityEmbedding.opportunity_id.in_(opp_ids),
                    OpportunityEmbedding.model == model,
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.opportunity_id: from_bytes(r.vector) for r in rows}


def _novelty(vec: np.ndarray | None, labeled_vecs: list[np.ndarray]) -> float:
    """1 - max cosine similarity to any labeled example (far from the labeled set -> novel). Neutral
    0.5 when there is nothing to compare against."""
    if vec is None or not labeled_vecs:
        return 0.5
    return float(1.0 - max(cosine(vec, lv) for lv in labeled_vecs))


async def rank_uncertain(
    session: AsyncSession, track: str, settings: Settings, *, limit: int = 10
) -> list[UncertainItem]:
    evals = await _latest_evals(session, track)
    labeled = await _labeled_ids(session, track)
    candidates = [oid for oid in evals if oid not in labeled]
    if not candidates:
        return []

    emb = await _embeddings(session, list(evals), current_model(settings))
    labeled_vecs = [emb[i] for i in labeled if i in emb]
    apply_t = settings.thresholds.apply

    scored: list[UncertainItem] = []
    for oid in candidates:
        ev = evals[oid]
        opp = await session.get(Opportunity, oid)
        if opp is None:
            continue
        score = ev.score if ev.score is not None else 3.0
        weighted = ev.weighted_score if ev.weighted_score is not None else score
        proximity = 1.0 - min(1.0, abs(score - apply_t) / 1.5)  # near 4.0 -> ~1
        divergence = min(1.0, abs(score - weighted) / 1.5)
        novelty = _novelty(emb.get(oid), labeled_vecs)
        recency = 1.0 - min(1.0, age_days(opp.first_seen_at) / 90.0)
        components = {
            "proximity": round(proximity, 3),
            "divergence": round(divergence, 3),
            "novelty": round(novelty, 3),
            "recency": round(recency, 3),
        }
        uncertainty = 0.40 * proximity + 0.25 * divergence + 0.20 * novelty + 0.15 * recency
        scored.append(UncertainItem(opp, ev, round(uncertainty, 4), components))

    scored.sort(key=lambda u: u.uncertainty, reverse=True)
    return scored[:limit]
