"""The boundary-run plugin freezes the test files' own clock, not only the app's (#116).

Test files are imported by basename (tests/ has no __init__.py), so a filter on the module name
alone froze `kestrel.*` at 00:03 while each test's own `datetime.now()` read the real clock: rows
seeded "13 hours ago" landed 8 hours before the app's frozen now and five Today tests failed."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import kestrel.analytics.today as today
from tests import boundary_clock


def test_a_test_module_imported_by_basename_counts_as_ours():
    me = sys.modules[__name__]
    assert __name__.split(".")[0] not in {"kestrel", "tests"}  # why the name alone is not enough
    assert boundary_clock._is_ours(__name__, me)


def test_app_modules_count_and_outside_modules_do_not():
    assert boundary_clock._is_ours(today.__name__, today)
    assert not boundary_clock._is_ours(json.__name__, json)
    assert not boundary_clock._is_ours("builtins", sys.modules["builtins"])


def test_the_fixture_freezes_a_basename_imported_test_module(monkeypatch):
    # The behavior, not just the predicate: this module's own `datetime` reads the frozen clock.
    from datetime import UTC, datetime

    me = sys.modules[__name__]
    monkeypatch.setattr(me, "datetime", datetime, raising=False)
    # Both start on the real clock, even when the stress run's clock plugin already swapped one
    # (freeze_modules rebinds only modules that hold the real class; D-CI-26).
    monkeypatch.setattr(today, "datetime", datetime)
    at = datetime(2026, 3, 1, 0, 3, tzinfo=UTC)
    boundary_clock.freeze_modules(monkeypatch, at)
    assert me.datetime.now(UTC) - at < timedelta(minutes=1)
    assert today.datetime.now(UTC) - at < timedelta(minutes=1)


def test_the_fixture_freezes_date_today_with_the_same_clock(monkeypatch):
    # The app counts "today" with date.today() in 32 places; seeds read datetime.now(). Under a
    # frozen clock they must agree, or a row seeded 30 days back reads 31 near midnight.
    from datetime import UTC, date, datetime

    monkeypatch.setattr(today, "datetime", datetime)
    monkeypatch.setattr(today, "date", date)
    at = datetime(2026, 3, 1, 0, 3, tzinfo=UTC)
    boundary_clock.freeze_modules(monkeypatch, at)
    assert today.date.today() == today.datetime.now().date() == at.astimezone().date()
    assert isinstance(date(2026, 1, 1), today.date)  # real dates still count as dates


def test_the_fixture_shifts_time_time_but_not_monotonic(monkeypatch):
    # Code that stamps with datetime and compares with time.time() must see one clock (the
    # embed re-sweep failed at 10x when only datetime ran fast); deadlines stay on monotonic.
    import time as real_time
    from datetime import UTC, datetime

    from kestrel.pipeline import embed

    monkeypatch.setattr(embed, "time", real_time)
    at = datetime(2026, 3, 1, 0, 3, tzinfo=UTC)
    boundary_clock.freeze_modules(monkeypatch, at, rate=10)
    assert abs(embed.time.time() - at.timestamp()) < 60
    assert abs(embed.time.monotonic() - real_time.monotonic()) < 5


def test_the_instants_are_the_most_recent_past_ones():
    from datetime import UTC, datetime

    from tests.test_clock_boundaries import _instants

    # A gate started 30 s into a UTC month: last month's 1st, never a future instant.
    now = datetime(2026, 12, 1, 0, 0, 30, tzinfo=UTC)
    got = _instants(now)
    assert got["00:01 UTC on the 1st"] == datetime(2026, 11, 1, 0, 1, tzinfo=UTC)
    assert all(v <= now for v in got.values())
    later = datetime(2026, 12, 2, 12, 0, tzinfo=UTC)
    assert all(v <= later for v in _instants(later).values())


def test_local_days_ago_counts_calendar_days_across_daylight_saving(monkeypatch):
    # T14: 23:17 CST on the night DST ended; 30 x 24 h back is only 29 local calendar days.
    import time

    import pytest

    if not hasattr(time, "tzset"):
        pytest.skip("TZ switching is POSIX only")
    from datetime import UTC, datetime

    from tests.clock_seed import local_days_ago

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        now = datetime(2026, 11, 2, 5, 17, tzinfo=UTC)  # 23:17 CST, Nov 1
        seeded = local_days_ago(30, now).astimezone().date()
        assert (now.astimezone().date() - seeded).days == 30
        assert ((now - timedelta(days=30)).astimezone().date() - seeded).days == 1  # the old seed
    finally:
        # Restore the zone the process had (not just drop TZ) and re-read it.
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
