"""The profile save says how many set-aside roles get a second look (D-RULES-RECHECK)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import Opportunity
from kestrel.services import profile_edit as pe
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


async def _seed() -> None:
    async with get_sessionmaker()() as s:
        s.add(Opportunity(track="employment", opp_type="job_posting", title="Support Engineer",
                          external_id="a", stage="t0_reject",
                          stage_reason="blocker:seniority_floor: low"))
        await s.commit()


def test_saving_a_looser_floor_says_how_many_roles_get_a_second_look():
    from kestrel.web.app import build_app

    pe.save_section("employment", "about", {"full_name": "Pat", "email": "pat@example.com"})
    _run(_seed())
    with TestClient(build_app(worker=False)) as c:
        first = c.post("/profile/employment/employment",
                       data={"target_titles": "IT Director", "seniority_floor": "director"})
        assert first.status_code == 200
        said = json.loads(first.headers["hx-trigger"])["pf-saved"]["message"]
        assert said.startswith("Saved.")
        assert "Saved to profile.yml" not in first.text  # one confirmation: the toast, not the bar
        again = c.post("/profile/employment/employment",
                       data={"target_titles": "IT Director", "seniority_floor": "director"})
        assert "re-check" not in again.headers["hx-trigger"]  # nothing the rules read has changed
        looser = c.post("/profile/employment/employment",
                        data={"target_titles": "IT Director", "seniority_floor": ""})
    assert "Kestrel will re-check 1 role the rules set aside." in looser.headers["hx-trigger"]
