"""The lifespan's background loops must survive a bad start, not die silently for the life of the
process.

`_worker_loop` built its LLMClient and Worker and ran `reclaim()` outside the per-cycle try, and
`_delivery_loop` read settings outside its own. A locked database or a bad settings.yml at launch
therefore raised once, the task ended, and nothing was ever evaluated or delivered again until a
restart, with nothing in the log to say so. Setup is now retried with the same warn-and-wait shape
as a failed cycle, and every created task has a done callback that logs an escaped exception.
"""

from __future__ import annotations

import asyncio
import contextlib

from structlog.testing import capture_logs

from kestrel.web import app as app_mod


def test_worker_setup_is_retried_until_it_succeeds(monkeypatch):
    from kestrel.pipeline.worker import Worker

    monkeypatch.setattr(app_mod, "_SETUP_RETRY_SECONDS", 0.01)
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _reclaim(self) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")  # the launch-time failure
        stop.set()  # setup succeeded; end the loop before it starts a real cycle
        return 0

    monkeypatch.setattr(Worker, "reclaim", _reclaim)
    with capture_logs() as logs:
        asyncio.run(asyncio.wait_for(app_mod._worker_loop(stop), timeout=10.0))

    assert calls["n"] == 2, "setup was not retried after the first failure"
    assert any(e["event"] == "serve_worker_setup_error" for e in logs), "failure not logged"


def test_delivery_loop_survives_a_settings_failure(monkeypatch):
    import kestrel.config as config_mod

    real_get_settings = config_mod.get_settings
    stop = asyncio.Event()
    calls = {"n": 0}

    def _get_settings():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("settings.yml: bad value")
        stop.set()
        return real_get_settings()

    async def _no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(config_mod, "get_settings", _get_settings)
    monkeypatch.setattr(app_mod, "_wait", _no_wait)
    with capture_logs() as logs:
        asyncio.run(asyncio.wait_for(app_mod._delivery_loop(stop), timeout=10.0))

    assert calls["n"] == 2, "the loop died on the first settings failure"
    assert any(e["event"] == "delivery_tick_error" for e in logs)


def test_a_background_task_that_dies_is_logged():
    async def _boom() -> None:
        raise RuntimeError("worker crashed")

    async def _run() -> None:
        task = asyncio.create_task(_boom(), name="worker")
        task.add_done_callback(app_mod._log_task_failure)
        with contextlib.suppress(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done callback run

    with capture_logs() as logs:
        asyncio.run(_run())
    died = [e for e in logs if e["event"] == "background_task_died"]
    assert died and died[0]["task"] == "worker" and "worker crashed" in died[0]["error"]


def test_a_cancelled_background_task_is_not_reported_as_a_failure():
    async def _forever() -> None:
        await asyncio.Event().wait()

    async def _run() -> None:
        task = asyncio.create_task(_forever(), name="scheduler")
        task.add_done_callback(app_mod._log_task_failure)
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    with capture_logs() as logs:
        asyncio.run(_run())
    assert not [e for e in logs if e["event"] == "background_task_died"]
