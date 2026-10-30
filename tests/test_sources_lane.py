"""Sources page: roles waiting in the background lane and "Score everything now" (D-SRC-8,
D-SRC-9). Offline, no model."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Opportunity, Source, Task
from kestrel.pipeline import lane
from kestrel.services import tracks


@pytest.fixture(autouse=True)
def _fresh():
    tracks.forget_cache()
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    yield
    tracks.forget_cache()
    asyncio.run(reset_engine())


def _run(coro):
    async def _wrap():
        try:
            return await coro
        finally:
            await reset_engine()

    return asyncio.run(_wrap())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _seed(n: int) -> None:
    async with get_sessionmaker()() as s:
        src = Source(track="employment", kind="greenhouse", name="Northwind jobs (Greenhouse)",
                     config={"token": "northwind"}, enabled=True, origin="discovered")
        s.add(src)
        await s.flush()
        for i in range(n):
            o = Opportunity(track="employment", opp_type="job_posting", source_id=src.id,
                            external_id=f"e{i}", title=f"Operations Analyst {i}", stage="t0_pass")
            s.add(o)
            await s.flush()
            s.add(Task(type="t1", priority=lane.LANE_PRIORITY,
                       payload={"opportunity_id": o.id, **lane.lane_extra(0.0)}))
        await s.commit()


async def _lane_left() -> int:
    async with get_sessionmaker()() as s:
        return await lane.waiting_count(s)


def test_waiting_count_and_the_cost_warning_are_shown_before_confirming():
    _run(_seed(3))
    with _client() as c:
        html = c.get("/sources").text
    assert 'data-testid="lane-waiting"' in html and "Waiting: 3 roles" in html
    assert 'data-testid="lane-score-now"' in html and 'data-testid="lane-confirm"' in html
    assert "Recheck 3 waiting roles and score matches locally?" in html
    assert "Unrelated titles are skipped before storage and counted on each run." in html
    assert "rough figure" in html  # no evaluations yet: the figure says it is rough


def test_nothing_waiting_shows_no_lane_block():
    with _client() as c:
        html = c.get("/sources").text
    assert 'data-testid="lane"' not in html


def test_score_everything_needs_the_confirmation_and_then_runs_once():
    _run(_seed(3))
    with _client() as c:
        assert c.post("/sources/score-everything").status_code == 400
        assert _run(_lane_left()) == 3  # an unconfirmed click changes nothing
        r = c.post("/sources/score-everything", data={"confirm": "1"})
        assert r.status_code == 200 and "Scoring 3 waiting roles now." in r.text
    assert _run(_lane_left()) == 0
    async def _normal() -> int:
        async with get_sessionmaker()() as s:
            ts = (await s.execute(select(Task))).scalars().all()
            return sum(1 for t in ts if t.payload.get("lane") == 0)
    assert _run(_normal()) == 3


def test_hosted_scoring_says_the_backlog_requires_local_models(monkeypatch):
    _run(_seed(2))
    with _client() as c:
        assert "Remaining roles score when Kestrel is idle." in c.get("/sources").text
        monkeypatch.setattr(lane, "scores_on_hosted", lambda settings: True)
        html = c.get("/sources").text
        refused = c.post("/sources/score-everything", data={"confirm": "1"})
    assert "Choose local models for both scoring tiers" in html
    assert refused.status_code == 400 and "Nothing was sent" in refused.text
    assert _run(_lane_left()) == 2
