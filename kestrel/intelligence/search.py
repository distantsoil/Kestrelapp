"""Semantic search over the opportunity embedding index (SPEC-INTERFACES 4, search_opportunities).
Embeds the query with the same model the corpus was embedded with and ranks by cosine over the
stored vectors. The index is built at ingestion (the embed task since Phase 3), so this is a read
over accumulated data with no crawling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity, OpportunityEmbedding
from kestrel.llm.embeddings import NOMIC_QUERY_PREFIX, cosine, current_model, from_bytes
from kestrel.llm.embeddings import prefixes as embed_prefixes

# Must match kestrel/pipeline/embed.py DOCUMENT_PREFIX's counterpart: nomic-embed-text is prefix
# conditioned, and embedding a query without "search_query: " against a "search_document: " corpus
# degrades ranking quietly rather than failing.
# The in-process model is symmetric and takes no prefix (embeddings.prefixes picks per model).
QUERY_PREFIX = NOMIC_QUERY_PREFIX


@dataclass
class SearchHit:
    opportunity_id: int
    title: str | None
    org_name: str | None
    track: str
    score: float  # cosine similarity to the query
    verdict: str | None
    eval_score: float | None


async def search_opportunities(
    session: AsyncSession, client: Any, query: str, *, track: str | None = None, limit: int = 10
) -> list[SearchHit]:
    """Embed `query`, rank opportunities by cosine over their stored embeddings, and return the top
    `limit` with their latest tier-2 verdict/score. Empty when the query embeds to nothing or the
    index is empty."""
    if not (query or "").strip():
        return []
    # nomic-embed-text is prefix conditioned: the corpus is embedded with "search_document: "
    # (kestrel/pipeline/embed.py) and a query must use "search_query: ", or the two sides land in
    # slightly different regions and ranking degrades quietly rather than failing.
    settings = client.settings
    _doc_prefix, query_prefix = embed_prefixes(settings)
    qvec = np.asarray((await client.embed([f"{query_prefix}{query}"]))[0], dtype=np.float32)

    # Only rows from the model that embedded the query: another model's vectors are another space
    # (and usually another width), so ranking across them is meaningless (D-EMBED-4).
    stmt = (
        select(OpportunityEmbedding, Opportunity)
        .join(Opportunity, Opportunity.id == OpportunityEmbedding.opportunity_id)
        .where(OpportunityEmbedding.model == current_model(settings))
    )
    if track:
        stmt = stmt.where(Opportunity.track == track)
    rows = (await session.execute(stmt)).all()

    scored = sorted(
        ((cosine(qvec, from_bytes(emb.vector)), opp) for emb, opp in rows),
        key=lambda t: t[0],
        reverse=True,
    )[:limit]

    hits: list[SearchHit] = []
    for score, opp in scored:
        ev = (
            await session.execute(
                select(Evaluation)
                .where(Evaluation.opportunity_id == opp.id, Evaluation.tier == 2)
                .order_by(Evaluation.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        hits.append(
            SearchHit(
                opportunity_id=opp.id, title=opp.title, org_name=opp.org_name, track=opp.track,
                score=round(float(score), 4),
                verdict=ev.verdict if ev else None, eval_score=ev.score if ev else None,
            )
        )
    return hits
