"""A local Tier 2 prompt that cannot fit its window is never sent (D-SCORE-20).

INVARIANT: a Tier 2 prompt larger than that tier's context budget is never sent. The role is kept
for the person with the reason (hard rule 4), never dropped and never scored from a prompt the
model server would cut silently. Invented text only; the model is faked and nothing reaches a
network."""

from __future__ import annotations

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Quarantine
from kestrel.pipeline import tier2
from kestrel.pipeline.profile import load_profile
from tests.pipeline_fakes import FakeLLM

_BODY = "Plan the tide schedules, keep the ledger, and run the harbor crane rota. " * 8


class _Capture(FakeLLM):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[list[dict]] = []

    async def complete(self, **kw):
        self.sent.append(kw["messages"])
        return await super().complete(**kw)


async def _role(s, *, scored: bool) -> int:
    o = Opportunity(
        track="employment", opp_type="job_posting", title="Planner", org_name="Quillfeather Co",
        stage="t2_done" if scored else "t1_pass", stage_reason="invented",
        description_md=_BODY, seen_count=1, ghost_score=0.0,
    )
    s.add(o)
    await s.flush()
    if scored:
        s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", score=3.5,
                         verdict="consider", blocks={}, prompt_version="evaluate/v3"))
    await s.commit()
    return o.id


def _profiles() -> dict:
    return {t: load_profile(t) for t in ("employment", "consulting")}


async def test_a_prompt_that_cannot_fit_is_not_sent_and_the_role_is_kept(sm, monkeypatch):
    async with sm() as s:
        a = await _role(s, scored=False)
    # A margin this large leaves no room at all: the prompt is over the window even with the
    # description cut to one token.
    monkeypatch.setattr(tier2, "TOKENIZER_MARGIN", 0.99)
    settings = get_settings()
    llm = _Capture(settings)

    out = await tier2.handle_t2(sm, llm, settings, opportunity_id=a, profiles=_profiles())

    assert llm.sent == []
    assert (out.action, out.quarantined, out.error) == (
        "quarantined", True, tier2.PROMPT_OVER_BUDGET)
    async with sm() as s:
        opp = await s.get(Opportunity, a)
        rows = (await s.execute(select(Quarantine))).scalars().all()
        evs = (await s.execute(select(Evaluation))).scalars().all()
    assert opp.stage == "quarantined"  # kept where the person sees it, with the reason
    assert tier2.PROMPT_OVER_BUDGET in opp.stage_reason
    assert [(q.opportunity_id, q.tier, q.error, q.attempts, q.raw_response) for q in rows] == [
        (a, 2, tier2.PROMPT_OVER_BUDGET, 0, None)
    ]
    assert evs == []


async def test_a_score_again_that_cannot_fit_keeps_the_earlier_score(sm, monkeypatch):
    async with sm() as s:
        a = await _role(s, scored=True)
    monkeypatch.setattr(tier2, "TOKENIZER_MARGIN", 0.99)
    settings = get_settings()
    llm = _Capture(settings)

    out = await tier2.handle_t2(sm, llm, settings, opportunity_id=a, rescore=True,
                                profiles=_profiles())

    assert llm.sent == []
    assert out.action == "quarantined"
    async with sm() as s:
        opp = await s.get(Opportunity, a)
        rows = (await s.execute(select(Quarantine))).scalars().all()
        evs = (await s.execute(select(Evaluation))).scalars().all()
    assert (opp.stage, opp.stage_reason) == ("t2_done", "invented")
    assert len(evs) == 1 and len(rows) == 1
    assert rows[0].error.endswith(tier2.PROMPT_OVER_BUDGET)


async def test_every_prompt_that_is_sent_fits_the_window(sm, monkeypatch):
    """The other side: with room for the posting once it is trimmed, the call goes out and what
    goes out is within the budget."""
    async with sm() as s:
        a = await _role(s, scored=False)
        opp = await s.get(Opportunity, a)
        opp.description_md = "Invented duty about the harbor rota, line after line. " * 900
        await s.commit()
    monkeypatch.setattr(tier2, "TOKENIZER_MARGIN", 0.5)
    settings = get_settings()
    llm = _Capture(settings)

    out = await tier2.handle_t2(sm, llm, settings, opportunity_id=a, profiles=_profiles())

    assert out.action == "t2_done", out
    assert len(llm.sent) == 1
    assert tier2._local_overflow(settings, "tier2", llm.sent[0]) == 0
