"""D-DB-1: write_with_retry waits out a lock in a fresh session, and only a lock."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import OperationalError

from kestrel.web import db_retry


def _op(msg: str, sql: str = "UPDATE x") -> OperationalError:
    return OperationalError(sql, {}, sqlite3.OperationalError(msg))


@pytest.fixture
def fast(monkeypatch):
    slept: list[float] = []

    async def fake(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(db_retry, "_sleep", fake)
    return slept


@pytest.fixture
def sessions(monkeypatch):
    """Each attempt opens a new, distinct session object."""
    opened: list[object] = []

    class _Ctx:
        async def __aenter__(self):
            opened.append(object())
            return opened[-1]

        async def __aexit__(self, *exc):
            return None

    import kestrel.web.deps as deps

    monkeypatch.setattr(deps, "sessionmaker", lambda: lambda: _Ctx())
    return opened


async def test_retries_a_locked_error_then_succeeds_in_a_fresh_session(fast, sessions):
    seen: list[object] = []

    async def fn(session):
        seen.append(session)
        if len(seen) < 3:
            raise _op("database is locked")
        return "saved"

    assert await db_retry.write_with_retry(fn, path="/x") == "saved"
    assert len(seen) == 3 and len({id(s) for s in seen}) == 3  # a fresh session every attempt
    assert len(fast) == 2 and all(0 < w <= 2.0 for w in fast)


async def test_gives_up_after_the_budget_and_reraises_the_lock_error(fast, sessions):
    calls = 0

    async def fn(session):
        nonlocal calls
        calls += 1
        raise _op("database is busy")

    with pytest.raises(OperationalError, match="busy"):
        await db_retry.write_with_retry(fn, budget_s=0.5, path="/x")
    assert calls >= 2


async def test_other_operational_errors_are_not_retried(fast, sessions):
    calls = 0

    async def fn(session):
        nonlocal calls
        calls += 1
        raise _op("no such table: nope")

    with pytest.raises(OperationalError, match="no such table"):
        await db_retry.write_with_retry(fn)
    assert calls == 1 and fast == []


async def test_non_database_errors_pass_straight_through(fast, sessions):
    async def fn(session):
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await db_retry.write_with_retry(fn)
    assert fast == []


async def test_the_sql_text_is_never_matched_only_the_driver_message(fast, sessions):
    calls = 0

    async def fn(session):
        nonlocal calls
        calls += 1
        raise _op("no such column: x", sql="UPDATE locked_things SET busy = 1")

    with pytest.raises(OperationalError):
        await db_retry.write_with_retry(fn)
    assert calls == 1


async def test_a_table_lock_is_retried_and_no_attempt_starts_after_the_deadline(
    fast, sessions, monkeypatch
):
    calls = 0

    async def fn(session):
        nonlocal calls
        calls += 1
        raise _op("database table is locked")

    clock = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    # Patch the module's own clock, never the global time.monotonic (the event loop reads that).
    monkeypatch.setattr(db_retry, "_monotonic", lambda: next(clock, 5.0))
    with pytest.raises(OperationalError):
        await db_retry.write_with_retry(fn, budget_s=1.0)
    assert calls == 2  # the second failure landed past the deadline: no third attempt
