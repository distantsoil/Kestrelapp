"""P9 mock interview: the critique flags a claim not present in the operator's proof points or
canonical stories, that flag is preserved on the transcript, and the proof points actually reach the
prompt so the model can check against them. Fully offline: the LLM is a capturing fake."""

from __future__ import annotations

import json

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import MockSession, Opportunity, Org, Quarantine, Source, Story
from kestrel.interview.mock import end_session, start_session, submit_answer
from kestrel.llm.client import ChatResult

_P9 = {
    "scores": {"structure": 4, "specificity": 2, "relevance": 4, "length": 3, "ownership": 5},
    "worked_well": "Clear arc.",
    "improvements": ["Add concrete numbers."],
    # The candidate claimed a metric that is NOT in their proof points/stories:
    "unsupported_claims": ["cut costs by 40 percent"],
    "revised_opening": "When I joined, the platform was failing...",
    "next_question": "Tell me about a conflict you navigated.",
    "next_question_rationale": "probe difficult_stakeholder",
}

_PROOF_POINTS = [
    {"claim": "Rescued the payments platform", "evidence": "postmortem", "metric": None}
]


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


class _CapturingClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sent: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        self.sent = messages
        return ChatResult(
            content=json.dumps(_P9),
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


async def test_mock_flags_unsupported_claim_and_passes_proof_points(sm):
    opp_id = await _seed_opp(sm)
    async with sm() as s:
        story = Story(title="Payments rescue", result="Error rate to 0.1 percent",
                      is_canonical=True)
        s.add(story)
        await s.commit()
        ms = await start_session(s, opportunity_id=opp_id, mode="behavioral")
        ms_id = ms.id

    # The pending first question is persisted with a null answer.
    async with sm() as s:
        ms = await s.get(MockSession, ms_id)
        assert len(ms.transcript) == 1
        assert ms.transcript[0]["answer"] is None

    client = _CapturingClient()
    async with sm() as s:
        stories = [(await s.get(Story, story.id))]
        turn = await submit_answer(
            s, client, ms_id, "I cut costs by 40 percent.",
            proof_points=_PROOF_POINTS, stories=stories,
        )

    # The unsupported claim is surfaced and preserved on the turn.
    assert turn is not None
    assert turn["unsupported_claims"] == ["cut costs by 40 percent"]
    assert turn["scores"]["ownership"] == 5

    # The proof point and both the question and answer reached the model, so the check is real.
    user_msg = next(m["content"] for m in client.sent if m["role"] == "user")
    assert "Rescued the payments platform" in user_msg
    assert "I cut costs by 40 percent." in user_msg

    async with sm() as s:
        ms = await s.get(MockSession, ms_id)
    assert ms.question_count == 1
    assert ms.transcript[0]["unsupported_claims"] == ["cut costs by 40 percent"]
    assert ms.transcript[1]["answer"] is None  # next question pending
    assert ms.transcript[1]["question"] == "Tell me about a conflict you navigated."
    assert ms.mean_scores["ownership"] == 5.0

    async with sm() as s:
        ended = await end_session(s, ms_id)
    assert ended.ended_at is not None
    assert len(ended.transcript) == 1  # trailing unanswered question dropped


class _BadJsonClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        return ChatResult(content="not valid json {", prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, model=self.settings.llm.tiers[tier].model)


async def test_mock_failure_quarantines_raw_response(sm):
    opp_id = await _seed_opp(sm)
    async with sm() as s:
        ms = await start_session(s, opportunity_id=opp_id, mode="behavioral")
        ms_id = ms.id
    async with sm() as s:
        turn = await submit_answer(s, _BadJsonClient(), ms_id, "some answer",
                                   proof_points=_PROOF_POINTS, stories=[])
    assert turn is None  # a failed critique does not fabricate a turn
    async with sm() as s:
        q = (await s.execute(select(Quarantine))).scalars().all()
        ms = await s.get(MockSession, ms_id)
    assert len(q) == 1  # hard rule 3: the raw response is preserved, not silently dropped
    assert q[0].raw_response == "not valid json {"
    assert ms.transcript[0]["answer"] is None  # the pending question is untouched, still answerable
