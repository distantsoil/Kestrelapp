"""Run detail (`GET /runs/{run_id}`) and the quarantine surface (`GET /quarantine`,
`POST /quarantine/{id}/retry`). Cloud-safe via TestClient, no browser and no live model (mirrors
tests/test_web_api.py: build_app(worker=False), a throwaway seed engine forgotten before the
TestClient builds its own on its own loop)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import Opportunity, Quarantine, Run

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed() -> dict:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        run = Run(
            name="pipeline",
            track="employment",
            status="done",
            started_at="2026-07-01T00:00:00Z",
            finished_at="2026-07-01T00:10:00Z",
            counts={
                "tier0": {"seen": 10, "pass": 6, "reject": 4},
                "tier1": {"completed": 6, "errors": 0, "per_minute": 12.0},
                "tier2": {"completed": 2, "errors": 0, "per_minute": 4.0},
            },
        )
        s.add(run)

        # A tier-2 quarantine: handle_t2 sets stage="quarantined" directly on structural failure.
        opp_t2 = Opportunity(
            track="employment", opp_type="job_posting", title="VP Engineering",
            org_name="Acme", url="https://x/t2", stage="quarantined", pre_score=0.5,
        )
        s.add(opp_t2)
        await s.flush()

        # A tier-1 quarantine: handle_t1 auto-promotes past the failure (hard rule 4), so the
        # opportunity is already at t1_pass when the quarantine record exists.
        opp_t1 = Opportunity(
            track="employment", opp_type="job_posting", title="Director of IT",
            org_name="Beacon", url="https://x/t1", stage="t1_pass", pre_score=0.3,
        )
        s.add(opp_t1)
        await s.flush()

        q2 = Quarantine(
            opportunity_id=opp_t2.id, tier=2, model="qwen2.5:14b",
            prompt="[system]\n...\n\n[user]\n...",
            raw_response="not valid json at all {{{",
            error="schema validation failed: verdict is a required property",
            attempts=2,
        )
        s.add(q2)
        q1 = Quarantine(
            opportunity_id=opp_t1.id, tier=1, model="qwen2.5:7b",
            prompt="[system]\n...\n\n[user]\n...",
            raw_response="I think this role is a good fit because",
            error="schema validation failed: missing 'confidence'",
            attempts=2,
        )
        s.add(q1)
        await s.commit()
        out = {
            "run_id": run.id, "opp_t2_id": opp_t2.id, "opp_t1_id": opp_t1.id,
            "q2_id": q2.id, "q1_id": q1.id,
        }
    await engine.dispose()
    forget_engine()
    return out


async def _fetch(model_cls, row_id: int):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        row = await s.get(model_cls, row_id)
        if row is not None:
            # detach values we need before the session (and its engine) close
            s.expunge(row)
    await engine.dispose()
    return row


def _client():
    """Exercise the module the way the real app serves it.

    `runs_detail` is registered in kestrel.web.app._PAGE_MODULES, so build_app() already includes
    it. The router is only attached here as a fallback if that registration is ever missing, and
    the assertion below is the point: an unconditional include would make every test in this file
    pass even with the module unregistered, so the pages could ship unreachable and still look
    tested."""
    from kestrel.web.app import _PAGE_MODULES, build_app

    assert "runs_detail" in _PAGE_MODULES, "runs_detail is not registered; its routes are dead"
    return TestClient(build_app(worker=False))


def test_run_detail_renders_a_seeded_run():
    seed = asyncio.run(_seed())
    with _client() as c:
        r = c.get(f"/runs/{seed['run_id']}")
        assert r.status_code == 200
        assert f"Run #{seed['run_id']}" in r.text
        assert "pipeline" in r.text


def test_unknown_run_id_is_404_not_500():
    asyncio.run(_seed())
    with _client() as c:
        r = c.get("/runs/999999")
        assert r.status_code == 404
        assert "No such run" in r.text


def test_quarantine_list_shows_seeded_item_and_error():
    asyncio.run(_seed())
    with _client() as c:
        r = c.get("/quarantine")
        assert r.status_code == 200
        assert "schema validation failed: verdict is a required property" in r.text
        assert "VP Engineering" in r.text


def test_retry_tier2_resets_stage_to_t1_pass_and_increments_attempts():
    seed = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/quarantine/{seed['q2_id']}/retry")
        assert r.status_code == 200

    opp = asyncio.run(_fetch(Opportunity, seed["opp_t2_id"]))
    assert opp.stage == "t1_pass"
    q = asyncio.run(_fetch(Quarantine, seed["q2_id"]))
    assert q.attempts == 3


def test_retry_tier1_never_un_promotes_the_item_hard_rule_4_saved():
    """A tier-1 quarantine is NOT stuck: handle_t1 already promoted it to t1_pass and queued t2,
    which is hard rule 4 (uncertainty escalates, it never rejects). Rolling the stage back to
    t0_pass would re-run Tier 1, which can return a kill and land the item in t1_reject, so Retry
    would be able to reject exactly the item the escalation rule rescued. Retry must instead leave
    the promotion intact and only re-queue the tier 2 that promotion already implies."""
    seed = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/quarantine/{seed['q1_id']}/retry")
        assert r.status_code == 200

    opp = asyncio.run(_fetch(Opportunity, seed["opp_t1_id"]))
    assert opp.stage == "t1_pass", "retry un-promoted an item hard rule 4 had already saved"
    q = asyncio.run(_fetch(Quarantine, seed["q1_id"]))
    assert q.attempts == 3


def test_retry_on_a_tier_without_a_worker_task_does_not_touch_the_stage():
    """Tier 3 has no worker task kind: escalation is operator-confirmed only and must never acquire
    an automatic path. Resetting to "new" would re-run Tier 0, which can reject an item that already
    cleared Tiers 0, 1 and 2, discarding t2_done and burning two more evaluations."""
    import asyncio as _asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker as _sm

    from kestrel.db.models import Quarantine as _Q

    seed = _asyncio.run(_seed())

    async def _add_t3():
        engine = make_async_engine(paths.db_path())
        sm = _sm(engine, expire_on_commit=False)
        async with sm() as s:
            opp = await s.get(Opportunity, seed["opp_t2_id"])
            opp.stage = "t2_done"
            row = _Q(opportunity_id=opp.id, tier=3, model="frontier", prompt="p",
                     raw_response="bad", error="schema", attempts=1)
            s.add(row)
            await s.commit()
            return row.id
        await engine.dispose()

    qid = _asyncio.run(_add_t3())
    with _client() as c:
        r = c.post(f"/quarantine/{qid}/retry")
        assert r.status_code == 200
        assert "operator-confirmed" in r.text

    opp = _asyncio.run(_fetch(Opportunity, seed["opp_t2_id"]))
    assert opp.stage == "t2_done", "retry threw away a completed tier-2 evaluation"


def test_retry_actually_enqueues_the_tier_task():
    """Resetting the stage is only half of a retry. Without a queued task the item sits at the reset
    stage until some unrelated sweep happens to pick it up, so the operator clicks Retry and nothing
    visibly happens. This asserts the re-queue itself, which is the point of the action."""
    from sqlalchemy import select

    from kestrel.db.models import Task

    seed = asyncio.run(_seed())
    with _client() as c:
        assert c.post(f"/quarantine/{seed['q2_id']}/retry").status_code == 200

    async def _tasks():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            rows = (await s.execute(select(Task))).scalars().all()
            out = [(t.type, t.status) for t in rows]
        await engine.dispose()
        forget_engine()
        return out

    tasks = asyncio.run(_tasks())
    assert ("t2", "pending") in tasks, f"tier-2 retry did not queue a t2 task: {tasks}"


def test_retry_preserves_raw_response():
    seed = asyncio.run(_seed())
    with _client() as c:
        c.post(f"/quarantine/{seed['q2_id']}/retry")

    q = asyncio.run(_fetch(Quarantine, seed["q2_id"]))
    assert q.raw_response == "not valid json at all {{{"


def test_retry_on_unknown_quarantine_id_does_not_500():
    asyncio.run(_seed())
    with _client() as c:
        r = c.post("/quarantine/999999/retry")
        assert r.status_code != 500
        assert r.status_code == 200
        assert "No quarantine record" in r.text


def test_no_route_deletes_a_quarantine_row():
    seed = asyncio.run(_seed())
    with _client() as c:
        r1 = c.delete(f"/quarantine/{seed['q2_id']}")
        assert r1.status_code in (404, 405)
        r2 = c.delete(f"/quarantine/{seed['q2_id']}/retry")
        assert r2.status_code in (404, 405)

    q = asyncio.run(_fetch(Quarantine, seed["q2_id"]))
    assert q is not None  # still there: retry is additive, never a delete/dismiss path


def test_a_failed_score_again_is_marked_and_try_again_keeps_the_stage():
    """Regression: a score-again of a scored role that fails keeps the stage; Failed scoring
    shows it under "Score again failed (the earlier score stands)", and Try again re-queues the
    same score-again (model override kept) without resetting the stage to t1_pass."""
    from sqlalchemy import select

    from kestrel.db.models import Task
    from kestrel.pipeline._shared import rescore_failure_prefix

    asyncio.run(_seed())
    override = {"backend": "local", "model": "kestrel-other", "json_mode": "schema"}

    async def _add():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            opp = Opportunity(track="employment", opp_type="job_posting", title="CIO Rescored",
                              org_name="Cobalt", url="https://x/rs", stage="t3_done",
                              pre_score=0.6)
            s.add(opp)
            await s.flush()
            task = Task(type="t2", status="done", payload={
                "opportunity_id": opp.id, "rescore": True,
                "requested_at": "2026-07-01T00:00:00Z", "model_override": override,
            })
            s.add(task)
            await s.flush()
            q = Quarantine(opportunity_id=opp.id, tier=2, model="kestrel-other",
                           error=rescore_failure_prefix(task.id) + "bad json", attempts=2)
            s.add(q)
            await s.commit()
            out = (opp.id, q.id, task.id)
        await engine.dispose()
        forget_engine()
        return out

    opp_id, q_id, first_task = asyncio.run(_add())
    with _client() as c:
        page = c.get("/quarantine").text
        # A failed score-again is a row of its own, marked so it never reads as a parked role.
        assert "CIO Rescored" in page and "Your earlier score stands" in page
        row = page.split(f'id="rt-row-{q_id}"')[1].split('id="rt-row-')[0]
        assert "Try again" in row and "[score again" not in row
        # Three roles wait: the two seeded rows and this one (one row per role, D-UI-104).
        assert "3 roles waiting" in page
        assert c.post(f"/quarantine/{q_id}/retry").status_code == 200

    assert asyncio.run(_fetch(Opportunity, opp_id)).stage == "t3_done"

    async def _new_tasks():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            rows = (await s.execute(select(Task).where(Task.id != first_task))).scalars().all()
            out = [(t.type, t.status, dict(t.payload)) for t in rows]
        await engine.dispose()
        forget_engine()
        return out

    ((typ, status, payload),) = asyncio.run(_new_tasks())
    assert (typ, status) == ("t2", "pending")
    assert payload["model_override"] == override and payload["rescore"] is True
    assert payload["opportunity_id"] == opp_id
