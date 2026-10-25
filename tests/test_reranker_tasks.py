"""Local sorting-model scheduling, durable retries, and app-first controls (lane F4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Label, Opportunity, Task
from kestrel.learning import reranker


async def _ratings(sm, n, *, offset=0):
    async with sm() as s:
        for i in range(n):
            opp = Opportunity(track="employment", opp_type="job_posting", title=f"Role {offset+i}")
            s.add(opp)
            await s.flush()
            s.add(Label(opportunity_id=opp.id, track="employment", score=3, source="manual"))
        await s.commit()


async def test_worker_attempts_training_at_floor_without_cli(sm, monkeypatch):
    from kestrel.db import engine
    from kestrel.learning import training
    from kestrel.pipeline.worker import TierStats, Worker
    from kestrel.services import setting_overrides
    from kestrel.web import app

    await _ratings(sm, 2)
    settings = get_settings().model_copy(deep=True)
    settings.learning.min_labels_for_reranker = 2
    monkeypatch.setattr(engine, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(setting_overrides, "load_settings_with_db",
                        AsyncMock(return_value=settings))
    monkeypatch.setattr(training, "load_settings_with_db", AsyncMock(return_value=settings))
    monkeypatch.setattr(Worker, "reclaim", AsyncMock(return_value=0))
    monkeypatch.setattr(Worker, "drain_scrapes", AsyncMock(return_value=TierStats()))
    monkeypatch.setattr(Worker, "drain_embed", AsyncMock())
    monkeypatch.setattr(Worker, "drain_tier", AsyncMock())
    monkeypatch.setattr(Worker, "release_lane", AsyncMock(return_value=0))
    monkeypatch.setattr(app, "is_paused", lambda: False)
    monkeypatch.setattr(app, "_maybe_run_liveness", AsyncMock())
    train = AsyncMock(return_value=reranker.TrainResult(
        "employment", 2, False, reason="not better"))
    monkeypatch.setattr(reranker, "train_reranker", train)
    monkeypatch.setattr(reranker, "check_drift", AsyncMock())
    stop = asyncio.Event()

    async def finish(*args):
        stop.set()

    monkeypatch.setattr(app, "_wait", finish)
    await asyncio.wait_for(app._worker_loop(stop), 20)
    assert train.await_count == 1
    async with sm() as s:
        tasks = list((await s.execute(select(Task).where(Task.type == "train_reranker"))).scalars())
    assert len(tasks) == 1 and tasks[0].status == "done"


async def test_failed_promotion_retries_after_50_more_ratings_and_survives_restart(sm, monkeypatch):
    from kestrel.learning import training

    settings = get_settings().model_copy(deep=True)
    settings.learning.min_labels_for_reranker = 2
    monkeypatch.setattr(training, "load_settings_with_db", AsyncMock(return_value=settings))
    train = AsyncMock(return_value=reranker.TrainResult(
        "employment", 2, False, reason="not better"))
    monkeypatch.setattr(reranker, "train_reranker", train)
    await _ratings(sm, 1)
    assert await training.run_cycle(sm) == 0
    await _ratings(sm, 1, offset=1)
    assert await training.run_cycle(sm) == 1
    assert await training.run_cycle(sm) == 0
    await _ratings(sm, 49, offset=2)
    assert await training.run_cycle(sm) == 0
    await _ratings(sm, 1, offset=51)
    # Queue, then abandon the caller. A new cycle sees the persisted work.
    async with sm() as s:
        assert "queued" in await training.request_training(s, "employment", automatic=True)
    assert await training.run_cycle(sm) == 1
    assert train.await_count == 2
    assert await training.run_cycle(sm) == 0


async def test_training_rechecks_off_switch_and_deduplicates_clicks(sm, monkeypatch):
    from kestrel.learning import training

    settings = get_settings().model_copy(deep=True)
    settings.learning.min_labels_for_reranker = 2
    monkeypatch.setattr(training, "load_settings_with_db", AsyncMock(return_value=settings))
    await _ratings(sm, 2)
    async with sm() as s:
        assert "queued" in await training.request_training(s, "employment")
        assert "already" in await training.request_training(s, "employment")
    settings.learning.build_model = False
    assert await training.run_cycle(sm) == 1
    async with sm() as s:
        tasks = list((await s.execute(select(Task))).scalars())
        assert len(tasks) == 1 and "switched off" in tasks[0].payload["result"]["reason"]
        assert "Turn on" in await training.request_training(s, "employment")


async def test_training_failure_is_retried_from_durable_task(sm, monkeypatch):
    from kestrel.learning import training

    settings = get_settings().model_copy(deep=True)
    settings.learning.min_labels_for_reranker = 2
    monkeypatch.setattr(training, "load_settings_with_db", AsyncMock(return_value=settings))
    train = AsyncMock(side_effect=[
        OSError("disk busy"), reranker.TrainResult("employment", 2, False)])
    monkeypatch.setattr(reranker, "train_reranker", train)
    await _ratings(sm, 2)
    assert await training.run_cycle(sm) == 0
    async with sm() as s:
        task = (await s.execute(select(Task))).scalar_one()
        assert task.status == "pending" and "disk busy" in task.last_error
    assert await training.run_cycle(sm) == 1
    assert train.await_count == 2


async def test_historical_promotion_does_not_claim_model_is_currently_active(sm):
    from kestrel.learning import training

    async with sm() as s:
        s.add(Task(type=training.TASK_TYPE, status="done", payload={
            "track": "employment", "ratings": 200, "result": {"promoted": True},
        }))
        await s.commit()
        status = await training.state(s, "employment")
    assert "now in use" not in status["message"]
    assert "passed" in status["message"]
