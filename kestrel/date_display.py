"""Region-aware display only. Stored timestamps, form values and exports stay machine-readable."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from contextvars import ContextVar
from datetime import date, datetime, time
from typing import Literal

from kestrel import paths, ui_settings
from kestrel.config import Region, get_settings

current_region: ContextVar[Region | None] = ContextVar("display_region", default=None)


def read_region() -> Region:
    import yaml

    try:
        region = get_settings().region
    except (ValueError, OSError, yaml.YAMLError):
        region = Region()
    values = region.model_dump()
    db = paths.db_path()
    if db.exists():
        try:
            with closing(sqlite3.connect(str(db), timeout=5.0)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=5000")
                rows = connection.execute(
                    "SELECT key, value FROM setting WHERE key IN (?, ?)",
                    ("region.date_format", "region.time_format"),
                ).fetchall()
            for key, raw in rows:
                try:
                    values[key.removeprefix("region.")] = json.loads(raw)
                except (ValueError, TypeError):
                    continue
        except sqlite3.Error:
            pass
    return Region.model_validate(values)


def format_display(
    value: date | datetime | time | str | None,
    date_style: str = "iso",
    *,
    kind: Literal["date", "time", "datetime"] = "date",
    time_style: str = "12h",
    separator: str = " ",
    local: bool = False,
    fallback: str = "",
    date_format: str | None = None,
    time_format: str | None = None,
) -> str:
    """Keep each surface's legacy default; a nondefault Region choice overrides its style.

    Calendar dates and appointments keep their written day/time. Only instant displays request
    local timezone conversion. A request snapshots preferences once, including HTMX fragments.
    """
    if value is None or value == "":
        return fallback
    original = value
    try:
        if isinstance(value, str):
            value = (time.fromisoformat(value) if kind == "time" and len(value) <= 8
                     else datetime.fromisoformat(value.replace("Z", "+00:00")))
        if local and isinstance(value, datetime):
            value = value.astimezone()
    except (ValueError, TypeError, OverflowError):
        return fallback
    region = current_region.get() or read_region()
    if date_style == "raw":
        if region == Region():
            return str(original)
        kind, date_style, time_style = "datetime", "iso", "24h"
    chosen_date = date_format or region.date_format
    day_text = ""
    clock = ""
    if kind != "time" and isinstance(value, date):
        if chosen_date != ui_settings.DEFAULT_DATE_FORMAT or date_style == "region":
            day_text = ui_settings.format_date(value, chosen_date)
        else:
            short = f"{value:%b} {value.day}"
            styles = {
                "iso": (value.date().isoformat() if isinstance(value, datetime)
                        else value.isoformat()),
                "short": short,
                "short_year": f"{short}, {value.year}",
                "short_year_optional": short if value.year == date.today().year
                else f"{short}, {value.year}",
                "weekday": f"{value:%a}",
                "weekday_short": f"{value:%a %b} {value.day}",
                "weekday_comma": f"{value:%a, %b} {value.day}",
                "weekday_long": f"{value:%A, %B} {value.day}",
                "weekday_iso": f"{value:%A %Y-%m-%d}",
                "month_upper": f"{value:%b}".upper(),
                "day": str(value.day),
                "custom_only": "",
            }
            day_text = styles.get(date_style, short)
    if kind != "date" and isinstance(value, (datetime, time)):
        chosen_time = (time_format or
                       (region.time_format if region.time_format != "12h" else time_style))
        clock = ui_settings.format_time(value, chosen_time)
        if time_style == "24h_seconds" and region.time_format == "12h":
            clock = f"{value:%H:%M:%S}"
    return separator.join(part for part in (day_text, clock) if part)
