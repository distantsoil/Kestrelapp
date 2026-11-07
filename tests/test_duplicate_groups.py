"""#122 (D-LRN-1): one role posted on several boards is one judgment. A duplicate group is labeled
once any copy is rated or overridden, Calibrate never asks about another copy, the sidebar badge
counts the groups it can ask about, and calibration and training count each group once (the most
recent judgment), with sample sizes that count distinct groups (hard rule 6). A role with no group
is its own group. Fictional roles; no model and no network."""

from __future__ import annotations

import asyncio
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Source,
    VerdictOverride,
)
from kestrel.learning.active import rank_uncertain
from kestrel.learning.capture import labeled_examples
from kestrel.llm.embeddings import to_bytes
from kestrel.web import calibrate_view as cv
from kestrel.web.routes_control import _nav_counts

DIMS = ["role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"]
TRACK = "employment"

# (name, dedup_group_id, is_canonical). Two copies of one posting, three of another, two roles
# that ingest never grouped.
SPECS = [
    ("a1", "grp-a", True), ("a2", "grp-a", False),
    ("b1", "grp-b", False), ("b2", "grp-b", True), ("b3", "grp-b", False),
    ("s1", None, True), ("s2", None, True),
]


def _fresh_db() -> None:
    async def prepare():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()

    asyncio.run(prepare())
    forget_engine()


def _run(fn):
    async def go():
        engine = make_async_engine(paths.db_path())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    out = asyncio.run(go())
    forget_engine()
    return out


def _seed(specs=SPECS) -> dict[str, int]:
    _fresh_db()

    async def fill(s):
        org = Org(name="Acme Freight", normalized_name="acme freight")
        src = Source(track=TRACK, kind="greenhouse", name="b", enabled=True)
        s.add_all([org, src])
        await s.flush()
        ids: dict[str, int] = {}
        for i, (name, group, canonical) in enumerate(specs):
            opp = Opportunity(
                track=TRACK, opp_type="job_posting", org_id=org.id, source_id=src.id,
                title=f"Operations Analyst {name}", description_md=f"Own weekly reporting {name}.",
                stage="t2_done", remote_type="hybrid", url=f"https://example.test/{name}",
                first_seen_at="2026-10-20T00:00:00.000000+00:00",
                dedup_group_id=group, is_canonical=canonical,
            )
            s.add(opp)
            await s.flush()
            ids[name] = opp.id
            s.add(Evaluation(
                opportunity_id=opp.id, tier=2, track=TRACK, verdict="consider",
                score=3.6 + (i % 3) * 0.1, weighted_score=3.6,
                dimensions={k: {"score": 3, "why": "x"} for k in DIMS}, blocks={},
            ))
        await s.commit()
        return ids

    return _run(fill)


def _label(opp_id: int, score: float, when: str, source: str = "active_learning") -> None:
    async def go(s):
        s.add(Label(opportunity_id=opp_id, track=TRACK, score=score, verdict="consider",
                    source=source, created_at=f"2026-10-{when}T00:00:00.000000+00:00"))
        await s.commit()

    _run(go)


def _asked() -> list[int]:
    async def go(s):
        return [u.opportunity.id for u in
                await rank_uncertain(s, TRACK, get_settings(), limit=50)]

    return _run(go)


def _badge() -> int:
    async def go(s):
        return (await _nav_counts(s, TRACK))["calibrate"]

    return _run(go)


@pytest.fixture(autouse=True)
def _data(_isolate_data_home):
    yield


def test_rating_one_copy_removes_every_copy_of_the_role_from_calibrate():
    ids = _seed()
    # The person rates the NON-canonical copy of group a: its sibling must not be asked next.
    _label(ids["a2"], 4.0, "05")
    asked = _asked()
    assert ids["a1"] not in asked and ids["a2"] not in asked
    # group b is untouched: exactly one copy of it (the canonical one) is still asked about
    assert [i for i in asked if i in (ids["b1"], ids["b2"], ids["b3"])] == [ids["b2"]]
    assert set(asked) == {ids["b2"], ids["s1"], ids["s2"]}


def test_an_override_counts_like_a_rating_for_the_whole_group():
    ids = _seed()

    async def go(s):
        ev = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == ids["b3"]))
              ).scalars().first()
        s.add(VerdictOverride(
            opportunity_id=ids["b3"], evaluation_id=ev.id, system_verdict="consider",
            system_score=3.6, operator_verdict="apply", operator_score=4.5, reason="x"))
        await s.commit()

    _run(go)
    asked = _asked()
    assert not {ids["b1"], ids["b2"], ids["b3"]} & set(asked)
    assert set(asked) == {ids["a1"], ids["s1"], ids["s2"]}


