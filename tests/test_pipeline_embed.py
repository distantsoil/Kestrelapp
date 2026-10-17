"""The embed task actually drains (AFTERACTION 1.1).

This is the test that would have caught the original defect. `embed` tasks were enqueued at
ingestion from Phase 3 onward and NOTHING ever leased them, so `opportunity_embedding` had no
production writer and five features were silently inert: reranker training, the Tier 1 reranker
gate, Tier 2 few-shot injection, active-learning novelty, and semantic search.

Nothing in the suite noticed, because every test asserted on what the pipeline PRODUCED and none
asserted on what the queue had LEFT BEHIND. A queue that silently fills is invisible to a test that
only looks at outputs, so the assertions here are deliberately about the queue's terminal state as
well as the vectors.

Offline: FakeLLM.embed is deterministic and no model is called.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, OpportunityEmbedding, Source, Task
from kestrel.llm.embeddings import from_bytes
from kestrel.pipeline.embed import embedding_text, handle_embed
from kestrel.pipeline.worker import Worker
from tests.pipeline_fakes import FakeLLM


async def _seed(sm, n: int = 3) -> list[int]:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.flush()
        ids = []
        for i in range(n):
            opp = Opportunity(
                track="employment", opp_type="job_posting", source_id=src.id,
                title=f"VP of IT #{i}", org_name=f"Beacon {i}", stage="new",
                description_md=f"Own IT leadership and vendor accountability. Item {i}.",
            )
            s.add(opp)
            await s.flush()
            ids.append(opp.id)
            s.add(Task(type="embed", payload={"opportunity_id": opp.id}))
        await s.commit()
    return ids


async def _task_states(sm, task_type: str = "embed") -> dict[str, int]:
    async with sm() as s:
        rows = (
            await s.execute(
                select(Task.status, func.count())
                .where(Task.type == task_type)
                .group_by(Task.status)
            )
        ).all()
    return {status: count for status, count in rows}


@pytest.mark.asyncio
async def test_embed_tasks_reach_done_and_vectors_exist(sm):
    """The load-bearing assertion, in both halves.

    A vector for every opportunity AND an empty pending queue. Asserting only the vectors would
    still pass if the handler wrote them but never completed the task; asserting only the queue
    would pass if the handler completed tasks while writing nothing.
    """
    ids = await _seed(sm, n=3)
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)

    from kestrel.pipeline.worker import TierStats

    stats = TierStats()
    await worker.drain_tier("embed", 2, handle_embed, stats)

    assert stats.completed == 3, f"expected 3 embed tasks drained, got {stats.completed}"
    assert stats.errors == 0

    states = await _task_states(sm)
    assert states.get("pending", 0) == 0, f"embed queue did not drain: {states}"
    assert states.get("done", 0) == 3, f"embed tasks did not reach done: {states}"

    async with sm() as s:
        rows = (await s.execute(select(OpportunityEmbedding))).scalars().all()
        stored = {r.opportunity_id: r for r in rows}
    assert set(stored) == set(ids), "not every opportunity got a vector"
    for row in stored.values():
        assert row.dims > 0
        assert row.vector, "vector blob is empty, which reads as 'no embedding' everywhere"
        assert len(from_bytes(row.vector)) == row.dims
        assert row.model == settings.llm.tiers["embed"].model


@pytest.mark.asyncio
async def test_a_full_worker_run_leaves_no_pending_embed_backlog(sm):
    """The end-to-end shape of the original bug: ingest enqueues, the worker runs, and the backlog
    grows forever. Worker.run() must drain embed as part of a normal pass."""
    await _seed(sm, n=2)
    settings = get_settings()
    worker = Worker(sm, FakeLLM(settings), settings)

    stats = await worker.run(track="employment")

    assert stats.embed.completed == 2, f"worker.run did not drain embed: {stats.embed}"
    states = await _task_states(sm)
    assert states.get("pending", 0) == 0, f"pending embed backlog after a full run: {states}"


@pytest.mark.asyncio
async def test_embedding_is_idempotent_on_resume(sm):
    """Hard rule 5: a task re-leased after a crash must not recompute. Re-running the handler on an
    already-embedded opportunity is a skip, and the stored bytes do not change."""
    ids = await _seed(sm, n=1)
    settings = get_settings()
    client = FakeLLM(settings)

    first = await handle_embed(sm, client, settings, opportunity_id=ids[0])
    assert first.action == "embedded"

    async with sm() as s:
        before = (await s.get(OpportunityEmbedding, ids[0])).vector

    second = await handle_embed(sm, client, settings, opportunity_id=ids[0])
    assert second.action == "skip", "re-embedded an opportunity that already had a vector"

    async with sm() as s:
        after = (await s.get(OpportunityEmbedding, ids[0])).vector
    assert before == after


@pytest.mark.asyncio
async def test_a_model_change_forces_recomputation(sm):
    """Vectors from two different models are not comparable, so mixing them would quietly corrupt
    every cosine in the system. A stored row from another model must be replaced, not kept."""
    ids = await _seed(sm, n=1)
    settings = get_settings()
    client = FakeLLM(settings)
    await handle_embed(sm, client, settings, opportunity_id=ids[0])

    async with sm() as s:
        row = await s.get(OpportunityEmbedding, ids[0])
        row.model = "some-other-embedder"
        row.vector = b"\x00" * 32
        await s.commit()

    outcome = await handle_embed(sm, client, settings, opportunity_id=ids[0])
    assert outcome.action == "embedded"
    async with sm() as s:
        row = await s.get(OpportunityEmbedding, ids[0])
    assert row.model == settings.llm.tiers["embed"].model
    assert row.vector != b"\x00" * 32


@pytest.mark.asyncio
async def test_a_missing_opportunity_is_a_skip_not_an_error(sm):
    # An error would make the queue retry forever on a row that will never exist.
    settings = get_settings()
    outcome = await handle_embed(sm, FakeLLM(settings), settings, opportunity_id=999999)
    assert outcome.action == "skip"


@pytest.mark.asyncio
async def test_an_opportunity_with_no_text_is_a_skip(sm):
    """Completes rather than erroring, so the queue cannot fill with permanently unprocessable
    rows, which is the failure mode this whole item is about."""
    async with sm() as s:
        opp = Opportunity(track="employment", opp_type="job_posting", stage="new")
        s.add(opp)
        await s.commit()
        opp_id = opp.id
    settings = get_settings()
    outcome = await handle_embed(sm, FakeLLM(settings), settings, opportunity_id=opp_id)
    assert outcome.action == "skip"


@pytest.mark.asyncio
async def test_a_backend_failure_is_retryable_not_a_silent_success(sm):
    """A transient Ollama failure must leave the task retryable. Reporting success would drop the
    opportunity out of the index with nothing to show it ever failed."""
    ids = await _seed(sm, n=1)
    settings = get_settings()

    class _Broken(FakeLLM):
        async def embed(self, texts, *, tier="embed", timeout=60.0):
            raise RuntimeError("ollama down")

    outcome = await handle_embed(sm, _Broken(settings), settings, opportunity_id=ids[0])
    assert outcome.action == "error"
    assert "ollama down" in (outcome.error or "")
    async with sm() as s:
        assert await s.get(OpportunityEmbedding, ids[0]) is None


def test_embedding_text_leads_with_title_and_org():
    """Few-shot retrieval ranks by this vector, so the identifying fields must lead rather than be
    buried under a long description."""
    from kestrel.pipeline.embed import DOCUMENT_PREFIX

    opp = Opportunity(
        track="employment", opp_type="job_posting", title="VP of IT",
        org_name="Beacon Health", description_md="x" * 50_000,
    )
    text = embedding_text(opp)
    assert text.startswith(f"{DOCUMENT_PREFIX}VP of IT\nBeacon Health")


def test_embedding_text_is_truncated_to_the_stated_budget():
    """Asserts against the CONSTANT, not against a length that any budget below 6250 tokens would
    satisfy. The point of the budget is that a long JD cannot dominate the vector, so the test has
    to notice the budget drifting upward."""
    from kestrel.ingest.tokens import count_tokens
    from kestrel.pipeline.embed import _EMBED_TOKEN_BUDGET

    opp = Opportunity(
        track="employment", opp_type="job_posting", title="VP of IT",
        org_name="Beacon Health", description_md="lorem ipsum dolor sit amet " * 5_000,
    )
    tokens = count_tokens(embedding_text(opp))
    assert tokens <= _EMBED_TOKEN_BUDGET + 50, f"{tokens} tokens exceeds the stated budget"
    assert _EMBED_TOKEN_BUDGET < 4000, "the budget drifted close to the model's window"


def test_embedding_text_survives_missing_fields():
    from kestrel.pipeline.embed import DOCUMENT_PREFIX

    opp = Opportunity(track="employment", opp_type="job_posting", title="VP of IT")
    assert embedding_text(opp) == f"{DOCUMENT_PREFIX}VP of IT"
    blank = Opportunity(track="employment", opp_type="job_posting")
    # No prefix on empty text: the handler treats "" as nothing to embed, and a bare prefix would
    # be a non-empty string that embeds to a meaningless constant vector for every blank row.
    assert embedding_text(blank) == ""


def test_the_corpus_and_query_prefixes_are_the_matched_nomic_pair():
    """nomic-embed-text is prefix conditioned. The corpus uses search_document: and the only query
    path (intelligence/search.py) must use search_query:, or ranking degrades quietly."""
    from kestrel.intelligence.search import QUERY_PREFIX
    from kestrel.pipeline.embed import DOCUMENT_PREFIX

    assert DOCUMENT_PREFIX == "search_document: "
    assert QUERY_PREFIX == "search_query: "


@pytest.mark.asyncio
async def test_a_changed_description_re_embeds(sm):
    """THE defect a fresh reviewer caught in the first cut of this handler.

    Re-seen postings enqueue a fresh embed task on both reseen_changed and reseen_minor precisely
    so the vector is refreshed. A guard keyed only on "a vector exists" discarded every one of
    them, so a rewritten JD kept its original vector forever while the task completed `done`. That
    is worse than the empty index it replaced: few-shot, the reranker gate, and search would all
    rank against text that no longer exists, and the queue would look perfectly clean.
    """
    ids = await _seed(sm, n=1)
    settings = get_settings()
    client = FakeLLM(settings)
    await handle_embed(sm, client, settings, opportunity_id=ids[0])

    async with sm() as s:
        before = (await s.get(OpportunityEmbedding, ids[0])).vector

    async with sm() as s:
        opp = await s.get(Opportunity, ids[0])
        opp.description_md = "Completely different role: hands-on platform engineering, no vendors."
        await s.commit()

    outcome = await handle_embed(sm, client, settings, opportunity_id=ids[0])
    assert outcome.action == "embedded", "a changed description did not re-embed"

    async with sm() as s:
        row = await s.get(OpportunityEmbedding, ids[0])
    assert row.vector != before, "the vector is stale after the description changed"
    assert row.text_hash is not None


@pytest.mark.asyncio
async def test_a_legacy_row_without_a_text_hash_re_embeds(sm):
    """Rows written before migration 0007 recorded nothing about what they were computed from, so
    trusting them would be trusting an unknown."""
    ids = await _seed(sm, n=1)
    settings = get_settings()
    client = FakeLLM(settings)
    await handle_embed(sm, client, settings, opportunity_id=ids[0])

    async with sm() as s:
        row = await s.get(OpportunityEmbedding, ids[0])
        row.text_hash = None
        await s.commit()

    assert (await handle_embed(sm, client, settings, opportunity_id=ids[0])).action == "embedded"


@pytest.mark.asyncio
async def test_a_concurrent_duplicate_write_is_a_skip_not_an_error(sm):
    """opportunity_id is the primary key, and ingestion adds embed tasks directly rather than via
    enqueue_once, so two live tasks for one opportunity is normal after a re-ingest. With
    concurrency 4 the write race is the expected case. It must not surface as an error, inflate the
    run's error count, or persist an IntegrityError string on a task that in fact succeeded."""
    ids = await _seed(sm, n=1)
    settings = get_settings()

    async with sm() as s:
        s.add(Task(type="embed", payload={"opportunity_id": ids[0]}))
        await s.commit()

    worker = Worker(sm, FakeLLM(settings), settings)
    from kestrel.pipeline.worker import TierStats

    stats = TierStats()
    await worker.drain_tier("embed", 4, handle_embed, stats)

    assert stats.errors == 0, "a duplicate embed task surfaced as an error"
    states = await _task_states(sm)
    assert states.get("pending", 0) == 0
    assert states.get("dead", 0) == 0


def test_every_production_drain_site_drains_embed():
    """The original defect was a MISSING DRAIN LINE, and worker.run is not the path the packaged
    app uses. Removing the drain from the desktop worker loop or the CLI must not leave the suite
    green, so all three sites are asserted, not just the one with an integration test."""
    import inspect

    from kestrel import cli
    from kestrel.pipeline import worker as worker_mod
    from kestrel.web import app as app_mod

    for label, src in (
        ("worker.run", inspect.getsource(worker_mod.Worker._run_tiers)),
        ("web serve loop", inspect.getsource(app_mod._worker_loop)),
        ("cli drain", inspect.getsource(cli)),
    ):
        # Whitespace-normalized: the call is wrapped across lines in worker.py, and this test is
        # about the drain existing, not about how it happens to be formatted.
        # Worker.drain_embed is the sweep-then-drain wrapper (D-EMBED-4); it must itself drain.
        flat = " ".join(src.split())
        assert "drain_embed(" in flat, f"{label} does not drain the embed queue"
    wrapper = " ".join(inspect.getsource(worker_mod.Worker.drain_embed).split())
    assert 'drain_tier("embed"' in wrapper and "enqueue_stale_embeddings(" in wrapper
