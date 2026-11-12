"""A restore or an in-app reset stops the worker AND the online lane first (D-SEC-20).

INVARIANT: a restore never leaves any work running against the wrong database. A story build or a
cover letter already running in the online lane is given the quit grace, then cancelled, and its
lease handed back, before the database is replaced or emptied; both loops start again afterwards.
When a loop ignores its cancel, nothing is restored or deleted.

The loops are stand-ins (no model, no network): the lane "holds an online call" by sleeping."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import reset_engine
from kestrel.web import app as app_mod

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


async def _fresh_db() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    await asyncio.to_thread(migrate.run_migrations)
    await asyncio.to_thread(paths.seed_config)


@pytest.fixture
def loops(monkeypatch):
    """Stand-in worker and lane. The worker stops on its event; the lane is in an online call
    that does not look at the event, so only the cancel ends it (`stubborn` makes it ignore that
    too)."""
    state = {"events": [], "stubborn": False}

    async def worker(stop):
        await stop.wait()
        state["events"].append("worker stopped")

    async def lane(stop):
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                state["events"].append("lane cancelled")
                if not state["stubborn"]:
                    raise
                state["stubborn"] = False  # ignores this cancel, then ends a moment later
                await asyncio.sleep(0.3)
                return

    monkeypatch.setattr(app_mod, "_worker_loop", worker)
    monkeypatch.setattr(app_mod, "_online_lane", lane)
    monkeypatch.setattr(app_mod, "_quit_grace_s", lambda: 0.05)
    monkeypatch.setattr(app_mod, "CANCEL_WAIT_S", 0.1)
    app_stop = asyncio.Event()
    state["app_stop"] = app_stop
    yield state
    app_stop.set()
    own, w, la = app_mod._current_work_loops()
    if own is not None:
        own.set()
    for t in (w, la):
        if t is not None and not t.done():
            t.cancel()
    app_mod._work_loops.update(stop=None, worker=None, lane=None, app_stop=None)


async def _settle(tasks):
    for t in tasks:
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


async def test_the_lane_is_stopped_before_the_body_runs_and_both_restart_after(loops):
    await _fresh_db()
    app_mod._spawn_work_loops(loops["app_stop"])
    await asyncio.sleep(0.01)
    _own, worker, lane = app_mod._current_work_loops()

    async with app_mod.work_loops_stopped() as stopped:
        assert stopped is True
        assert worker.done() and lane.done()
        loops["events"].append("body")

    assert loops["events"].index("body") > loops["events"].index("lane cancelled")
    _own2, worker2, lane2 = app_mod._current_work_loops()
    assert worker2 is not worker and lane2 is not lane
    assert not worker2.done() and not lane2.done()
    await reset_engine()


async def test_a_lane_that_ignores_its_cancel_means_nothing_is_reset(loops):
    from kestrel.services import reset as reset_mod

    await _fresh_db()
    app_mod._spawn_work_loops(loops["app_stop"])
    await asyncio.sleep(0.01)
    loops["stubborn"] = True
    from kestrel.db.engine import get_sessionmaker

    with pytest.raises(reset_mod.ResetBusy):
        await reset_mod.reset_in_app(get_sessionmaker())
    assert paths.db_path().exists()
    await asyncio.sleep(0.5)  # the stubborn call ends; the loops come back
    _own, worker, lane = app_mod._current_work_loops()
    assert worker is not None and not worker.done() and not lane.done()
    await _settle((worker, lane))
    await reset_engine()


async def test_the_restore_route_stops_the_lane_before_it_restores(loops, tmp_path):
    from kestrel.services.backup import create_backup
    from kestrel.web.app import build_app

    await _fresh_db()
    archive = await asyncio.to_thread(create_backup, tmp_path)
    app_mod._spawn_work_loops(loops["app_stop"])
    await asyncio.sleep(0.01)
    _own, worker, lane = app_mod._current_work_loops()
    seen = {}
    from kestrel.services import backup as bk

    real = bk.restore_backup

    def spy(*args, **kwargs):
        seen["lane_done"] = lane.done()
        seen["worker_done"] = worker.done()
        return real(*args, **kwargs)

    import kestrel.services.backup as backup_mod

    backup_mod.restore_backup = spy
    try:
        transport = httpx.ASGITransport(app=build_app(worker=False), client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await c.post("/settings/restore", files={
                "backup_file": ("b.zip", archive.read_bytes(), "application/zip")})
    finally:
        backup_mod.restore_backup = real
    assert r.status_code == 200, r.text
    assert seen == {"lane_done": True, "worker_done": True}
    _own, worker2, lane2 = app_mod._current_work_loops()
    assert not lane2.done()
    await _settle((worker2, lane2))
    await reset_engine()
