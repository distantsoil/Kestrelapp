"""Values and validation for the settings the interface owns (SPEC.md 4.6, D-UI-92 to D-UI-129):
scene, seasonal touches, avatar, notification sound, quiet hours, region formats, text size.

Two ways to read a value, one rule each:

* The strict normalizers here (`scene`, `touches_off`, ...) are what a WRITE runs: an unknown
  choice is a `ValueError` with a plain message, so a bad form post is refused.
* `lenient(fn, default)` wraps one for the config layer, which READS stored rows on every request:
  a stored value that no longer validates (a scene removed in a later version, a hand-edited
  row) falls back to the default instead of failing the whole settings load. Numbers are clamped,
  never rejected.

This module imports nothing from the rest of Kestrel, so `kestrel.config` can use it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

# ------------------------------------------------------------------------------------ scenes
SCENES: tuple[str, ...] = (
    "mountains", "coast", "desert", "prairie", "forest", "city", "lake", "farmland",
    "winter", "canyon", "island", "aurora", "rain", "savanna",
)
NO_SCENE = "none"
DEFAULT_SCENE = SCENES[0]

# --------------------------------------------------------------------------- seasonal touches
# id -> group (SPEC-INTERFACES 2.5). Neutral by rule: ids are plain words, no festival names.
TOUCH_GROUPS: dict[str, tuple[str, ...]] = {
    "seasons": (
        "spring", "earth_day", "longest_day", "shooting_stars", "autumn", "harvest",
        "longest_night",
    ),
    "civic": ("new_year", "fourth_of_july", "halloween", "lunar_new_year", "labor_day"),
    "lights": ("colors_of_spring", "crescent_evenings", "lantern_night", "winter_lights"),
}
TOUCHES: tuple[str, ...] = tuple(t for group in TOUCH_GROUPS.values() for t in group)
TOUCH_GROUP_IDS: tuple[str, ...] = tuple(TOUCH_GROUPS)

# ------------------------------------------------------------------------------------ avatar
AVATARS: tuple[str, ...] = (
    "initial", "bird", "leaf", "star", "sun", "mountain", "compass", "photo",
)

# ------------------------------------------------------------------------------------- sound
SOUNDS: tuple[str, ...] = ("chime", "soft_click", "bell")
DEFAULT_SOUND = "chime"
DEFAULT_VOLUME = 0.6

# ------------------------------------------------------------------------------------ region
# Country (US) and currency (USD) are fixed, not settings (D-UI-109).
DATE_FORMATS: tuple[str, ...] = (
    "MM/DD/YYYY", "MM/DD/YY", "DD/MM/YYYY", "DD/MM/YY", "YYYY-MM-DD", "Mon D, YYYY",
)
DEFAULT_DATE_FORMAT = DATE_FORMATS[0]
TIME_FORMATS: tuple[str, ...] = ("12h", "24h")
TEXT_SIZES: tuple[str, ...] = ("small", "default", "large")

DEFAULT_QUIET_START = "21:00"
DEFAULT_QUIET_END = "07:00"


def _choice(value: Any, allowed: tuple[str, ...], what: str) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    raise ValueError(f"{what} must be one of: {', '.join(allowed)}.")


def lenient(fn: Callable[[Any], Any], default: Any) -> Callable[[Any], Any]:
    """Wrap a strict normalizer for the config layer: a value that fails reads as `default`."""

    def read(value: Any) -> Any:
        try:
            return fn(value)
        except (ValueError, TypeError):
            return default() if callable(default) else default

    return read


def scene(value: Any) -> str:
    return _choice(value, (*SCENES, NO_SCENE), "Scene")


def scene_rotation(value: Any) -> list[str]:
    """The checked scenes, in library order and without repeats. At least one stays checked."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("The scene list must be a list.")
    for item in value:
        _choice(item, SCENES, "A scene in the list")
    chosen = [s for s in SCENES if s in value]
    if not chosen:
        raise ValueError("At least one scene must stay checked.")
    return chosen


def flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError("This setting is on or off.")


def touches_off(value: Any) -> list[str]:
    """The touch ids turned off, in library order. Empty means every touch is on."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("The touch list must be a list.")
    for item in value:
        _choice(item, TOUCHES, "A touch")
    return [t for t in TOUCHES if t in value]


def touch_groups_off(value: Any) -> list[str]:
    """The touch groups turned off as a whole (all their touches), in library order."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("The touch group list must be a list.")
    for item in value:
        _choice(item, TOUCH_GROUP_IDS, "A touch group")
    return [g for g in TOUCH_GROUP_IDS if g in value]


