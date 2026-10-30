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
    """A settings file that cannot be read at all fails the cycle (nothing to fall back to) and the
    loop goes on to the next one."""
    import kestrel.config as config_mod
    import kestrel.services.setting_overrides as overrides_mod

    real_load = overrides_mod.load_settings_with_db
    real_get = config_mod.get_settings
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _load(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("settings.yml: bad value")
        stop.set()
        return await real_load(session, **kwargs)

    def _get():
        if calls["n"] == 1:
            raise ValueError("settings.yml: bad value")
        return real_get()

    async def _no_wait(_stop, _seconds) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(overrides_mod, "load_settings_with_db", _load)
    monkeypatch.setattr(config_mod, "get_settings", _get)
    monkeypatch.setattr(app_mod, "_wait", _no_wait)
    with capture_logs() as logs:
        asyncio.run(asyncio.wait_for(app_mod._delivery_loop(stop), timeout=10.0))

    assert calls["n"] == 2, "the loop died on the first settings failure"
    assert any(e["event"] == "delivery_tick_error" for e in logs)


def test_one_bad_settings_row_does_not_stop_every_alert(monkeypatch):
    """A hand-edited row that no longer validates makes the rows unusable. The cycle then runs on
    the settings file alone (alerts still arrive), says so once rather than every minute, and
    goes back to the rows as soon as they read again."""
    from sqlalchemy import select

    from kestrel.db import migrate
    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import forget_engine, get_sessionmaker
    from kestrel.db.models import Notification, Setting
    from kestrel.services import delivery

    migrate.run_migrations()
    shown: list[str] = []
    monkeypatch.setattr(delivery, "_toast", lambda title, body: shown.append(title) or True)
    monkeypatch.setattr(app_mod, "_delivery_fallback_logged", False)
    ticks = {"n": 0, "stop_at": 0}

    async def note(title: str) -> None:
        async with get_sessionmaker()() as s:
            s.add(Notification(kind="run_finished", title=title, severity="info",
                               delivered_channels=[], created_at=utcnow_iso()))
            await s.commit()

    async def row(value) -> None:
        async with get_sessionmaker()() as s:
            existing = await s.get(Setting, "thresholds.apply")
            if value is None and existing is not None:
                await s.delete(existing)
            elif value is not None:
                await s.merge(Setting(key="thresholds.apply", value=value))
            await s.commit()

    async def run_ticks(count: int) -> None:
        stop = asyncio.Event()
        ticks["n"] = 0

        async def _wait(_stop, _seconds) -> None:
            ticks["n"] += 1
            if ticks["n"] >= count:
                stop.set()

        monkeypatch.setattr(app_mod, "_wait", _wait)
        await asyncio.wait_for(app_mod._delivery_loop(stop), timeout=20.0)

    async def pending() -> int:
        async with get_sessionmaker()() as s:
            rows = (await s.execute(select(Notification))).scalars().all()
        return sum(1 for r in rows if not r.delivered_channels)

    async def go() -> list[dict]:
        with capture_logs() as logs:
            await row("not a number")  # the row that no longer validates
            await note("First")
            await run_ticks(3)  # three cycles with the bad row
            assert shown == ["First"] and await pending() == 0  # on the file alone
            await row(None)
            await note("Second")
            await run_ticks(1)  # the rows read again
            await row("not a number")
            await note("Third")
            await run_ticks(2)  # and again bad: said once more, once
        return logs

    logs = asyncio.run(go())
    forget_engine()
    said = [e for e in logs if e["event"] == "delivery_settings_rows_unusable"]
    assert len(said) == 2, "said once per run of bad cycles, not once per cycle"
    assert shown == ["First", "Second", "Third"]
    assert not [e for e in logs if e["event"] == "delivery_tick_error"]


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
