"""pytest plugin: run a test session with the wall clock frozen (D-TEST-3). Opt-in only.

Loaded by `test_clock_boundaries.py` as `-p tests.boundary_clock` with `KESTREL_FROZEN_AT` set to
an ISO instant, and by the stress run (scripts/stress.py) with that and/or `KESTREL_CLOCK_RATE`
(the clock runs that many times real speed from now). No freezegun in the stack, so this rebinds
the `datetime` name of every already imported `kestrel` or `tests` module to a subclass whose
`now()`/`utcnow()` start at the chosen instant and then advance at that speed (a clock that never
moves hangs deadline loops), their `date` name to one whose `today()` reads that clock, and their
`time` module name to one whose `time()` reads it, so every wall clock a module reads agrees
(D-TEST-26); `time.monotonic()` and `sleep()` stay real. Limits: the `datetime.datetime` class
itself is left alone (swapping it hangs C extensions such as pandas on import), so a `from datetime
import datetime` run inside a function, or `from time import time`, still reads the real clock (the
honest limit of this approach)."""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
from pathlib import Path

import pytest

_REAL = _dt.datetime
_REAL_DATE = _dt.date
_REAL_TIME = time
_ROOT = Path(__file__).resolve().parent.parent
_OURS = (_ROOT / "kestrel", _ROOT / "tests")


def _is_ours(name: str, mod: object) -> bool:
    """A `kestrel` or test module. Test files are imported by basename (`test_today`, not
    `tests.test_today`: tests/ has no __init__.py), so the file path decides, not the name."""
    if name.split(".")[0] in {"kestrel", "tests"}:
        return True
    file = getattr(mod, "__file__", None)
    if not file:  # Path("") would resolve to the working directory
        return False
    try:
        path = Path(file).resolve()
    except (OSError, ValueError):
        return False
    return any(path.is_relative_to(base) for base in _OURS)


class _Meta(type):
    def __instancecheck__(cls, obj: object) -> bool:  # real datetimes still count
        return isinstance(obj, _REAL)


def _frozen(at: _REAL, rate: float = 1.0) -> type:
    """A class whose `now()` starts at `at` and then runs at `rate` times real speed, so deadline
    loops and timeouts still finish (a clock that never advances hangs them). A rate above 1 is
    the stress run's slow machine (D-CI-26): more time passes between seeding a row and reading
    it than the test's author saw."""
    started = time.monotonic()

    def current() -> _REAL:
        return at + _dt.timedelta(seconds=(time.monotonic() - started) * rate)

    class Frozen(_REAL, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return current().astimezone(tz) if tz else current().astimezone().replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # noqa: ANN206
            return current().astimezone(_dt.UTC).replace(tzinfo=None)

    return Frozen


def _rate(raw: str | None) -> float:
    """KESTREL_CLOCK_RATE as a positive number; anything else stops the run with its value."""
    try:
        rate = float(raw or 1)
    except ValueError:
        rate = 0.0
    if not rate > 0:
        raise pytest.UsageError(f"KESTREL_CLOCK_RATE must be a positive number, not {raw!r}")
    return rate


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    raw = os.environ.get("KESTREL_FROZEN_AT")
    rate = _rate(os.environ.get("KESTREL_CLOCK_RATE"))
    if not raw and rate == 1:
        yield
        return
    at = _REAL.fromisoformat(raw) if raw else _REAL.now(_dt.UTC)
    freeze_modules(monkeypatch, at, rate)
    yield


class _DateMeta(type):
    def __instancecheck__(cls, obj: object) -> bool:  # real dates still count
        return isinstance(obj, _REAL_DATE)


def _frozen_date(clock: type) -> type:
    """A `date` whose `today()` is the local date of the frozen clock, so code that reads
    `date.today()` agrees with code that reads `datetime.now()` (D-CI-26)."""

    class FrozenDate(_REAL_DATE, metaclass=_DateMeta):
        @classmethod
        def today(cls):  # noqa: ANN206
            return clock.now().date()

    return FrozenDate


class _TimeShim:
    """The `time` module with `time()` read from the frozen clock. Everything else, monotonic
    and sleep included, is the real module, so deadline loops still finish."""

    def __init__(self, clock: type) -> None:
        self._clock = clock

    def time(self) -> float:
        return self._clock.now(_dt.UTC).timestamp()

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(_REAL_TIME, name)


def freeze_modules(monkeypatch, at: _REAL, rate: float = 1.0) -> type:  # noqa: ANN001
    """Rebind `datetime`, `date` (for `date.today()`) and `time` (for `time.time()`) in every
    loaded module of ours, so every wall clock it reads agrees (D-TEST-26)."""
    frozen = _frozen(at, rate)
    frozen_date = _frozen_date(frozen)
    shim = _TimeShim(frozen)
    for name, mod in list(sys.modules.items()):
        if not _is_ours(name, mod) or mod is sys.modules.get(__name__):
            continue
        if getattr(mod, "time", None) is _REAL_TIME:
            monkeypatch.setattr(mod, "time", shim)
        if getattr(mod, "datetime", None) is _REAL:
            monkeypatch.setattr(mod, "datetime", frozen)
        if getattr(mod, "date", None) is _REAL_DATE:
            monkeypatch.setattr(mod, "date", frozen_date)
    return frozen
