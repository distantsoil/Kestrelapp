"""The boundary-run plugin freezes the test files' own clock, not only the app's (#116).

Test files are imported by basename (tests/ has no __init__.py), so a filter on the module name
alone froze `kestrel.*` at 00:03 while each test's own `datetime.now()` read the real clock: rows
seeded "13 hours ago" landed 8 hours before the app's frozen now and five Today tests failed."""

from __future__ import annotations

import json
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
    at = datetime(2026, 3, 1, 0, 3, tzinfo=UTC)
    boundary_clock.freeze_modules(monkeypatch, at)
    assert me.datetime.now(UTC) - at < timedelta(minutes=1)
    assert today.datetime.now(UTC) - at < timedelta(minutes=1)


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
