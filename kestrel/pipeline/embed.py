"""The embed task handler (AFTERACTION 1.1, SPEC.md 4.3 / 5.2).

`embed` tasks have been enqueued at ingestion since Phase 3 (`ingest/runner.py`, `ingest/manual.py`,
`ingest/signal_runner.py`) and nothing ever drained them, so `opportunity_embedding` had no
production writer at all. Four features depend on that table and were therefore inert:

  * the learned reranker: `build_rows` skips every label without a vector, so `train_reranker`
    returned "insufficient usable labels (n=0)" no matter how much the operator labeled;
  * the Tier 1 reranker gate (`pipeline/reranker_gate.py`);
  * few-shot injection in Tier 2, which returns ([], []) with no query embedding;
  * active-learning novelty (`learning/active.py`), which fell back to a neutral 0.5.

Semantic search (`intelligence/search.py`) also reads the table and now has a corpus to read.

NOT fixed by this, despite what an earlier draft of this docstring claimed: **dedup pass 3**
(SPEC.md 5.5) does not exist. `normalize.py::_assign_dedup_group` implements passes 1 and 2 only,
and it runs INSIDE `normalize_and_store` while the embed task is enqueued by its caller afterward,
so the vector does not exist at the point dedup would need it. Writing vectors is necessary for
pass 3 and nowhere near sufficient; it needs its own post-embed hook. Still deferred.

Mirrors `handle_t1`: same signature, same idempotent-on-resume shape, same HandlerOutcome contract,
so `Worker.drain_tier` can drive it unchanged.
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.models import Opportunity, OpportunityEmbedding, Task
from kestrel.ingest.tokens import truncate_head
from kestrel.llm.client import LLMClient
from kestrel.llm.embeddings import NOMIC_DOCUMENT_PREFIX, current_model, prefixes, to_bytes
from kestrel.pipeline._shared import HandlerOutcome

log = structlog.get_logger()

# nomic-embed-text has an 8192-token window. The embedding is a similarity key, not an evaluation,
# so the head of the description carries almost all of the signal: title, org, seniority, and the
# opening responsibilities. Truncating well inside the window keeps a long JD from silently
# dominating the vector and keeps the call fast enough to run on every ingested item.
_EMBED_TOKEN_BUDGET = 1500

# nomic-embed-text is prefix conditioned: it expects `search_document: ` on indexed passages and
# `search_query: ` on queries, and embedding both sides raw puts them in slightly different regions
# of the space. Doc-to-doc comparisons (few-shot, novelty, reranker features) are self-consistent
# either way, but query-to-doc search is measurably worse without it, and it degrades quietly
# rather than failing. Applied here and in intelligence/search.py, which is the only query path.
# The prefix belongs to the MODEL: the in-process model is symmetric and takes none
# (embeddings.prefixes), and the in-process tokenizer truncates at its own 256-token window.
DOCUMENT_PREFIX = NOMIC_DOCUMENT_PREFIX


def embedding_text(opp: Opportunity, prefix: str = DOCUMENT_PREFIX) -> str:
    """The canonical text for an opportunity's vector.

    Title and organization lead deliberately: the same role at the same company posted to three
    boards should land close together, and few-shot retrieval ranks by this same vector.
    """
    parts = [
        (opp.title or "").strip(),
        (opp.org_name or "").strip(),
        truncate_head(opp.description_md or "", _EMBED_TOKEN_BUDGET).strip(),
    ]
    body = "\n".join(p for p in parts if p)
    return f"{prefix}{body}" if body else ""


def text_hash(text: str) -> str:
    """Key for "has this exact text already been embedded". See migration 0007 for why this hashes
    the model input rather than opportunity.content_hash or version."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _existing(session: AsyncSession, opportunity_id: int) -> OpportunityEmbedding | None:
    return await session.get(OpportunityEmbedding, opportunity_id)


