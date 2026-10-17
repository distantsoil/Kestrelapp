"""REST API robustness (Phase 6 reviewer fixes), cloud-safe via TestClient (no browser). Seeds with
a throwaway engine and then forgets the cached engine so the TestClient app builds its own on its
own loop (no cross-loop aiosqlite sharing)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Evaluation, Opportunity, VerdictOverride

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed() -> int:
    await reset_engine()
    _wipe_db()  # a fresh db per test: _isolate_data_home gives each test its OWN app-db file
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        opp = Opportunity(
            track="employment", opp_type="job_posting", title="Head of IT", org_name="Beacon",
            url="https://x/9", stage="t2_done", ghost_score=0.2,
        )
        s.add(opp)
        await s.flush()
        opp_id = opp.id
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment", verdict="consider",
                         score=3.4, weighted_score=3.3))
        await s.commit()
    await engine.dispose()
    forget_engine()  # TestClient builds its own engine on its own loop
    return opp_id


async def _override_count(opp_id: int) -> int:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(VerdictOverride).where(
                    VerdictOverride.opportunity_id == opp_id
                )
            )
        ).scalar_one()
    await engine.dispose()
    return n


def _client():
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_agree_writes_override_and_lookup_works():
    opp_id = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/api/opportunities/{opp_id}/override", json={"mode": "agree"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["override"]["operator_verdict"] == "consider"  # agreed with the system
        lk = c.post("/api/opportunities/lookup", json={"urls": ["https://x/9"]})
        assert lk.json()["https://x/9"]["verdict"] == "consider"
    assert asyncio.run(_override_count(opp_id)) == 1


def test_override_requires_a_reason():
    opp_id = asyncio.run(_seed())
    with _client() as c:
        r = c.post(
            f"/api/opportunities/{opp_id}/override",
            json={"mode": "override", "operator_verdict": "pass"},
        )
        assert r.status_code == 400 and "reason" in r.json()["error"]
        # with a reason it succeeds
        ok = c.post(
            f"/api/opportunities/{opp_id}/override",
            json={"mode": "override", "operator_verdict": "pass", "reason": "culture mismatch"},
        )
        assert ok.status_code == 200
    assert asyncio.run(_override_count(opp_id)) == 1  # only the valid one was written


def test_malformed_json_is_400_not_500():
    opp_id = asyncio.run(_seed())
    with _client() as c:
        r = c.post(
            f"/api/opportunities/{opp_id}/override",
            content=b"not json", headers={"content-type": "application/json"},
        )
        assert r.status_code == 400
        lk = c.post(
            "/api/opportunities/lookup",
            content=b"{bad",
            headers={"content-type": "application/json"},
        )
        assert lk.status_code == 400


def test_override_on_missing_opportunity_is_404():
    asyncio.run(_seed())
    with _client() as c:
        r = c.post("/api/opportunities/999999/override", json={"mode": "agree"})
        assert r.status_code == 404


def test_override_rejects_an_unknown_verdict():
    # The override is a label the learning loop trains on; an arbitrary string would poison
    # calibration silently instead of failing loudly.
    opp_id = asyncio.run(_seed())
    with _client() as c:
        r = c.post(
            f"/api/opportunities/{opp_id}/override",
            json={"mode": "override", "operator_verdict": "maybe", "reason": "hmm"},
        )
        assert r.status_code == 422
        assert "operator_verdict" in r.json()["error"]
        ok = c.post(
            f"/api/opportunities/{opp_id}/override",
            json={"mode": "override", "operator_verdict": "kill", "reason": "not a fit"},
        )
        assert ok.status_code == 200
    assert asyncio.run(_override_count(opp_id)) == 1  # only the valid verdict was written


async def _seed_for_lookup() -> None:
    """One opportunity with two evaluations (the newer must win), one with none."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        twice = Opportunity(track="employment", opp_type="job_posting", title="A",
                            org_name="Beacon", url="https://x/twice", stage="t2_done")
        never = Opportunity(track="employment", opp_type="job_posting", title="B",
                            org_name="Beacon", url="https://x/never", stage="t0_pass")
        s.add_all([twice, never])
        await s.flush()
        s.add(Evaluation(opportunity_id=twice.id, tier=2, track="employment",
                         verdict="consider", score=3.1))
        s.add(Evaluation(opportunity_id=twice.id, tier=2, track="employment",
                         verdict="apply", score=4.6))
        await s.commit()
    await engine.dispose()
    forget_engine()


def test_lookup_reports_the_latest_evaluation_in_one_query():
    # lookup_by_urls is called by the extension on every DOM mutation batch, so it must not
    # run one Evaluation query per matched URL; and the single join must still pick the LATEST
    # evaluation and tolerate an opportunity with none.
    asyncio.run(_seed_for_lookup())
    with _client() as c:
        body = c.post(
            "/api/opportunities/lookup",
            json={"urls": ["https://x/twice", "https://x/never", "https://x/unknown"]},
        ).json()
    assert body["https://x/twice"]["verdict"] == "apply"
    assert body["https://x/twice"]["score"] == 4.6
    assert body["https://x/never"]["verdict"] is None
    assert body["https://x/never"]["stage"] == "t0_pass"
    assert "https://x/unknown" not in body


async def test_lookup_issues_a_single_statement(sm):
    from sqlalchemy import event

    from kestrel.web.queries import lookup_by_urls

    async with sm() as s:
        for i in range(4):
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"T{i}",
                              org_name="Beacon", url=f"https://x/{i}", stage="t2_done")
            s.add(opp)
            await s.flush()
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="consider", score=3.0))
        await s.commit()

    statements: list[str] = []
    async with sm() as s:
        engine = s.get_bind()

        def _count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            out = await lookup_by_urls(s, [f"https://x/{i}" for i in range(4)])
        finally:
            event.remove(engine, "before_cursor_execute", _count)
    assert len(out) == 4
    assert len(statements) == 1, f"expected one SELECT, got {len(statements)}"
