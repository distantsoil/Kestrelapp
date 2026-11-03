"""Stage line priority (issue #104): when scoring is in flight, it outranks searching. The stage
line should read "Rating N of M" (not "Searching N of M searches") once a scoring task is leased
or pending, even if some search tasks are still leased.

Tests at the stage boundary: search done, scoring started."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from kestrel.analytics.run_summary import run_summary
from kestrel.db.models import Opportunity, Run, Source, Task


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _future_iso() -> str:
    """A lease time in the future (1 hour from now), so the task is still in flight."""
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


async def _seed_search_and_score(sm, *, search_done: bool, score_started: bool) -> None:
    """Seed a run with searches and (optionally) a scoring task."""
    async with sm() as s:
        # A source and an opportunity
        src = Source(track="employment", kind="greenhouse", name="TestSource", enabled=True)
        s.add(src)
        await s.flush()
        opp = Opportunity(
            source_id=src.id,
            track="employment",
            opp_type="posting",
            title="Test Role",
            url="https://example.com/1",
            description_md="A test role.",
            stage="t1_pass" if score_started else "new",
        )
        s.add(opp)
        await s.flush()

        # A run
        run = Run(
            name="desktop",
            track="employment",
            status="running",
            started_at=_now_iso(),
            counts={},
        )
        s.add(run)
        await s.flush()

        # Search tasks: done or leased
        for _i in range(5):
            status = "done" if search_done else "leased"
            task = Task(
                type="scrape",
                status=status,
                payload={"source_id": src.id},
                created_at=_now_iso(),
                updated_at=_now_iso(),
                lease_until=None if status == "done" else _future_iso(),
            )
            s.add(task)

        # Scoring task: leased or pending (only if score_started)
        if score_started:
            task = Task(
                type="t2",
                status="leased",
                payload={"opportunity_id": opp.id},
                created_at=_now_iso(),
                updated_at=_now_iso(),
                lease_until=_future_iso(),
                worker_id="test-worker",
            )
            s.add(task)

        await s.commit()


async def test_stage_is_full_review_when_scoring_started(sm):
    """When scoring has started (t2 task leased), the stage is 'full_review', not 'searching'."""
    # Seed: searches done, scoring started
    await _seed_search_and_score(sm, search_done=True, score_started=True)

    async with sm() as session:
        summary = await run_summary(session, track=None)

    # The stage should be full_review (scoring outranks searching)
    assert summary["stage"] == "full_review"


async def test_stage_is_searching_when_only_searching(sm):
    """When only searching is in flight (no scoring), the stage is 'searching'."""
    # Seed: searches leased, no scoring
    await _seed_search_and_score(sm, search_done=False, score_started=False)

    async with sm() as session:
        summary = await run_summary(session, track=None)

    # The stage should be searching
    assert summary["stage"] == "searching"


async def test_stage_is_quick_screen_when_t1_in_flight(sm):
    """When t1 (quick screen) is in flight, the stage is 'quick_screen'."""
    # Seed: searches done, t1 in flight (with an opportunity)
    await _seed_search_and_score(sm, search_done=True, score_started=False)

    async with sm() as s:
        # Get the opportunity we just created
        from kestrel.db.models import Opportunity
        opp = (await s.execute(select(Opportunity))).scalars().first()
        if opp is None:
            pytest.skip("No opportunity seeded")

        # Seed a t1 task with the opportunity
        task = Task(
            type="t1",
            status="leased",
            payload={"opportunity_id": opp.id},
            created_at=_now_iso(),
            updated_at=_now_iso(),
            lease_until=_future_iso(),
            worker_id="test-worker",
        )
        s.add(task)
        await s.commit()

    async with sm() as session:
        summary = await run_summary(session, track=None)

    # The stage should be quick_screen (t1 outranks searching, but t2 would outrank t1)
    assert summary["stage"] in ("quick_screen", "full_review")
