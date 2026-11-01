"""`clock_seed.ago_within` (D-TEST-2): seeded "a few minutes ago" rows never cross the period a
test counts them in, and keep the order they were seeded in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests import clock_seed

H = timedelta(minutes=10)
START = datetime(2026, 11, 1, tzinfo=UTC)


def test_an_old_enough_period_keeps_the_exact_gap():
    now = START + timedelta(hours=3)
    assert clock_seed.ago_within(timedelta(minutes=5), START, horizon=H, now=now) == (
        now - timedelta(minutes=5))


def test_just_after_the_start_gaps_clamp_in_order_and_stay_inside():
    now = START + timedelta(minutes=1)
    got = [clock_seed.ago_within(timedelta(minutes=m), START, horizon=H, now=now)
           for m in (5, 4, 3)]
    assert START < got[0] < got[1] < got[2] < now
    # The clamp does not move with the clock: a later call keeps the earlier order.
    later = clock_seed.ago_within(timedelta(minutes=4), START, horizon=H,
                                  now=now + timedelta(seconds=30))
    assert got[0] < later < got[2]


def test_a_gap_at_or_past_the_horizon_is_left_in_the_earlier_period():
    now = START + timedelta(minutes=1)
    assert clock_seed.ago_within(timedelta(days=2), START, horizon=H, now=now) == (
        now - timedelta(days=2))


def test_the_period_starts():
    at = datetime(2026, 11, 17, 13, 45, tzinfo=UTC)
    assert clock_seed.utc_month_start(at) == START
    day = clock_seed.local_day_start(at)
    assert day.tzinfo is not None and day <= at < day + timedelta(days=1, hours=1)
    assert clock_seed.frozen_datetime(at).now(UTC) == at