def avatar(value: Any) -> str:
    return _choice(value, AVATARS, "Avatar")


def sound_name(value: Any) -> str:
    return _choice(value, SOUNDS, "Sound")


def sound_volume(value: Any) -> float:
    """A volume from 0 to 1. Out-of-range numbers are clamped, never refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Volume must be a number from 0 to 1.")
    if value != value:  # NaN
        raise ValueError("Volume must be a number from 0 to 1.")
    return round(min(1.0, max(0.0, float(value))), 3)


def date_format(value: Any) -> str:
    return _choice(value, DATE_FORMATS, "Date format")


def time_format(value: Any) -> str:
    return _choice(value, TIME_FORMATS, "Time format")


def text_size(value: Any) -> str:
    return _choice(value, TEXT_SIZES, "Text size")


def clock_text(value: Any) -> str:
    """`HH:MM` (24 hour) as zero-padded text."""
    if isinstance(value, str):
        head, sep, tail = value.strip().partition(":")
        if sep and head.isdigit() and tail.isdigit() and len(tail) == 2:
            h, m = int(head), int(tail)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
    raise ValueError("A time must be written HH:MM, for example 21:00.")


def quiet_hours(value: Any) -> dict[str, Any]:
    """`{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`. A night such as 21:00 to 07:00 is
    one span (see `in_quiet_hours`); start equal to end is an empty span."""
    if not isinstance(value, dict):
        raise ValueError("Quiet hours must be a start and an end.")
    return {
        "enabled": flag(value.get("enabled", False)),
        "start": clock_text(value.get("start", DEFAULT_QUIET_START)),
        "end": clock_text(value.get("end", DEFAULT_QUIET_END)),
    }


def default_quiet_hours() -> dict[str, Any]:
    return {"enabled": False, "start": DEFAULT_QUIET_START, "end": DEFAULT_QUIET_END}


def in_quiet_hours(hours: dict[str, Any], now: time) -> bool:
    """Whether `now` (local wall-clock time) falls in the quiet span. A span that crosses
    midnight (21:00 to 07:00) counts as one night."""
    if not hours.get("enabled"):
        return False
    start = time.fromisoformat(clock_text(hours.get("start")))
    end = time.fromisoformat(clock_text(hours.get("end")))
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


# ------------------------------------------------------------------------------- derived use
def scene_for_day(
    chosen: str, daily: bool, rotation: list[str], day: date
) -> str:
    """The scene to show on a local `day`. Change daily cycles the checked set by date, so the
    answer is stable all day and moves at local midnight; otherwise the one chosen scene shows."""
    if not daily or not rotation:
        return chosen
    return rotation[day.toordinal() % len(rotation)]


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date(day: date | datetime, fmt: str = DEFAULT_DATE_FORMAT) -> str:
    """A date in the operator's chosen format (Region, D-UI-109)."""
    fmt = fmt if fmt in DATE_FORMATS else DEFAULT_DATE_FORMAT
    d, m, y = day.day, day.month, day.year
    if fmt == "MM/DD/YYYY":
        return f"{m:02d}/{d:02d}/{y:04d}"
    if fmt == "MM/DD/YY":
        return f"{m:02d}/{d:02d}/{y % 100:02d}"
    if fmt == "DD/MM/YYYY":
        return f"{d:02d}/{m:02d}/{y:04d}"
    if fmt == "DD/MM/YY":
        return f"{d:02d}/{m:02d}/{y % 100:02d}"
    if fmt == "YYYY-MM-DD":
        return f"{y:04d}-{m:02d}-{d:02d}"
    return f"{_MONTHS[m - 1]} {d}, {y}"


def format_time(moment: time | datetime, fmt: str = "12h") -> str:
    """A time of day as `9:05 PM` (12h) or `21:05` (24h)."""
    if fmt == "24h":
        return f"{moment.hour:02d}:{moment.minute:02d}"
    hour12 = moment.hour % 12 or 12
    return f"{hour12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


