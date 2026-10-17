"""The run schedule (SPEC.md 6.5, D-RUNS-1): daily or weekly, local time, once per period, only
while the app runs, every enabled source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from kestrel.db.models import Setting, Source, Task
from kestrel.queue.queue import TaskQueue
from kestrel.services import run_schedule as rs
from tests.test_hosted_mode import _fresh_app

TX = timezone(timedelta(hours=-5))  # a fixed offset standing in for the operator's clock


def _at(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=TX)


def test_parse_is_forgiving_and_never_raises():
    assert rs.parse(None) == rs.RunSchedule()
    assert rs.parse({"kind": "nonsense"}).kind == "off"
    assert rs.parse({"kind": "daily", "time": "25:00"}).kind == "off"
    assert rs.parse({"kind": "daily", "time": "7:05"}) == rs.RunSchedule("daily", "07:05", 0)
    assert rs.parse({"kind": "weekly", "time": "18:30", "weekday": "9"}).weekday == 0
    assert rs.parse({"kind": "weekly", "time": "18:30", "weekday": 4}).describe() == (
        "weekly on Friday at 18:30"
    )


def test_daily_is_due_after_the_time_and_once_per_local_day():
    daily = rs.RunSchedule("daily", "07:00")
    assert not rs.is_due(daily, _at(2026, 10, 14, 6, 59), None)
    assert rs.is_due(daily, _at(2026, 10, 14, 7, 0), None)
    assert rs.is_due(daily, _at(2026, 10, 14, 23, 0), "2026-10-13")  # missed earlier today
    assert not rs.is_due(daily, _at(2026, 10, 14, 23, 0), "2026-10-14")  # already fired
    assert rs.next_fire(daily, _at(2026, 10, 14, 8, 0), "2026-10-14") == _at(2026, 10, 15, 7, 0)
    assert rs.next_fire(daily, _at(2026, 10, 14, 6, 0), None) == _at(2026, 10, 14, 7, 0)


def test_weekly_is_due_from_the_weekday_time_and_once_per_iso_week():
    weekly = rs.RunSchedule("weekly", "09:00", weekday=2)  # Wednesday; 2026-10-14 is one
    assert not rs.is_due(weekly, _at(2026, 10, 13, 12, 0), None)  # Tuesday
    assert not rs.is_due(weekly, _at(2026, 10, 14, 8, 59), None)
    assert rs.is_due(weekly, _at(2026, 10, 14, 9, 0), None)
    assert rs.is_due(weekly, _at(2026, 10, 16, 9, 0), "2026-W41")  # Friday, missed this week
    assert not rs.is_due(weekly, _at(2026, 10, 16, 9, 0), "2026-W42")
    assert rs.period_key(weekly, _at(2026, 10, 16, 9, 0)) == "2026-W42"
    assert rs.next_fire(weekly, _at(2026, 10, 16, 9, 0), "2026-W42") == _at(2026, 10, 21, 9, 0)


def test_off_is_never_due():
    assert not rs.is_due(rs.RunSchedule(), _at(2026, 10, 14, 12, 0), None)
    assert rs.next_fire(rs.RunSchedule(), _at(2026, 10, 14, 12, 0), None) is None


async def test_fire_enqueues_every_enabled_source_once_per_period(sm):
    async with sm() as s:
        s.add_all(
            [
                Source(name="a", kind="greenhouse", track="employment", enabled=True, config={}),
                Source(name="b", kind="lever", track="employment", enabled=True, config={}),
                Source(name="off", kind="lever", track="employment", enabled=False, config={}),
            ]
        )
        await s.commit()
    queue = TaskQueue(sm)
    async with sm() as s:
        await rs.save(s, rs.RunSchedule("daily", "07:00"))
        await s.commit()
        assert await rs.fire_if_due(s, queue, now=_at(2026, 10, 14, 6, 0)) is None
        assert await rs.fire_if_due(s, queue, now=_at(2026, 10, 14, 7, 30)) == 2
        assert await rs.fire_if_due(s, queue, now=_at(2026, 10, 14, 9, 0)) is None  # same day
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == "scrape"))).scalars().all()
        rows = dict((await s.execute(select(Setting.key, Setting.value))).all())
    assert len(tasks) == 2
    assert rows[rs.LAST_KEY] == "2026-10-14" and "scheduler.enabled" not in rows
    # Next day: pending scrapes from yesterday are not doubled; a drained one is re-enqueued.
    async with sm() as s:
        assert await rs.fire_if_due(s, queue, now=_at(2026, 10, 15, 7, 0)) == 0


async def test_fire_uses_the_local_clock_by_default(sm):
    before = rs.local_now().date().isoformat()
    async with sm() as s:
        await rs.save(s, rs.RunSchedule("daily", "00:00"))
        await s.commit()
        fired = await rs.fire_if_due(s, TaskQueue(sm))
    after = rs.local_now().date().isoformat()
    assert fired == 0  # due (midnight has passed), no sources
    async with sm() as s:
        last = (await s.get(Setting, rs.LAST_KEY)).value
    assert last in (before, after)  # robust across a midnight that falls inside the test


def test_runs_page_has_the_schedule_panel_and_saves_it(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    page = client.get("/runs").text
    assert 'data-testid="run-schedule"' in page and 'value="off" checked' in page
    saved = client.post(
        "/runs/schedule", data={"kind": "weekly", "weekday": "4", "time": "18:30"}
    ).text
    assert "Saved: weekly on Friday at 18:30; next Friday" in saved
    assert 'value="weekly" checked' in saved and 'value="4" selected' in saved
    page = client.get("/runs").text
    assert "weekly on Friday at 18:30" in page
    cleared = client.post("/runs/schedule", data={"kind": "off"}).text
    assert "Saved: off." in cleared
    assert "—" not in saved
