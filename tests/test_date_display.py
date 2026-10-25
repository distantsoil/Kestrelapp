from __future__ import annotations

from datetime import datetime

import pytest

from kestrel.config import Region
from kestrel.date_display import current_region, format_display


@pytest.mark.parametrize(("style", "expected"), [
    ("iso", "2026-10-24"), ("short", "Oct 24"), ("short_year", "Oct 24, 2026"),
    ("weekday", "Sat"), ("weekday_short", "Sat Oct 24"),
    ("weekday_comma", "Sat, Oct 24"), ("weekday_long", "Saturday, October 24"),
    ("weekday_iso", "Saturday 2026-10-24"), ("region", "10/24/2026"),
])
def test_default_keeps_existing_date_styles(style, expected):
    token = current_region.set(Region())
    try:
        assert format_display(datetime(2026, 10, 24, 21, 5), style) == expected
    finally:
        current_region.reset(token)


@pytest.mark.parametrize("style", ["iso", "short", "short_year", "weekday", "weekday_long"])
@pytest.mark.parametrize(("chosen", "expected"), [
    ("MM/DD/YY", "10/24/26"), ("DD/MM/YYYY", "24/10/2026"),
    ("DD/MM/YY", "24/10/26"), ("YYYY-MM-DD", "2026-10-24"),
    ("Mon D, YYYY", "Oct 24, 2026"),
])
def test_saved_nondefault_date_wins_over_each_surface_style(style, chosen, expected):
    token = current_region.set(Region(date_format=chosen))
    try:
        assert format_display("2026-10-24T21:05:00", style) == expected
    finally:
        current_region.reset(token)


@pytest.mark.parametrize(("chosen", "legacy", "expected"), [
    ("12h", "12h", "9:05 PM"), ("12h", "24h", "21:05"),
    ("24h", "12h", "21:05"), ("24h", "24h", "21:05"),
])
def test_time_preference_and_legacy_default_are_independent(chosen, legacy, expected):
    token = current_region.set(Region(time_format=chosen))
    try:
        assert format_display("21:05", kind="time", time_style=legacy) == expected
    finally:
        current_region.reset(token)


@pytest.mark.parametrize("value", [None, "", "invalid", "2026-99-99"])
def test_unusable_dates_have_a_safe_fallback(value):
    assert format_display(value, fallback="unknown") == "unknown"


def test_calendar_dates_do_not_shift_timezone_but_instants_can():
    from datetime import UTC, timedelta, timezone

    moment = datetime(2026, 10, 24, 0, 5, tzinfo=timezone(timedelta(hours=14)))
    token = current_region.set(Region(date_format="DD/MM/YYYY"))
    try:
        assert format_display(moment) == "24/10/2026"
        instant = moment.astimezone(UTC)
        assert format_display(instant) == "23/10/2026"
    finally:
        current_region.reset(token)


def test_preferences_follow_database_changes_and_invalid_values_use_defaults():
    import json
    import sqlite3

    from kestrel import paths
    from kestrel.date_display import read_region
    from kestrel.db import migrate

    migrate.run_migrations()
    with sqlite3.connect(paths.db_path()) as connection:
        connection.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
                           ("region.date_format", json.dumps("DD/MM/YYYY"), "now"))
        connection.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)",
                           ("region.time_format", json.dumps("24h"), "now"))
    assert read_region() == Region(date_format="DD/MM/YYYY", time_format="24h")
    with sqlite3.connect(paths.db_path()) as connection:
        connection.execute("UPDATE setting SET value = ?", (json.dumps("invalid"),))
    assert read_region() == Region()


@pytest.mark.parametrize("custom", [False, True])
def test_existing_screen_helpers_share_preferences_and_keep_default_words(custom):
    from kestrel.analytics.run_summary import local_when
    from kestrel.services.pursuit_board import clock_label, short_date
    from kestrel.web.bench_view import date_words
    from kestrel.web.company_view import short_date as company_date
    from kestrel.web.prep_view import when_parts

    moment = datetime(2026, 10, 24, 21, 5).astimezone()
    stamp = moment.isoformat()
    token = current_region.set(Region(date_format="DD/MM/YYYY", time_format="24h")
                               if custom else Region())
    try:
        assert short_date(stamp) == ("24/10/2026" if custom else "Oct 24")
        assert company_date(stamp, now=moment.replace(day=25)) == (
            "24/10/2026" if custom else "Oct 24")
        assert date_words(stamp) == ("24/10/2026" if custom else "Oct 24")
        assert clock_label(stamp) == ("21:05" if custom else "9:05 PM")
        assert when_parts(stamp)["label"] == (
            "24/10/2026 · 21:05" if custom else "Sat, Oct 24 · 9:05 PM")
        assert local_when(stamp) == (
            "24/10/2026, 21:05" if custom else "Sat Oct 24, 9:05 PM")
    finally:
        current_region.reset(token)
