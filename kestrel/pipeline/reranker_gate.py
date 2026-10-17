"""The promoted reranker as the Tier 1 fast path (SPEC-MODULES 5.4). When an active reranker exists
for the track, it predicts a score from cheap features in milliseconds; a confident keep/kill is
decided WITHOUT the LLM. An item whose prediction falls within reranker_uncertain_band of the
threshold still escalates to the LLM, and the LLM path is never deleted (cold-start + fallback).

Returns a HandlerOutcome when the reranker decides, or None to fall through to the LLM Tier 1."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Evaluation, Opportunity, OpportunityEmbedding, Source
from kestrel.learning.reranker import load_active_reranker, predict_score
from kestrel.llm.embeddings import current_model, from_bytes
from kestrel.pipeline._shared import HandlerOutcome
from kestrel.pipeline.tasks import enqueue_once

log = structlog.get_logger()


async def try_reranker_tier1(
    session: AsyncSession,
    settings: Settings,
    opp: Opportunity,
    org,
    source: Source | None,
    *,
    t2_extra: dict | None = None,
) -> HandlerOutcome | None:
    model = current_model(settings)
    loaded = await load_active_reranker(session, opp.track, embed_model=model)
    if loaded is None:
        return None  # no promoted reranker -> LLM Tier 1
    emb = await session.get(OpportunityEmbedding, opp.id)
    if emb is None or emb.model != model:
        return None  # the reranker needs a current-model embedding; fall back to the LLM
    if loaded.fitted.embed_dim and emb.dims != loaded.fitted.embed_dim:
        return None  # trained on another embedding width (a pre-D-EMBED-4 model); use the LLM
    opp._source_kind = source.kind if source else None
    pred = predict_score(loaded, opp, org, from_bytes(emb.vector))

    keep_cutoff = settings.thresholds.consider
    band = settings.learning.reranker_uncertain_band
    if abs(pred - keep_cutoff) <= band:
        return None  # within the uncertain band -> escalate to the LLM

    keep = pred >= keep_cutoff
    session.add(
        Evaluation(
            opportunity_id=opp.id, tier=1, track=opp.track,
            model=f"reranker:{loaded.ml_model.version}", verdict="keep" if keep else "kill",
            score=round(pred, 3), forced_by="reranker",
        )
    )
    if keep:
        opp.stage = "t1_pass"
        # `t2_extra`: what Tier 1 hands its Tier 2 task (a run's one-run model, D-UI-72).
        await enqueue_once(
            session, "t2", opp.id, priority=int(opp.pre_score * 100), extra=t2_extra
        )
        action = "t1_pass"
    else:
        opp.stage = "t1_reject"
        opp.stage_reason = f"reranker kill (predicted {pred:.2f})"
        action = "t1_reject"
    await session.commit()
    return HandlerOutcome(action, 1, verdict="keep" if keep else "kill")
