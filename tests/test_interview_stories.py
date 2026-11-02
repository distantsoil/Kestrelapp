"""P8 story consolidation: a cluster of sketches of the same event merges into one canonical story
with its sources attached, and an operator-edited story is NEVER overwritten by a later merge — new
sketches attach to it as usage instead. Fully offline: the LLM (embeddings + P8) is a fake, and
clustering is driven by deterministic keyword-keyed vectors so the merges are exactly controlled."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Org, Source, Story, StoryUsage
from kestrel.interview.stories import consolidate_stories
from kestrel.llm.client import ChatResult

# One P8 merge result, reused for every cluster; the pipeline (not the model) decides how many
# stories are created and what is attached where, which is what these tests exercise.
_P8 = {
    "title": "Turnaround of the payments platform",
    "situation": "The platform was failing under peak load.",
    "task": "Restore reliability without a rewrite.",
    "action": "I triaged, re-architected the hot path, and shipped in stages.",
    "result": "Error rate fell sharply.",
    "reflection": "I would have invested in load testing far sooner.",
    "themes": ["turnaround", "technical_judgment"],
    "uncertain_details": [],
    "spoken_seconds_estimate": 105,
    "source_sketch_ids": [],
}


class _FakeClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        if "delta" in t:
            return [1.0, 0.0, 0.0]
        if "omega" in t:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed(self, texts, *, tier: str = "embed", timeout: float = 60.0):
        return [self._vec(t) for t in texts]

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        return ChatResult(
            content=json.dumps(_P8),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


def _sketch(question: str, keyword: str) -> dict:
    return {
        "likely_question": question,
        "why_asked": "probes ownership",
        "proof_point_key": None,
        "star_r": {
            "situation": f"The {keyword} platform was in trouble.",
            "task": f"Fix {keyword}.",
            "action": f"I led the {keyword} turnaround.",
            "result": f"{keyword} recovered.",
            "reflection": "Test earlier.",
        },
    }


async def _seed_opp(sm) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP IT", org_name="Acme", url="https://x/1",
        )
        s.add(opp)
        await s.commit()
        return opp.id


async def _add_eval(sm, opp_id: int, sketches: list[dict]) -> None:
    async with sm() as s:
        s.add(
            Evaluation(
                opportunity_id=opp_id, tier=2, track="employment", verdict="apply",
                verdict_model="apply", score=4.0, weighted_score=4.0, needs_review=False,
                dimensions={}, blocks={"f_interview": sketches}, blockers=[],
                legitimacy={"status": "clean", "signals": []},
            )
        )
        await s.commit()


async def test_two_events_merge_into_two_canonical_stories(sm):
    opp = await _seed_opp(sm)
    # Two sketches of the "delta" event (must merge) and one of the "omega" event (separate).
    await _add_eval(sm, opp, [_sketch("Tell me about a turnaround.", "delta")])
    await _add_eval(sm, opp, [_sketch("Describe rescuing a system.", "delta")])
    await _add_eval(sm, opp, [_sketch("A hard technical call?", "omega")])

    async with sm() as s:
        result = await consolidate_stories(s, _FakeClient(), get_settings())

    assert result.sketches == 3
    assert result.clusters == 2  # delta x2 merged, omega x1 on its own
    assert len(result.created_story_ids) == 2
    assert result.attached == []

    async with sm() as s:
        stories = (await s.execute(select(Story))).scalars().all()
        usage = (await s.execute(select(func.count()).select_from(StoryUsage))).scalar_one()
    assert len(stories) == 2
    assert all(st.is_canonical and not st.operator_edited for st in stories)
    assert {st.use_count for st in stories} == {2, 1}  # the merged story carries both sources
    assert usage == 3  # every source sketch is recorded as usage
    assert all("turnaround" in (st.themes or []) for st in stories)


async def test_operator_edited_story_is_never_overwritten(sm):
    opp = await _seed_opp(sm)
    async with sm() as s:
        s.add(
            Story(
                title="My delta rescue, in my words",
                situation="The delta platform nearly took us down and I owned the fix.",
                task="Keep us alive.", action="I did the hard part myself.",
                result="We survived.", reflection="My own lesson.",
                themes=["turnaround"], is_canonical=True, operator_edited=True, use_count=1,
            )
        )
        await s.commit()

    # New sketches of the same (delta) event arrive.
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    await _add_eval(sm, opp, [_sketch("Rescue a system?", "delta")])

    async with sm() as s:
        result = await consolidate_stories(s, _FakeClient(), get_settings())

    # The cluster matched the operator-edited story: it is attached as usage, never re-synthesized.
    assert result.created_story_ids == []
    assert len(result.attached) == 1
    assert result.attached[0]["operator_edited"] is True
    assert result.attached[0]["sketches"] == 2

    async with sm() as s:
        stories = (await s.execute(select(Story))).scalars().all()
        edited = next(st for st in stories if st.operator_edited)
        usage = (await s.execute(select(func.count()).select_from(StoryUsage))).scalar_one()
    assert len(stories) == 1  # no new story was created
    assert edited.title == "My delta rescue, in my words"  # text untouched, byte for byte
    assert edited.situation.startswith("The delta platform nearly took us down")
    assert edited.use_count == 3  # 1 original + 2 newly attached
    assert usage == 2


async def test_consolidation_is_resumable_via_watermark(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])
    async with sm() as s:
        first = await consolidate_stories(s, _FakeClient(), get_settings())
    # A second run with no new sketches does nothing (the watermark advanced past them).
    async with sm() as s:
        second = await consolidate_stories(s, _FakeClient(), get_settings())
    assert first.sketches == 1
    assert second.sketches == 0
    assert second.created_story_ids == []


class _MixedClient(_FakeClient):
    """Succeeds for the delta cluster, returns un-parseable output for the omega cluster (so P8
    quarantines). Lets a test prove a failed merge neither loses its sketches nor duplicates the
    ones that succeeded alongside it."""

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if "omega" in user.lower():
            return ChatResult(content="not json {", prompt_tokens=1, completion_tokens=1,
                              latency_ms=1, model=self.settings.llm.tiers[tier].model)
        return await super().complete(tier=tier, messages=messages, schema=schema,
                                      schema_name=schema_name)


async def test_failed_merge_neither_loses_sketches_nor_duplicates_successes(sm):
    opp = await _seed_opp(sm)
    await _add_eval(sm, opp, [_sketch("Turnaround?", "delta")])  # will succeed
    await _add_eval(sm, opp, [_sketch("Hard call?", "omega")])  # will fail (quarantine)

    async with sm() as s:
        first = await consolidate_stories(s, _MixedClient(), get_settings())
    assert len(first.created_story_ids) == 1  # only delta merged

    # The failed omega cluster held the watermark, so a second run re-collects omega — but NOT the
    # already-consolidated delta sketch (it was recorded consumed). No duplicate delta story.
    async with sm() as s:
        second = await consolidate_stories(s, _MixedClient(), get_settings())
        stories_after_two = (await s.execute(select(Story))).scalars().all()
    assert second.sketches == 1  # only omega came back
    assert second.created_story_ids == []  # still failing
    assert len(stories_after_two) == 1  # delta was not re-created

    # Once the model recovers, omega finally consolidates and the run fully advances.
    async with sm() as s:
        third = await consolidate_stories(s, _FakeClient(), get_settings())
        stories_after_three = (await s.execute(select(Story))).scalars().all()
    assert len(third.created_story_ids) == 1
    assert len(stories_after_three) == 2  # delta + omega, each exactly once