# ----------------------------------------------------------------------- seasonal touch dates
# Each touch shows only inside its own window (SPEC-INTERFACES 2.5). The lunar and lunisolar
# windows come from a table bundled here, one entry per year; a year not in the table shows no
# touch for those entries (never a wrong date). Labor Day follows the US, the only region for now.
_LUNAR_NEW_YEAR = {
    2026: date(2026, 2, 17), 2027: date(2027, 2, 6), 2028: date(2028, 1, 26),
    2029: date(2029, 2, 13), 2030: date(2030, 2, 3), 2031: date(2031, 1, 23),
    2032: date(2032, 2, 11), 2033: date(2033, 1, 31), 2034: date(2034, 2, 19),
    2035: date(2035, 2, 8),
}
_CRESCENT_START = {
    2026: date(2026, 2, 18), 2027: date(2027, 2, 8), 2028: date(2028, 1, 28),
    2029: date(2029, 1, 16), 2030: date(2030, 1, 6),
}
_LANTERN_NIGHT = {
    2026: date(2026, 9, 25), 2027: date(2027, 9, 15), 2028: date(2028, 10, 3),
    2029: date(2029, 9, 22), 2030: date(2030, 9, 12), 2031: date(2031, 10, 1),
    2032: date(2032, 9, 19), 2033: date(2033, 9, 8), 2034: date(2034, 9, 27),
    2035: date(2035, 9, 16),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _around(day: date, month: int, dom: int, before: int = 2, after: int = 2) -> bool:
    centre = date(day.year, month, dom)
    return centre - timedelta(days=before) <= day <= centre + timedelta(days=after)


def _in(day: date, start: date | None, end: date | None) -> bool:
    return start is not None and end is not None and start <= day <= end


# Most specific first: when two windows overlap, the earlier entry is the one drawn.
_TOUCH_ORDER: tuple[str, ...] = (
    "new_year", "halloween", "fourth_of_july", "labor_day", "harvest", "lunar_new_year",
    "lantern_night", "crescent_evenings", "longest_night", "winter_lights", "shooting_stars",
    "longest_day", "earth_day", "autumn", "spring", "colors_of_spring",
)


def touch_active(touch: str, day: date) -> bool:
    """Whether `touch` (a TOUCHES id) is inside its window on `day`."""
    y = day.year
    if touch == "new_year":
        return (day.month, day.day) in ((12, 31), (1, 1))
    if touch == "halloween":
        return _around(day, 10, 31, before=2, after=0)
    if touch == "fourth_of_july":
        return _around(day, 7, 4, before=1, after=1)
    if touch == "labor_day":
        mon = _nth_weekday(y, 9, 0, 1)
        return mon - timedelta(days=3) <= day <= mon
    if touch == "harvest":
        thanks = _nth_weekday(y, 11, 3, 4)
        return thanks - timedelta(days=3) <= day <= thanks + timedelta(days=3)
    if touch == "lunar_new_year":
        ny = _LUNAR_NEW_YEAR.get(y)
        return ny is not None and ny - timedelta(days=1) <= day <= ny + timedelta(days=14)
    if touch == "lantern_night":
        ln = _LANTERN_NIGHT.get(y)
        return ln is not None and ln - timedelta(days=2) <= day <= ln + timedelta(days=2)
    if touch == "crescent_evenings":
        start = _CRESCENT_START.get(y)
        return start is not None and _in(day, start, start + timedelta(days=29))
    if touch == "longest_night":
        return _around(day, 12, 21)
    if touch == "winter_lights":
        return (day.month == 12 and day.day >= 10) or (day.month == 1 and day.day <= 2)
    if touch == "shooting_stars":
        return _around(day, 8, 12)
    if touch == "longest_day":
        return _around(day, 6, 21)
    if touch == "earth_day":
        return _around(day, 4, 22)
    if touch == "autumn":
        return _around(day, 9, 22)
    if touch == "spring":
        return _around(day, 3, 20)
    if touch == "colors_of_spring":
        return day.month == 3
    return False


def touch_for_day(
    day: date, enabled: bool, groups_off: list[str], off: list[str]
) -> str | None:
    """The one touch id to draw on a local `day`, or None. The master switch off, a group off
    (all its touches), or a single touch off each remove it from the candidates."""
    if not enabled:
        return None
    blocked = set(off)
    for group in groups_off:
        blocked.update(TOUCH_GROUPS.get(group, ()))
    for touch in _TOUCH_ORDER:
        if touch not in blocked and touch_active(touch, day):
            return touch
    return None