async def handle_embed(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: LLMClient,
    settings: Settings,
    *,
    opportunity_id: int,
    profiles: dict | None = None,
) -> HandlerOutcome:
    """Embed one opportunity and store the vector. Idempotent on resume.

    `profiles` is accepted and unused so the signature matches the other handlers and
    `Worker.drain_tier` needs no special case.
    """
    model = current_model(settings)
    doc_prefix, _query_prefix = prefixes(settings)

    async with sessionmaker() as session:
        opp = await session.get(Opportunity, opportunity_id)
        if opp is None:
            return HandlerOutcome("skip", 0)
        text = embedding_text(opp, doc_prefix)
        if not text:
            # Nothing to embed. Recorded as a skip rather than an error so the task completes and
            # the queue does not fill with permanently unprocessable rows.
            return HandlerOutcome("skip", 0)
        digest = text_hash(text)
        existing = await _existing(session, opportunity_id)
        # Idempotent resume (hard rule 5) WITHOUT going stale. A task re-leased after a crash must
        # not re-embed, but a re-seen posting whose description changed enqueues a fresh embed task
        # precisely so the vector is refreshed, and that task must not be discarded. Keying on the
        # text means both hold. A model change also forces recomputation, because vectors from two
        # different models are not comparable.
        if (
            existing is not None
            and existing.model == model
            and existing.vector
            and existing.text_hash == digest
        ):
            return HandlerOutcome("skip", 0)

    try:
        vectors = await client.embed([text])
    except Exception as exc:
        # Returning "error" lets the queue retry with backoff, which is right for a transient
        # Ollama failure. It is logged with the opportunity so a persistent failure is diagnosable
        # rather than an anonymous growing backlog, which is exactly how this feature stayed
        # broken for so long.
        log.warning("embed_failed", opportunity_id=opportunity_id, error=str(exc))
        return HandlerOutcome("error", 0, error=f"embed failed: {exc}")

    if not vectors or not vectors[0]:
        # The likeliest real cause is a wrong or unpulled model name returning an empty `data`
        # array, so this must not be the one error path with no log line.
        log.warning("embed_empty_vector", opportunity_id=opportunity_id, model=model)
        return HandlerOutcome("error", 0, error="embedding backend returned no vector")
    vector = vectors[0]

    async with sessionmaker() as session:
        row = await _existing(session, opportunity_id)
        if row is None:
            session.add(
                OpportunityEmbedding(
                    opportunity_id=opportunity_id,
                    model=model,
                    vector=to_bytes(vector),
                    dims=len(vector),
                    text_hash=digest,
                )
            )
        else:
            row.model = model
            row.vector = to_bytes(vector)
            row.dims = len(vector)
            row.text_hash = digest
        try:
            await session.commit()
        except IntegrityError:
            # Another worker inserted the row between our check and this commit. opportunity_id is
            # the primary key and ingestion adds embed tasks directly rather than through
            # enqueue_once, so two live tasks for one opportunity is normal after a re-ingest, and
            # with concurrency 4 the race is the expected case rather than an exotic one. The other
            # worker computed the same vector from the same text, so losing this one costs nothing.
            # Reporting an error here would inflate the run's error count with non-errors and
            # persist a scary IntegrityError string on a task that actually succeeded.
            await session.rollback()
            return HandlerOutcome("skip", 0)

    return HandlerOutcome("embedded", 0)


RESWEEP_S = 1800.0  # how often an incomplete sweep re-queues stale vectors


async def enqueue_stale_embeddings(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Enqueue an `embed` task for every opportunity whose vector is missing or was produced by a
    model other than the current one, unless a live embed task for it already exists. Returns how
    many were added.

    This is what makes a model switch (Ollama nomic-embed-text to the in-process model, or back)
    re-embed the corpus: `handle_embed` already recomputes a row whose model differs, but only
    when a task reaches it, and ingestion enqueues embed tasks for NEW postings only. It also
    backfills opportunities whose embed tasks failed for good while no engine was available (a
    BYOK install before the in-process engine existed). Readers ignore stale rows meanwhile
    (embeddings.current_model), so nothing compares vectors across spaces in the gap.

    Done once per model, but only once the backlog is empty: while stale rows remain (tasks still
    queued, or failed for good because the model files were missing or corrupt), the sweep runs
    again at most every RESWEEP_S and re-queues them, so fixing the install heals the corpus
    (hard rule 5). The state is the `embed_swept_model` setting row: "<model>" when done,
    "<model>|<epoch>" while incomplete. A worker cycle otherwise costs one primary-key read.
    """
    from kestrel.db.models import Setting
    from kestrel.services.setting_overrides import EMBED_SWEPT_MODEL_KEY

    model = current_model(settings)
    if not model:
        return 0
    async with sessionmaker() as session:
        swept = await session.get(Setting, EMBED_SWEPT_MODEL_KEY)
        state = str(swept.value) if swept is not None else ""
        if state == model:
            return 0  # this model's backlog drained; ingestion enqueues new postings itself
        now = time.time()
        recent = False
        if state.startswith(model + "|"):
            try:
                recent = now - float(state.split("|", 1)[1]) < RESWEEP_S
            except ValueError:
                recent = False  # an unreadable stamp: sweep now
        stale = (
            await session.execute(
                select(Opportunity.id)
                .outerjoin(
                    OpportunityEmbedding, OpportunityEmbedding.opportunity_id == Opportunity.id
                )
                .where(
                    (OpportunityEmbedding.opportunity_id.is_(None))
                    | (OpportunityEmbedding.model != model)
                )
            )
        ).scalars().all()
        live = {
            int(oid)
            for (oid,) in (
                await session.execute(
                    select(func.json_extract(Task.payload, "$.opportunity_id")).where(
                        Task.type == "embed", Task.status.in_(("pending", "leased"))
                    )
                )
            ).all()
            if oid is not None
        }
        if stale and recent:
            return 0  # open and recently swept: let the queued tasks run before re-queueing
        added = [oid for oid in stale if oid not in live]
        for oid in added:
            session.add(Task(type="embed", payload={"opportunity_id": oid}, priority=0))
        # Recorded in the same commit as the tasks, so a crash leaves either both or neither and
        # the sweep is redone on restart (hard rule 5). Done only when nothing is stale.
        value = model if not stale else f"{model}|{now:.0f}"
        if swept is None:
            session.add(Setting(key=EMBED_SWEPT_MODEL_KEY, value=value))
        else:
            swept.value = value
        await session.commit()
    if added:
        log.info("embed_sweep_enqueued", model=model, count=len(added))
    return len(added)
