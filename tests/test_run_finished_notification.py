"""Issues #109, #110, #125, #126, #127: a finished run raises ONE "Run finished" notification
(SPEC-INTERFACES 6), and the Settings > Preferences > Notifications switch named "Run finished"
is the one that decides whether the person is told.

Every test drives the worker's real `run()` against a migrated database with a fake model, then
reads the stored notifications and, for the switch, runs the real delivery cycle."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import get_settings
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine, reset_engine
from kestrel.db.models import Notification, Opportunity, Source
from kestrel.pipeline.worker import Worker
from kestrel.services.delivery import deliver_pending
from kestrel.services.setting_overrides import load_settings_with_db
from tests.pipeline_fakes import FakeLLM

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture(autouse=True)
def _no_ollama_unload(monkeypatch):
    """The worker unloads the previous tier's model on a tier switch; there is no Ollama here,
    so patch that call at the boundary instead of logging a ConnectError warning."""

    async def fake_unload(client, native_url, name, timeout=30.0) -> bool:  # noqa: ASYNC109
        return True

    monkeypatch.setattr("kestrel.llm.client.unload_model", fake_unload)


def _recent() -> str:
    return (datetime.now(UTC) - timedelta(days=3)).date().isoformat()


async def _with_sm(fn):
    engine = make_async_engine(paths.db_path())
    try:
        return await fn(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _fresh_db(opportunities: int, *, new_in_run: int = 0) -> None:
    async def go() -> None:
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()

        async def seed(sm) -> None:
            async with sm() as s:
                src = Source(track="employment", kind="jobspy", name="emp", enabled=True)
                s.add(src)
                await s.flush()
                for i in range(opportunities):
                    s.add(Opportunity(
                        track="employment", opp_type="job_posting", source_id=src.id,
                        title=f"Operations Analyst #{i}", comp_max=120000, comp_source="parsed",
                        remote_type="hybrid", state="OH", city="Columbus",
                        location_raw="Columbus, OH", date_posted=_recent(), seen_count=1,
                        ghost_score=0.1, stage="new",
                        description_md="Own reporting for three distribution centers."
                        + " Details follow." * 20,
                    ))
                for i in range(new_in_run):  # first seen after the run starts: new in this run
                    s.add(Opportunity(
                        track="employment", opp_type="job_posting", source_id=src.id,
                        title=f"Fresh Operations Analyst #{i}", comp_max=120000,
                        comp_source="parsed", remote_type="hybrid", state="OH", city="Columbus",
                        location_raw="Columbus, OH", date_posted=_recent(), seen_count=1,
                        ghost_score=0.1, stage="new", first_seen_at="2999-01-01T00:00:00+00:00",
                        description_md="Own reporting for three distribution centers."
                        + " Details follow." * 20,
                    ))
                await s.commit()

        await _with_sm(seed)

    asyncio.run(go())


def _run_once(llm_cls=FakeLLM) -> None:
    async def go(sm) -> None:
        settings = get_settings()
        await Worker(sm, llm_cls(settings), settings).run()

    asyncio.run(_with_sm(go))


def _notifications(kind: str | None = None) -> list[Notification]:
    async def go(sm):
        async with sm() as s:
            q = select(Notification).order_by(Notification.id)
            if kind:
                q = q.where(Notification.kind == kind)
            return list((await s.execute(q)).scalars())

    return asyncio.run(_with_sm(go))


def _deliver() -> tuple[int, int]:
    """One real delivery cycle with the person's saved switches; no pop-up, sound or push."""

    async def go(sm) -> tuple[int, int]:
        async with sm() as s:
            settings = await load_settings_with_db(s)
            settings.notify.enabled = True
            settings.notify.toast = False
            settings.notify.sound = False
            settings.notify.ntfy_topic = None
            settings.notify.webhook_url = None
            settings.notify.quiet_hours = {"enabled": False, "start": "21:00", "end": "07:00"}
            res = await deliver_pending(s, settings)
            return res.sent, res.suppressed

    return asyncio.run(_with_sm(go))


def _switch(group: str, on: bool) -> None:
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False)) as c:
        r = c.post("/settings/prefs/notify", json={"group": group, "on": on})
        assert r.status_code == 200


def test_a_completed_run_raises_exactly_one_run_finished_notification():
    _fresh_db(2)
    _run_once()
    notes = _notifications("run_finished")
    assert len(notes) == 1
    assert re.search(r"\d", f"{notes[0].title} {notes[0].body or ''}"), "no summary count"


def test_each_run_raises_its_own_single_notification():
    _fresh_db(2)
    _run_once()
    _run_once()
    assert len(_notifications("run_finished")) == 2


def test_a_run_with_nothing_to_score_still_says_it_finished_once():
    _fresh_db(0)
    _run_once()
    assert len(_notifications("run_finished")) == 1


