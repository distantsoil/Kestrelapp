"""Seeded "a few minutes ago" timestamps that never cross the period a test checks (D-TEST-2).

A row seeded at `now - 5 minutes` and then counted as "today" fails for the first five minutes
of every day; one seeded `0.1 days` ago and counted as "this month" fails for 2.4 hours on the
1st. Windows CI runs on UTC and hit both (public PR 24). `ago_within` keeps the gap the test asked
for whenever that still lands inside the current period. Otherwise a gap below `horizon` is
clamped to a fixed point just after the period start, `start + (horizon - delta) / 600` (at most
`horizon / 600` in), so seeded rows land strictly after the start and keep the order asked, even
across separate seeding calls (the point does not move with the clock). A gap at or above
`horizon` is left alone: those rows are meant to be old (yesterday, last month).

`datetime` is read from this module's globals so a test can freeze the clock by patching it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def local_day_start(now: datetime) -> datetime:
    """Midnight today on this machine's clock, in UTC: the app's own "today" (D-UI-9)."""
    local = now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def utc_month_start(now: datetime) -> datetime:
    """00:00 UTC on the 1st of the current UTC month: hosted spend's "this month"."""
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def ago_within(delta: timedelta, period_start: datetime, *, horizon: timedelta,
               now: datetime | None = None) -> datetime:
    """`now - delta`, clamped to a fixed point just inside the current period when it would fall
    before `period_start` and `delta` is below `horizon`."""
    now = now or datetime.now(UTC)
    if delta >= horizon:
        return now - delta
    return max(now - delta, period_start + (horizon - delta) / 600)


# The instants the boundary tests freeze the clock at: one minute into the 1st of a month in UTC
# (what Windows CI runs on) and on this machine's own clock (where "today" is decided).
JUST_AFTER_MIDNIGHT_ON_THE_1ST = (
    datetime(2026, 11, 1, 0, 1, tzinfo=UTC),
    datetime(2026, 11, 1, 0, 1).astimezone().astimezone(UTC),
)


def frozen_datetime(at: datetime) -> type[datetime]:
    """A `datetime` whose `now()` is always `at`; patch it over a module's `datetime` name."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime.now
            return at.astimezone(tz) if tz else at.astimezone().replace(tzinfo=None)

    return _Frozen
