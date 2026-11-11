"""#153 quit grace: Quit gives a model call that is already running a short window, then cuts
it off and hands its task back with the attempt refunded. Invariant: Quit always finishes
within the grace plus a small bound, whatever the model call does. No network, no real model."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from kestrel.config import Server
from kestrel.db.models import Task
from kestrel.queue import TaskQueue
from kestrel.web import app as app_mod


@pytest.fixture
def _bounds(monkeypatch, sm):
    monkeypatch.setattr(app_mod, "CANCEL_WAIT_S", 0.2)
    monkeypatch.setattr("kestrel.db.engine.get_sessionmaker", lambda: sm)


async def _leased_task(sm) -> int:
    q = TaskQueue(sm)
    tid = await q.enqueue("t1", {"opportunity_id": 1})
    assert await q.lease("w", types=["t1"]) is not None
    return tid


async def test_quit_finishes_in_grace_plus_bound_when_the_model_call_never_returns(sm, _bounds):
    tid = await _leased_task(sm)
    stop = asyncio.Event()
    unstick = asyncio.Event()

    async def model_call_that_never_returns():
        while not unstick.is_set():  # ignores the stop event and swallows the cancel
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(model_call_that_never_returns())
    await asyncio.sleep(0)
    grace = 0.3
    started = time.monotonic()
    await app_mod._graceful_worker_shutdown(worker, stop, grace_s=grace)
    took = time.monotonic() - started
    unstick.set()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert stop.is_set()
    assert grace <= took < grace + app_mod.CANCEL_WAIT_S + 1.0
    async with sm() as s:  # cut off, not failed: pending again, attempt handed back
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == "pending" and t.attempts == 0


async def test_quit_is_prompt_when_nothing_is_running(sm, _bounds):
    await _leased_task(sm)
    stop = asyncio.Event()

    async def idle_worker():
        await stop.wait()

    worker = asyncio.create_task(idle_worker())
    started = time.monotonic()
    await app_mod._graceful_worker_shutdown(worker, stop, grace_s=5.0)
    assert time.monotonic() - started < 1.0  # the grace is a ceiling, not a delay


def test_the_grace_is_a_setting_with_a_few_second_default_and_a_clamp():
    assert Server().quit_grace_s == 5.0
    assert Server(quit_grace_s=-3).quit_grace_s == 0.0
    assert Server(quit_grace_s=999).quit_grace_s == 30.0


def test_quit_reads_the_configured_grace(monkeypatch):
    from kestrel import config

    class _S:
        server = Server(quit_grace_s=7)

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    assert app_mod._quit_grace_s() == 7.0


async def test_a_broken_settings_file_still_stops_the_worker_and_hands_back_its_lease(
    sm, _bounds
):
    """D-QUEUE-6: a settings.yml that does not parse raised out of the grace read before the
    worker was cancelled and before the lease release. Now the grace falls back to its default
    and the lease is handed back with the attempt refunded."""
    from kestrel import paths

    paths.config_dir().joinpath("settings.yml").write_text(
        "server: [this is: not, valid yaml\n  quit_grace_s: {{\n", encoding="utf-8"
    )
    assert app_mod._quit_grace_s() == app_mod.WORKER_STOP_S
    tid = await _leased_task(sm)
    stop = asyncio.Event()

    async def idle_worker():
        await stop.wait()

    worker = asyncio.create_task(idle_worker())
    await app_mod._graceful_worker_shutdown(worker, stop)
    assert worker.done()
    async with sm() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == "pending" and t.attempts == 0


async def test_the_lease_release_runs_even_when_an_earlier_shutdown_step_raises(
    sm, _bounds, monkeypatch
):
    tid = await _leased_task(sm)

    def broken_grace():
        raise RuntimeError("an unexpected failure before the release")

    monkeypatch.setattr(app_mod, "_quit_grace_s", broken_grace)
    stop = asyncio.Event()
    worker = asyncio.create_task(stop.wait())
    await app_mod._graceful_worker_shutdown(worker, stop)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    async with sm() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == "pending" and t.attempts == 0


async def test_the_online_lane_gets_the_same_grace_as_the_worker(sm, _bounds):
    """D-QUEUE-6: a running online call on the lane is given the quit grace to finish, the same
    as the worker's, instead of being cancelled the moment Quit starts."""
    stop = asyncio.Event()
    finished: list[str] = []

    async def online_call_finishing_soon():
        await stop.wait()
        await asyncio.sleep(0.2)  # the reply arrives a moment after Quit
        finished.append("lane")

    async def idle_worker():
        await stop.wait()

    worker = asyncio.create_task(idle_worker())
    lane = asyncio.create_task(online_call_finishing_soon())
    await asyncio.sleep(0)
    started = time.monotonic()
    await app_mod._graceful_worker_shutdown(worker, stop, grace_s=2.0, also=(lane,))
    assert finished == ["lane"] and not lane.cancelled()
    assert time.monotonic() - started < 1.5  # the grace is a ceiling, not a delay


