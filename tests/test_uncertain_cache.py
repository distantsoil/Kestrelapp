"""The cached active-learning order (issue 171 review, D-LEARN-8): the strip and Calibrate read the
same order `active.rank_uncertain` gives, computed once until something it reads changes.
Cloud-safe: a throwaway database of fictional roles, no model, no network."""

from __future__ import annotations

import pytest

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Label, Opportunity, OpportunityEmbedding
from kestrel.learning import active, uncertain_cache
from kestrel.llm.embeddings import current_model
from tests import quick_fixtures as qf

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def calls(monkeypatch):
    seen: list[int] = []
    real = active.rank_uncertain

    async def spy(*a, **kw):
        seen.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(uncertain_cache, "rank_uncertain", spy)
    uncertain_cache.clear()
    return seen


def _pair(limit: int):
    """(cached order, direct order) as opportunity id, evaluation id, uncertainty, components."""
    settings = get_settings()

    async def go(s):
        got = await uncertain_cache.ranked(s, "employment", settings, limit=limit)
        want = await active.rank_uncertain(s, "employment", settings, limit=limit)
        key = [(u.opportunity.id, u.evaluation.id, u.uncertainty, u.components) for u in got]
        ref = [(u.opportunity.id, u.evaluation.id, u.uncertainty, u.components) for u in want]
        return key, ref

    return qf.run(go)


def test_the_cached_order_is_the_active_learning_order_and_is_ranked_once(calls):
    qf.seed(14, n_labeled=2)
    for limit in (10, 3, 50, 10):
        got, want = _pair(limit)
        assert got == want and len(got) == min(limit, 12)
    assert len(calls) == 1  # the later reads were served from the cache


def _change(fn) -> None:
    async def go(s):
        await fn(s)
        await s.commit()

    qf.run(go)


@pytest.mark.parametrize("what", ["label", "evaluation", "embedding", "archive", "unlabel"])
def test_a_change_to_what_the_order_reads_ranks_again(calls, what):
    ids = qf.seed(12, n_labeled=1)
    got, want = _pair(10)
    assert got == want and len(calls) == 1

    async def label(s):
        s.add(
            Label(
                opportunity_id=ids[5],
                track="employment",
                score=2.0,
                source="quick",
                created_at="2026-11-01T00:00:00+00:00",
            )
        )

    async def evaluation(s):
        s.add(
            Evaluation(
                opportunity_id=ids[7],
                tier=2,
                track="employment",
                verdict="consider",
                score=4.0,
                weighted_score=2.0,
                blocks={},
                dimensions={},
            )
        )

    async def embedding(s):
        import numpy as np

        v = np.ones(8, dtype=np.float32)
        s.add(
            OpportunityEmbedding(
                opportunity_id=ids[3],
                model=current_model(get_settings()),
                vector=v.tobytes(),
                dims=8,
                text_hash="h",
            )
        )

    async def archive(s):
        (await s.get(Opportunity, ids[4])).archived_at = "2026-11-02T00:00:00+00:00"

    async def unlabel(s):
        from sqlalchemy import delete

        await s.execute(delete(Label))

    _change(locals()[what])
    got, want = _pair(10)
    assert got == want
    assert len(calls) == 2


# ------------------------------------------- changes that cancel out in counts (D-LEARN-10)
def test_hiding_one_role_and_putting_back_another_misses_the_cache(calls):
    ids = qf.seed(12, n_labeled=1)

    async def hide(s):
        (await s.get(Opportunity, ids[7])).archived_at = "2026-11-01T00:00:00+00:00"

    _change(hide)
    got, want = _pair(50)
    assert got == want

    async def swap(s):
        (await s.get(Opportunity, ids[7])).archived_at = None  # Put back
        (await s.get(Opportunity, ids[5])).archived_at = "2026-11-02T00:00:00+00:00"  # Hide

    _change(swap)
    got, want = _pair(50)
    assert got == want
    assert ids[5] not in [g[0] for g in got] and ids[7] in [g[0] for g in got]


def test_a_reused_label_id_on_another_role_misses_the_cache(calls):
    ids = qf.seed(12, n_labeled=1)
    box: dict[str, int] = {}

    async def answer(s):
        row = Label(opportunity_id=ids[5], track="employment", score=4.0, source="quick",
                    created_at="2026-11-01T00:00:00+00:00")
        s.add(row)
        await s.flush()
        box["first"] = row.id

    _change(answer)
    assert _pair(50)[0] == _pair(50)[1]

    async def undo_then_other(s):
        await s.delete(await s.get(Label, box["first"]))
        await s.flush()
        row = Label(opportunity_id=ids[6], track="employment", score=4.0, source="quick",
                    created_at="2026-11-01T00:01:00+00:00")
        s.add(row)
        await s.flush()
        box["second"] = row.id

    _change(undo_then_other)
    assert box["second"] == box["first"]  # SQLite gave the id back: counts alone cannot tell
    got, want = _pair(50)
    assert got == want


def test_moving_a_role_between_duplicate_groups_misses_the_cache(calls):
    ids = qf.seed(12, n_labeled=1)

    async def group(s):
        for i, oid in enumerate(ids[2:6]):
            o = await s.get(Opportunity, oid)
            o.dedup_group_id = "g-a" if i < 2 else "g-b"
            o.is_canonical = i in (0, 2)

    _change(group)
    _pair(50)
    n = len(calls)

    async def move(s):
        (await s.get(Opportunity, ids[3])).dedup_group_id = "g-b"

    _change(move)
    got, want = _pair(50)
    assert got == want and len(calls) == n + 1
