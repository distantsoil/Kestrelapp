"""Bounded retry for short web writes that collide with another writer (D-DB-1).

SQLite allows one writer at a time. A person's save (a Calibrate rating, Stop) can land while the
worker commits a batch; the connection's 5 second busy timeout then expires and the write raises
"database is locked". The right answer is a short, invisible wait, not a failed click.

`write_with_retry(fn)` runs `fn(session)` in a FRESH session for every attempt (a failed attempt
leaves a half-used session behind), retries only a SQLAlchemy OperationalError that says
locked/busy, and backs off with jitter. When the budget is spent it re-raises the last error, so
the app's `_db_busy` handler still answers with its calm 503 as the last resort. Everything
`fn` writes must be safe to run again: it must commit itself, and nothing may be committed
before the failing statement (a lock error rolls the attempt back)."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.locks import is_lock_error

log = structlog.get_logger()

FIRST_DELAY_S = 0.1
MAX_DELAY_S = 2.0
DEFAULT_BUDGET_S = 12.0



def _monotonic() -> float:
    """The retry clock. Tests patch this, never `time.monotonic` itself: replacing the global
    clock also feeds the event loop, and on Windows the proactor loop's teardown then reads a
    fake clock that has run out (public PR gate on Windows, 2026-11-01)."""
    return time.monotonic()

async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def write_with_retry[T](
    fn: Callable[[AsyncSession], Awaitable[T]],
    *,
    budget_s: float = DEFAULT_BUDGET_S,
    path: str = "",
) -> T:
    """Run `await fn(session)` in a fresh session, retrying lock errors within `budget_s`."""
    from kestrel.web.deps import sessionmaker

    started = _monotonic()
    deadline = started + budget_s
    delay = FIRST_DELAY_S
    attempt = 0
    while True:
        attempt += 1
        try:
            async with sessionmaker()() as session:
                return await fn(session)
        except OperationalError as exc:
            if not is_lock_error(exc):
                raise
            now = _monotonic()
            wait = delay * random.uniform(0.5, 1.0)
            if now + wait >= deadline:  # no attempt may start after the deadline
                log.warning(
                    "db_write_gave_up",
                    path=path,
                    attempts=attempt,
                    waited_s=round(now - started, 2),
                )
                raise
            log.warning(
                "db_write_retry",
                path=path,
                attempt=attempt,
                wait_s=round(wait, 2),
                waited_s=round(now - started, 2),
            )
            await _sleep(wait)
            delay = min(delay * 2, MAX_DELAY_S)
