"""GATE (Phase 7): /calibrate loads and a label round-trips. Cloud-safe via TestClient (no browser).
Seeds with a throwaway engine and forgets the cached engine so the TestClient app builds its own on
its own loop (no cross-loop aiosqlite sharing)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Source,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


async def _seed() -> list[int]:
    await reset_engine()
    _wipe_db()  # fresh db: _isolate_data_home gives this test its OWN app-db file (function-scoped)
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids: list[int] = []
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
        s.add_all([org, src])
        await s.flush()
        for i in range(3):
            opp = Opportunity(
                track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
                title=f"VP of IT {i}", description_md="Own IT strategy.", stage="t2_done",
                remote_type="hybrid", first_seen_at="2026-07-20T00:00:00.000000+00:00",
            )
            s.add(opp)
            await s.flush()
            ids.append(opp.id)
            vec = np.random.default_rng(i).normal(0, 1, size=16).astype(np.float32)
            s.add(OpportunityEmbedding(opportunity_id=opp.id, model="m", vector=vec.tobytes(),
                                       dims=16))
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment",
                             verdict="consider", score=3.6 + i * 0.1, weighted_score=3.4))
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


async def _label_count() -> int:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        n = (await s.execute(select(func.count()).select_from(Label))).scalar_one()
    await engine.dispose()
    return n


def _client():
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_calibrate_loads_and_a_label_round_trips():
    ids = asyncio.run(_seed())
    with _client() as c:
        assert c.get("/calibrate").status_code == 200
        q = c.get("/api/learning/queue?track=employment").json()
        assert q["track"] == "employment"
        queued_ids = {item["opportunity_id"] for item in q["items"]}
        assert set(ids) <= queued_ids  # the unlabeled evaluated items are in the queue

        target = ids[0]
        r = c.post(
            "/api/learning/label",
            json={"opportunity_id": target, "track": "employment", "score": 4.1,
                  "verdict": "apply", "rationale": "strong ownership"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True

        q2 = c.get("/api/learning/queue?track=employment").json()
        # now labeled -> out of the queue
        assert target not in {i["opportunity_id"] for i in q2["items"]}
    assert asyncio.run(_label_count()) == 1  # the label persisted


def test_label_on_an_unknown_opportunity_is_404_not_500():
    # label.opportunity_id is a foreign key and SQLite enforces it, so an unknown id used to
    # surface as an IntegrityError on commit: a 500 with a database traceback.
    from kestrel.web.app import build_app

    asyncio.run(_seed())
    with TestClient(build_app(worker=False), raise_server_exceptions=False) as c:
        r = c.post(
            "/api/learning/label",
            json={"opportunity_id": 999999, "track": "employment", "score": 4.0,
                  "verdict": "apply"},
        )
    assert r.status_code == 404
    assert asyncio.run(_label_count()) == 0


def test_htmx_label_response_escapes_the_verdict():
    # The verdict is operator-supplied text echoed straight into an HTMX swap.
    ids = asyncio.run(_seed())
    hostile = '<img src=x onerror="alert(1)">'
    with _client() as c:
        r = c.post(
            "/api/learning/label",
            data={"opportunity_id": ids[0], "track": "employment", "verdict": hostile},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200
    assert hostile not in r.text
    assert "&lt;img" in r.text


def test_train_and_outcomes_endpoints_respond():
    asyncio.run(_seed())
    with _client() as c:
        # too few labels to promote, but the endpoint must respond cleanly (not 500)
        t = c.post("/api/learning/train?track=employment").json()
        assert t["promoted"] is False
        o = c.get("/api/learning/outcomes?track=employment").json()
        assert "discriminating" in o and "headline" in o
