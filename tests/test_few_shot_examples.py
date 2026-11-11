"""Few-shot examples say what was rated (PROMPTS.md FEW_SHOT_EXAMPLES, D-LEARN-10).

A Quick answer has no reasons, so an example built only from the label's rationale reached the
Tier 2 prompt as "(opportunity summary unavailable)" with a bare score. Each example now carries a
one-line summary of its own role, and only the newest answer per role is used. Invented roles;
nothing calls a model."""

from __future__ import annotations

import numpy as np
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Label, Opportunity, OpportunityEmbedding
from kestrel.llm.embeddings import current_model
from kestrel.pipeline.context import few_shot_block
from kestrel.pipeline.tier2 import _gather_few_shot


async def _seed(sm, n: int) -> list[int]:
    settings = get_settings()
    model = current_model(settings)
    ids = []
    async with sm() as s:
        for i in range(n):
            o = Opportunity(track="employment", opp_type="job_posting",
                            title=f"Invented Planner {i}", org_name=f"Quillfeather {i}",
                            stage="t2_done")
            s.add(o)
            await s.flush()
            vec = np.zeros(8, dtype=np.float32)
            vec[i % 8] = 1.0
            vec[0] += 0.5
            s.add(OpportunityEmbedding(opportunity_id=o.id, model=model, dims=8,
                                       vector=vec.tobytes()))
            ids.append(o.id)
        await s.commit()
    return ids


async def test_quick_answers_reach_the_prompt_with_the_roles_they_rated(sm):
    ids = await _seed(sm, 17)
    async with sm() as s:
        for oid in ids[1:]:  # Quick answers: a score, no reasons
            s.add(Label(opportunity_id=oid, track="employment", verdict="apply", score=4.0))
        await s.commit()
        query = await s.get(Opportunity, ids[0])
        chosen, _ids = await _gather_few_shot(s, query, get_settings())
    assert chosen
    block = few_shot_block(chosen)
    assert "summary unavailable" not in block
    for ex in chosen:
        n = ex.summary.removeprefix("Invented Planner ").split()[0]
        assert ex.summary == f"Invented Planner {n} at Quillfeather {n}"
        assert ex.summary in block


async def test_only_the_newest_answer_per_role_is_an_example(sm):
    ids = await _seed(sm, 17)
    async with sm() as s:
        for oid in ids[1:]:
            s.add(Label(opportunity_id=oid, track="employment", verdict="apply", score=4.0))
        await s.commit()
        for oid in ids[1:]:  # changed later under Detailed: the opposite answer
            s.add(Label(opportunity_id=oid, track="employment", verdict="pass", score=1.0,
                        rationale="Invented reason: the role is on site five days."))
        await s.commit()
        query = await s.get(Opportunity, ids[0])
        chosen, chosen_ids = await _gather_few_shot(s, query, get_settings())
        assert len(chosen_ids) == len(set(chosen_ids))
        assert {ex.verdict for ex in chosen} == {"pass"}
        assert len((await s.execute(select(Label))).scalars().all()) == 32