def test_a_role_with_no_group_is_its_own_group():
    ids = _seed()
    _label(ids["s1"], 3.0, "05")
    asked = _asked()
    assert ids["s1"] not in asked and ids["s2"] in asked  # a different role, a different group
    assert len(asked) == 3  # a, b and s2: one per group


def test_the_sidebar_badge_is_the_number_of_groups_calibrate_can_ask_about():
    ids = _seed()
    assert _badge() == len(_asked()) == 4  # groups a, b and the two ungrouped roles
    _label(ids["b1"], 2.0, "05")
    assert _badge() == len(_asked()) == 3
    _label(ids["s2"], 5.0, "06")
    assert _badge() == len(_asked()) == 2
    # with no track the tray counts every track the same way
    async def all_tracks(s):
        return (await _nav_counts(s, None))["calibrate"]

    assert _run(all_tracks) == 2


def test_rating_through_the_page_updates_the_badge_to_groups_and_never_asks_the_sibling():
    ids = _seed()
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False)) as c:
        page = c.get("/calibrate").text
        since = re.search(r'data-since="([^"]+)"', page).group(1)
        r = c.post("/calibrate/rate", headers={"HX-Request": "true"},
                   data={"opportunity_id": ids["a1"], "score": "4", "since": since, "skip": ""})
        assert r.status_code == 200
        badge = re.search(r'id="nav-count-calibrate"[^>]*>(\d*)<', r.text)
        assert badge and badge.group(1) == "3"  # groups b and the two ungrouped roles
        again = c.get(f"/calibrate/card?since={since}", headers={"HX-Request": "true"}).text
    assert "Operations Analyst a2" not in again  # the other copy of a rated role is not offered

    async def queue(s):
        q = await cv.build_queue(s, TRACK, get_settings(), since=since, skip=None)
        return q.done, q.total

    done, total = _run(queue)
    assert done == 1 and total == 4  # one rated, three groups left


def test_training_and_calibration_read_one_judgment_per_group_the_latest():
    ids = _seed()
    # Historical duplicates: three copies of group b rated at different times, plus one ungrouped.
    _label(ids["b1"], 2.0, "01", "manual")
    _label(ids["b2"], 4.0, "03", "manual")
    _label(ids["b3"], 5.0, "02", "manual")
    _label(ids["a1"], 3.0, "04", "manual")
    _label(ids["s1"], 1.0, "05", "manual")

    async def go(s):
        return await labeled_examples(s, TRACK)

    examples = _run(go)
    assert len(examples) == 3  # distinct groups a, b and s1: not five rows
    by_group = {e.opportunity_id: e.operator_score for e in examples}
    assert by_group == {ids["a1"]: 3.0, ids["b2"]: 4.0, ids["s1"]: 1.0}  # b: the latest is b2
    assert [e.created_at[:10] for e in examples] == sorted(e.created_at[:10] for e in examples)


def test_a_sample_below_five_groups_is_insufficient_even_with_many_label_rows():
    ids = _seed()
    for name, day in (("a1", "01"), ("a2", "02"), ("b1", "03"), ("b2", "04"), ("b3", "05"),
                      ("s1", "06")):
        _label(ids[name], 4.0, day)

    async def go(s):
        return await cv.session_line(s, TRACK, "2026-09-01T00:00:00+00:00")

    line = _run(go)  # six rows, three groups
    assert "Not enough ratings" in line and "3 so far" in line


def test_the_reranker_trains_on_each_group_once():
    from kestrel.learning import reranker

    ids = _seed()
    # Group a is rated on its NON-canonical copy; the canonical copy a1 must add no second row
    # and must not stand in for the rated one.
    _label(ids["a2"], 5.0, "04", "manual")

    class Fitted:
        def transform(self, opp, org, vec):
            return np.asarray([float(vec[0])])

    async def go(s):
        for oid in ids.values():
            s.add(OpportunityEmbedding(opportunity_id=oid, model="m", dims=3,
                                       vector=to_bytes([float(oid), 0.0, 1.0])))
        await s.commit()
        return await reranker.build_rows(s, TRACK, Fitted(), "m")

    rows = _run(go)
    got = {r.opp_id: r.labeled for r in rows}
    # a: the rated copy only (a2), as labeled. b: one copy (the canonical) on the Tier 2 holistic.
    # s1 and s2: one each. Seven stored roles, four groups, four rows.
    assert got == {ids["a2"]: True, ids["b2"]: False, ids["s1"]: False, ids["s2"]: False}
