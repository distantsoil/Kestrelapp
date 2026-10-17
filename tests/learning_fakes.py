"""Synthetic label sets for the Phase 7 learning-loop gate. Builds opportunities with embeddings,
Tier 1/2 evaluations, and operator labels whose relationship to the features is KNOWN, so the tests
can assert calibration recovers sane weights and the reranker's promotion criteria evaluate
correctly in both a passing and a failing case."""

from __future__ import annotations

import numpy as np
from sqlalchemy import select

from kestrel.db.models import Evaluation, Label, Opportunity, OpportunityEmbedding, Org, Source

_DIMS = ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"]
_W_TRUE = np.array([0.40, 0.20, 0.20, 0.10, 0.10])  # role_fit-heavy ground truth
_DEFAULT_W = np.array([0.30, 0.20, 0.20, 0.15, 0.15])  # the shipped default weights
_EMBED_DIM = 32


def _verdict(score: float) -> str:
    return "apply" if score >= 4.0 else ("consider" if score >= 3.0 else "pass")


async def seed_dataset(
    session,
    *,
    n: int = 250,
    signal: bool = True,
    random_target: bool = False,
    llm_flip: float = 0.2,
    seed: int = 0,
    label: bool = True,
    stage: str = "t2_done",
    tier1: bool = True,
    base_id_tag: str = "",
) -> list[int]:
    """Seed `n` labeled employment opportunities.
    - signal=True encodes the dimension scores into the first embedding components (so the reranker
      CAN learn the target from the embedding); signal=False makes the embedding pure noise.
    - random_target=True makes the operator score independent of the dimensions (unlearnable)."""
    rng = np.random.default_rng(seed)
    org = Org(name=f"Org{base_id_tag}", normalized_name=f"org{base_id_tag}{seed}",
              size_band="100-500", ownership="pe_backed")
    src = Source(track="employment", kind="greenhouse", name=f"s{base_id_tag}{seed}", enabled=True)
    session.add_all([org, src])
    await session.flush()

    ids: list[int] = []
    for _ in range(n):
        dims = rng.uniform(1.0, 5.0, size=5)
        # ~35% high-quality items so a healthy fraction reach the apply band (>=4.0); operator_score
        # stays exactly Σ(w_true·dims) so calibration still recovers the ground-truth weights.
        if rng.uniform() < 0.35:
            dims = np.clip(dims + 1.8, 1.0, 5.0)
        if random_target:
            op_score = float(rng.uniform(1.0, 5.0))
        else:
            op_score = float(np.clip(dims @ _W_TRUE, 1.0, 5.0))
        weighted = float(np.clip(dims @ _DEFAULT_W, 1.0, 5.0))

        vec = np.zeros(_EMBED_DIM, dtype=np.float32)
        if signal and not random_target:
            vec[:5] = (dims * 3.0).astype(np.float32)
        vec[5:] = rng.normal(0, 0.05, size=_EMBED_DIM - 5).astype(np.float32)
        if not signal:
            vec = rng.normal(0, 1.0, size=_EMBED_DIM).astype(np.float32)

        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of Information Technology", org_name=org.name,
            description_md="Own IT strategy, security, and vendor management.",
            description_tokens=int(rng.integers(200, 1200)),
            comp_min=180000, comp_max=240000, comp_currency="USD", comp_interval="year",
            comp_source="parsed", pre_score=float(rng.uniform(0, 4)),
            remote_type="hybrid", seen_count=int(rng.integers(1, 4)),
            ghost_score=float(rng.uniform(0, 0.3)), date_posted="2026-07-01",
            stage=stage, stage_reason="soft: title_contains, direct_ats",
        )
        session.add(opp)
        await session.flush()
        ids.append(opp.id)

        session.add(
            OpportunityEmbedding(opportunity_id=opp.id, model="nomic-embed-text",
                                 vector=vec.tobytes(), dims=_EMBED_DIM)
        )
        dims_json = {
            k: {"score": round(float(d), 3), "why": ""}
            for k, d in zip(_DIMS, dims, strict=False)
        }
        session.add(
            Evaluation(
                opportunity_id=opp.id, tier=2, track="employment",
                verdict=_verdict(op_score), score=round(op_score + float(rng.normal(0, 0.05)), 3),
                weighted_score=round(weighted, 3), dimensions=dims_json,
                needs_review=abs(op_score - weighted) > 0.75,
            )
        )
        if tier1:
            op_keep = op_score >= 3.0
            if rng.uniform() < llm_flip:
                op_keep = not op_keep  # a noisy LLM the reranker can beat
            session.add(
                Evaluation(opportunity_id=opp.id, tier=1, track="employment",
                           verdict="keep" if op_keep else "kill")
            )
        if label:
            session.add(
                Label(opportunity_id=opp.id, track="employment", score=round(op_score, 3),
                      verdict=_verdict(op_score), dimensions=dims_json, source="active_learning")
            )
    await session.commit()
    return ids


async def seed_high_quality_labels_with_low_scores(
    session, *, n: int = 50, seed: int = 7, created_at: str | None = None, embed: bool = True
):
    """For the drift test: opportunities whose EMBEDDING says 'excellent' (reranker will predict
    high) but whose operator label is 1.0 — a market shift the reranker no longer tracks.
    `created_at` backdates the labels (to before a model's trained_at); `embed=False` leaves the
    opportunity without an embedding so the drift window's embedding filter drops it."""
    org = (await session.execute(select(Org).limit(1))).scalar_one()
    src = (await session.execute(select(Source).limit(1))).scalar_one()
    for _ in range(n):
        vec = np.zeros(_EMBED_DIM, dtype=np.float32)
        vec[:5] = np.full(5, 15.0, dtype=np.float32)  # encodes ~5.0 dims -> reranker predicts high
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of IT", org_name=org.name, description_md="x", stage="t2_done",
            remote_type="hybrid", stage_reason="soft: title_contains",
        )
        session.add(opp)
        await session.flush()
        if embed:
            session.add(OpportunityEmbedding(opportunity_id=opp.id, model="nomic-embed-text",
                                             vector=vec.tobytes(), dims=_EMBED_DIM))
        label = Label(opportunity_id=opp.id, track="employment", score=1.0, verdict="pass",
                      source="active_learning")
        if created_at is not None:
            label.created_at = created_at
        session.add(label)
    await session.commit()