async def test_a_lane_call_that_never_returns_is_cut_off_within_the_bound(sm, _bounds):
    tid = await _leased_task(sm)
    stop = asyncio.Event()
    unstick = asyncio.Event()

    async def swallowing_lane():
        while not unstick.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(stop.wait())
    lane = asyncio.create_task(swallowing_lane())
    await asyncio.sleep(0)
    grace = 0.3
    started = time.monotonic()
    await app_mod._graceful_worker_shutdown(worker, stop, grace_s=grace, also=(lane,))
    took = time.monotonic() - started
    unstick.set()
    lane.cancel()
    await asyncio.gather(lane, worker, return_exceptions=True)
    assert grace <= took < grace + app_mod.CANCEL_WAIT_S + 1.0
    async with sm() as s:
        t = (await s.execute(select(Task).where(Task.id == tid))).scalar_one()
    assert t.status == "pending" and t.attempts == 0


def test_the_lifespan_lets_the_online_lane_finish_its_call_on_quit(monkeypatch):
    """The lifespan gives the online lane the quit grace instead of cancelling it with the other
    loops (D-QUEUE-6): a lane call that ends a moment after Quit finishes."""
    from fastapi.testclient import TestClient

    from kestrel.db import migrate
    from tests.test_startup_retry import _recorders

    migrate.run_migrations()
    _recorders(monkeypatch)
    monkeypatch.setattr(app_mod, "_graceful_worker_shutdown", _REAL_SHUTDOWN)
    monkeypatch.setattr(app_mod, "_quit_grace_s", lambda: 2.0)
    monkeypatch.setattr(app_mod, "WORKER_DELAY_S", 0.0)
    events: list[str] = []

    async def lane(stop):
        events.append("lane-started")
        await stop.wait()
        try:
            await asyncio.sleep(0.2)  # the online reply arrives a moment after Quit
        except asyncio.CancelledError:
            events.append("lane-cancelled")
            raise
        events.append("lane-finished")

    monkeypatch.setattr(app_mod, "_online_lane", lane)
    with TestClient(app_mod.build_app(worker=True)) as c:
        assert c.get("/health").status_code == 200
        deadline = time.time() + 5
        while "lane-started" not in events and time.time() < deadline:
            time.sleep(0.01)
    assert events == ["lane-started", "lane-finished"]


_REAL_SHUTDOWN = app_mod._graceful_worker_shutdown


async def test_the_online_lane_releases_holds_by_the_settings_the_run_holds(sm, monkeypatch):
    """D-QUEUE-6: while a run holds the settings it was confirmed on (a carried run, a one-shot
    run mode), the lane releases holds by those, the same as the worker defers by, never by the
    stored settings (release, defer again, every pass, for the life of the run)."""
    from kestrel.config import get_settings
    from kestrel.queue import queue as queue_mod

    monkeypatch.setattr("kestrel.db.engine.get_sessionmaker", lambda: sm)
    held = get_settings().model_copy(deep=True)
    seen: list[object] = []
    stop = asyncio.Event()

    async def record(_sm, settings):
        seen.append(settings)
        stop.set()
        return 0

    monkeypatch.setattr(queue_mod, "release_holds", record)
    app_mod._hold_run(held)
    try:
        await asyncio.wait_for(app_mod._online_lane(stop), timeout=10)
    finally:
        app_mod._release_run()
    assert seen == [held] and seen[0] is held
    stop.clear()
    seen.clear()
    await asyncio.wait_for(app_mod._online_lane(stop), timeout=10)  # no run: the stored ones
    assert len(seen) == 1 and seen[0] is not held


def test_the_quit_ceiling_fits_the_largest_grace_the_setting_allows():
    """At the top of the quit grace range the shutdown still cancels the work and hands the
    leases back before the desktop shell gives up waiting (fable review, D-QUEUE-5)."""
    from kestrel.config import Server
    from kestrel.desktop import shell
    from kestrel.web import app as app_mod
    from kestrel.web import serve

    largest = Server(quit_grace_s=10_000).quit_grace_s
    needed = (serve.GRACEFUL_REQUEST_S + largest + app_mod.CANCEL_WAIT_S
              + app_mod.RELEASE_WAIT_S)
    assert shell.QUIT_JOIN_S >= needed + 5  # room for the other loops' own stop