def test_the_run_finished_switch_turns_off_the_runs_notification():
    _fresh_db(2)
    _switch("runs", False)
    _run_once()
    _deliver()
    notes = _notifications("run_finished")
    assert len(notes) == 1  # the bell still collects it
    assert notes[0].delivered_channels == ["suppressed"]


def test_run_finished_is_delivered_when_the_switch_is_on():
    _fresh_db(2)
    _run_once()
    sent, _suppressed = _deliver()
    assert sent == 1
    assert _notifications("run_finished")[0].delivered_channels != ["suppressed"]
    assert _notifications("run_finished")[0].delivered_channels


def test_other_switches_do_not_decide_the_run_finished_notification():
    _fresh_db(2)
    for group in ("matches", "followups", "attention"):
        _switch(group, False)
    _run_once()
    sent, _suppressed = _deliver()
    assert sent == 1
    channels = _notifications("run_finished")[0].delivered_channels
    assert channels and channels != ["suppressed"]


class _BrokenTier2(FakeLLM):
    async def complete(self, *, tier, **kw):
        if tier == "tier2":
            raise RuntimeError("model unavailable")
        return await super().complete(tier=tier, **kw)


def test_the_run_finished_notification_carries_the_failure_count():
    # D-NOTIFY-1 (#110): with failures the one outcome is a warning under Something needs
    # attention (its own kind), not the plain "Run finished" summary.
    _fresh_db(2)
    _run_once(_BrokenTier2)
    notes = _notifications("roles_need_retry")
    assert len(notes) == 1
    assert notes[0].severity == "warning"
    assert re.search(r"2 roles", notes[0].title), notes[0].title
    assert not _notifications("run_finished")
    assert not _notifications("high_score")


def test_new_roles_worth_reviewing_is_one_alert_with_its_own_switch():
    _fresh_db(1, new_in_run=2)
    _run_once()
    worth = _notifications("high_score")
    assert len(worth) == 1  # one per run, not per role
    assert worth[0].title.startswith("2 new roles")  # the older seeded role is not new
    assert len(_notifications("run_finished")) == 1  # the run summary is separate
    _switch("matches", False)
    _deliver()
    assert _notifications("high_score")[0].delivered_channels == ["suppressed"]
    assert _notifications("run_finished")[0].delivered_channels != ["suppressed"]


def test_a_notification_failure_never_fails_a_finished_run(monkeypatch):
    from kestrel.services.notifications import NotificationService

    async def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(NotificationService, "notify", boom)
    _fresh_db(1)
    _run_once()  # does not raise

    from kestrel.db.models import Run

    async def statuses(sm):
        async with sm() as s:
            return [r.status for r in (await s.execute(select(Run))).scalars()]

    assert asyncio.run(_with_sm(statuses)) == ["done"]


def test_any_notifier_failure_is_logged_and_leaves_the_run_finished(monkeypatch):
    from structlog.testing import capture_logs

    from kestrel.db.models import Run
    from kestrel.services.notifications import NotificationService

    async def boom(self, *a, **k):
        raise RuntimeError("toast backend exploded")

    monkeypatch.setattr(NotificationService, "notify", boom)
    _fresh_db(1)
    with capture_logs() as logs:
        _run_once()  # does not raise

    async def statuses(sm):
        async with sm() as s:
            return [r.status for r in (await s.execute(select(Run))).scalars()]

    assert asyncio.run(_with_sm(statuses)) == ["done"]
    failed = [e for e in logs if e["event"] == "run_finished_notification_failed"]
    assert len(failed) == 1 and failed[0]["error"] == "RuntimeError"
    assert failed[0]["run_id"] and failed[0]["exc_info"] is True


def test_the_failure_count_ignores_dead_tasks_from_earlier_runs(monkeypatch):
    """With no window from the summary, a dead task from last month must not read as a failure
    of this run."""
    from kestrel.db.models import Task

    async def no_window(session, track, paused=False):
        return {"started_at": None, "failed": 0, "worth_new": 0}

    monkeypatch.setattr("kestrel.analytics.run_summary.run_summary", no_window)
    _fresh_db(1)

    async def seed(sm):
        async with sm() as s:
            s.add(Task(type="t1", status="dead", payload={"opportunity_id": 999},
                       updated_at="2000-01-01T00:00:00+00:00"))
            await s.commit()

    asyncio.run(_with_sm(seed))
    _run_once()
    titles = [n.title for n in _notifications("run_finished")]
    assert titles == ["Run finished"], titles


def test_a_finished_run_is_listed_in_the_bell_with_a_link_to_the_run():
    """Issue #125: the bell is where the person looks. One real run, then the bell's own list
    shows the one "Run finished" entry and its way to the run details."""
    from kestrel.web.app import build_app

    _fresh_db(2)
    _run_once()
    with TestClient(build_app(worker=False)) as c:
        html = c.get("/notifications/list").text
    assert html.count("Run finished") == 1, html
    assert 'href="/runs"' in html
