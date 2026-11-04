"""#138: Calibrate's uncertain list shows one role per duplicate group, picks it the same way every
time (the canonical copy, else the lowest id), orders ties by id, and reads the roles in a
constant number of queries rather than one per candidate."""

from __future__ import annotations

from sqlalchemy import event

from kestrel.config import get_settings
from kestrel.db.models import Evaluation, Opportunity, Org, Source
from kestrel.learning.active import rank_uncertain


async def _seed(s, specs):
    """specs: (name, dedup_group_id, is_canonical, score). Returns {name: opportunity id}."""
    org = Org(name="Acme", normalized_name="acme")
    src = Source(track="employment", kind="greenhouse", name="s", enabled=True)
    s.add_all([org, src])
    await s.flush()
    ids: dict[str, int] = {}
    for name, group, canonical, score in specs:
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="Operations Analyst", description_md="Own reporting.", stage="t2_done",
            first_seen_at="2026-07-20T00:00:00.000000+00:00",
            dedup_group_id=group, is_canonical=canonical,
        )
        s.add(opp)
        await s.flush()
        ids[name] = opp.id
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                         verdict="consider", score=score, weighted_score=score))
    await s.commit()
    return ids


async def test_one_copy_per_group_canonical_first_and_stable_order(sm):
    specs = [
        ("a1", "g-a", False, 3.9), ("a2", "g-a", True, 3.9), ("a3", "g-a", False, 3.9),
        ("b1", "g-b", False, 3.9), ("b2", "g-b", False, 3.9),
        ("solo1", None, True, 3.9), ("solo2", None, True, 3.9),
    ]
    async with sm() as s:
        ids = await _seed(s, specs)
    runs = []
    for _ in range(2):
        async with sm() as s:
            ranked = await rank_uncertain(s, "employment", get_settings(), limit=10)
        runs.append([u.opportunity.id for u in ranked])
    # a: the canonical copy; b: no canonical, so the lowest id; solos stay. Equal scores and no
    # embeddings means equal uncertainty, so the order is by id.
    expected = sorted([ids["a2"], ids["b1"], ids["solo1"], ids["solo2"]])
    assert runs[0] == expected
    assert runs[1] == expected


async def test_ranking_reads_roles_in_a_constant_number_of_queries(sm):
    specs = [(f"r{i}", None, True, 3.9) for i in range(25)]
    async with sm() as s:
        await _seed(s, specs)
    statements: list[str] = []
    async with sm() as s:
        engine = s.bind.sync_engine

        def count(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            ranked = await rank_uncertain(s, "employment", get_settings(), limit=50)
        finally:
            event.remove(engine, "before_cursor_execute", count)
    assert len(ranked) == 25
    assert len(statements) < 10, f"{len(statements)} queries for 25 candidates"
