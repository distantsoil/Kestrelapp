"""A thin copy of a role that is scored from another board says so and links to the scored copy
(issue #144). Display and link only: the thin copy is never folded, hidden or rejected. Cloud-safe:
a migrated SQLite file and the test client. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib

from kestrel.db.models import Evaluation, Opportunity
from kestrel.web.queries import OppFilters, list_opportunities
from tests import test_pipeline_view as T
from tests.test_not_scored_reasons import _db, _reasons


async def _link(s, thin: int, scored: int, *, score: float = 1.8, archived: bool = False):
    for oid in (thin, scored):
        (await s.get(Opportunity, oid)).dedup_group_id = "grp-1"
    if archived:
        (await s.get(Opportunity, scored)).archived_at = "2026-11-01T00:00:00Z"
    # Like the real thin copies: it passed the Tier 1 screen and never reached a Tier 2 score.
    s.add(Evaluation(opportunity_id=thin, tier=1, track="employment", verdict="keep"))
    s.add(Evaluation(opportunity_id=scored, tier=2, track="employment", verdict="pass",
                     score=score, weighted_score=score))
    await s.commit()


def _seed_pair(**kw):
    asyncio.run(T._seed())
    ids = _db(_reasons)

    async def link(s):
        await _link(s, ids["empty"], ids["waiting"], **kw)

    _db(link)
    return ids


def test_the_row_the_panel_and_the_role_page_show_the_scored_copy_and_link_to_it():
    ids = _seed_pair(score=1.8)
    thin, scored = ids["empty"], ids["waiting"]
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
        panel = htmllib.unescape(c.get(f"/opportunities/{thin}/panel").text)
        role = htmllib.unescape(c.get(f"/opportunities/{thin}").text)
    row = T._row(page, thin)
    assert "The same role from another board is scored 1.8." in row
    assert f'href="/opportunities/{scored}"' in row and 'data-testid="row-twin"' in row
    assert "The same role from another board is scored 1.8." in panel
    assert f'href="/opportunities/{scored}"' in panel
    assert "The same role from another board is scored 1.8." in role
    assert f'href="/opportunities/{scored}"' in role


def test_the_thin_copy_stays_listed_and_never_reads_as_a_rejection():
    ids = _seed_pair()
    thin = ids["empty"]

    async def listed(s):
        return await list_opportunities(s, OppFilters(grouped=True, page_size=200))

    row = {r.opp.id: r for r in _db(listed).rows}[thin]
    assert row.twin is not None and row.twin.opp_id == ids["waiting"]
    assert row.fit.word == "new" and row.opp.stage == "t1_pass" and row.opp.archived_at is None


def test_a_role_without_a_scored_copy_shows_no_twin_line():
    asyncio.run(T._seed())
    ids = _db(_reasons)
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
    assert "from another board" not in T._row(page, ids["empty"])


def test_a_hidden_scored_copy_is_not_offered():
    ids = _seed_pair(archived=True)
    with T._client() as c:
        page = htmllib.unescape(T._get(c, "/opportunities"))
    assert "from another board" not in T._row(page, ids["empty"])


def test_a_tier2_row_without_a_score_still_shows_the_scored_copy_on_the_role_page():
    ids = _seed_pair(score=2.2)
    thin, scored = ids["empty"], ids["waiting"]

    async def quarantined_t2(s):  # the newest row is a Tier 2 attempt that produced no score
        s.add(Evaluation(opportunity_id=thin, tier=2, track="employment", verdict=None,
                         score=None, weighted_score=None))
        await s.commit()

    _db(quarantined_t2)
    with T._client() as c:
        role = htmllib.unescape(c.get(f"/opportunities/{thin}").text)
    assert "The same role from another board is scored 2.2." in role
    assert f'href="/opportunities/{scored}"' in role
