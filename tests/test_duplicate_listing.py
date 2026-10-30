"""One role found on two boards is one row in every list (SPEC.md 5.5): the owner saw the same
Vice President role at the same company twice on Today, with different description text."""

from __future__ import annotations

from kestrel.db.models import Opportunity, Pursuit, Source
from kestrel.ingest.base import RawPosting
from kestrel.ingest.normalize import normalize_and_store
from kestrel.web.queries import OppFilters, list_opportunities


async def _two_copies(session) -> list[int]:
    ids = []
    for kind, xid, desc in (
        ("jobspy", "j1", "A newly created VP of IT role at an established manufacturer."),
        ("google", "g1", "A remote, full-time VP of IT posting with posted pay of $200,000."),
    ):
        src = Source(track="employment", kind=kind, name=kind, config={}, enabled=True)
        session.add(src)
        await session.flush()
        raw = RawPosting(
            external_id=xid,
            url=f"https://{kind}.example/{xid}",
            title="Vice President of Information Technology",
            org_name="Harbor Spa Group",
            description_md=desc,
        )
        result = await normalize_and_store(session, raw, source=src)
        ids.append(result.opportunity.id)
    await session.commit()
    return ids


async def test_one_role_on_two_boards_lists_once(sm):
    async with sm() as session:
        await _two_copies(session)
        page = await list_opportunities(session, OppFilters(track="employment"))
        assert page.total == 1 and len(page.rows) == 1
        both = await list_opportunities(
            session, OppFilters(track="employment", include_duplicates=True)
        )
        assert both.total == 2


async def test_a_shortlisted_copy_never_disappears(sm):
    async with sm() as session:
        ids = await _two_copies(session)
        page = await list_opportunities(session, OppFilters(track="employment"))
        shown = page.rows[0].opp.id
        other = next(i for i in ids if i != shown)
        session.add(Pursuit(opportunity_id=other, track="employment", status="shortlisted"))
        await session.commit()
        page = await list_opportunities(session, OppFilters(track="employment"))
        assert {r.opp.id for r in page.rows} == set(ids)


async def test_failure_counts_match_the_list_for_a_duplicated_role(sm):
    # Windows round 8: the Scoring failed group read 2 while the header and chip read 4, because
    # the counts still included the duplicate copies the list hides.
    from kestrel.web.queries import _PARKED_STAGES, scoring_failed_count

    async with sm() as session:
        ids = await _two_copies(session)
        for oid in ids:
            opp = await session.get(Opportunity, oid)
            opp.stage = _PARKED_STAGES[0]
        await session.commit()
        page = await list_opportunities(session, OppFilters(track="employment"))
        assert page.total == 1
        assert await scoring_failed_count(session, "employment") == 1
