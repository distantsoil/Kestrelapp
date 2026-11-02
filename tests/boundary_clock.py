"""pytest plugin: run a test session with the wall clock frozen (D-TEST-3). Opt-in only.

Loaded by `test_clock_boundaries.py` as `-p tests.boundary_clock` with `KESTREL_FROZEN_AT` set to
an ISO instant. No freezegun in the stack, so this rebinds the `datetime` name of every already
imported `kestrel` or `tests` module to a subclass whose `now()`/`utcnow()` start at the chosen
instant and then advance at real speed (a clock that never moves hangs deadline loops). Limits:
the `datetime.datetime` class itself is left alone (swapping it hangs C extensions such as pandas
on import), so a `from datetime import datetime` run inside a function still reads the real clock,
and `time.time()` and `date.today()` are not shifted, so code mixing them with `datetime.now()`
sees a skew (the honest limit of this approach)."""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time

import pytest

_REAL = _dt.datetime


class _Meta(type):
    def __instancecheck__(cls, obj: object) -> bool:  # real datetimes still count
        return isinstance(obj, _REAL)


def _frozen(at: _REAL) -> type:
    """A class whose `now()` starts at `at` and then runs at real speed, so deadline loops and
    timeouts still finish (a clock that never advances hangs them)."""
    started = time.monotonic()

    def current() -> _REAL:
        return at + _dt.timedelta(seconds=time.monotonic() - started)

    class Frozen(_REAL, metaclass=_Meta):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return current().astimezone(tz) if tz else current().astimezone().replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # noqa: ANN206
            return current().astimezone(_dt.UTC).replace(tzinfo=None)

    return Frozen


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    raw = os.environ.get("KESTREL_FROZEN_AT")
    if not raw:
        yield
        return
    frozen = _frozen(_REAL.fromisoformat(raw))
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in {"kestrel", "tests"} and getattr(mod, "datetime", None) is _REAL:
            monkeypatch.setattr(mod, "datetime", frozen)
    yield
