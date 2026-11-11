"""A short-lived cache for the counts every poll asks for (D-PERF-2): the Pipeline's group
counts behind the nav badge, the status rail, the tray status and Today's Review card.

What it must never do is disagree with the Pipeline, so it is exact about invalidation, not only
timed. Every write statement that names a table the group statement reads (WATCHED) bumps a
version when its transaction ends (commit or rollback), and an entry is used only at the version
it was computed at. While any watched write is still uncommitted anywhere, the cache is bypassed
(a reader never stores or shares a number that includes a write that may roll back). What no
write announces still ages out: a retry or a lease coming due is a clock change, so an entry also
dies after TTL_SECONDS, and a write by another process is seen within it. The per-engine key
keeps one database's numbers from ever answering for another.

The hooks sit on the Engine and Pool classes, so every engine (the app's, a test's, Alembic's) is
covered by one registration. Cost: one string-prefix test per statement, and a regex only for a
write."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import Pool

TTL_SECONDS = 4.0

# Every table the Pipeline's group expression, its filters and the fit words read
# (tests/test_count_cache.py compiles the real statements and fails if one is missing here).
WATCHED = frozenset({
    "opportunity", "evaluation", "pursuit", "task", "verdict_override", "org", "setting",
    "quarantine", "source", "scrape_run", "status_event",
})
_WRITE = re.compile(
    r"\s*(?:insert\s+(?:or\s+\w+\s+)?into|replace\s+into|update(?:\s+or\s+\w+)?|delete\s+from)"
    r'\s+["`\[]?(\w+)',
    re.IGNORECASE,
)
_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLAC")

_state: dict[str, int] = {"version": 0, "open": 0}
_entries: dict[tuple[Any, ...], tuple[int, float, Any]] = {}


def version() -> int:
    return _state["version"]


def bump() -> None:
    """Something that feeds the counts changed (also callable by hand, and by tests)."""
    _state["version"] += 1


def clear() -> None:
    _entries.clear()
    bump()


def _before_execute(conn, _cursor, statement, _params, _context, _executemany) -> None:
    if not statement[:6].upper().startswith(_VERBS):
        return
    m = _WRITE.match(statement)
    if m is None or m.group(1).lower() not in WATCHED:
        return
    info = conn.connection.info
    if not info.get("count_dirty"):
        info["count_dirty"] = True
        _state["open"] += 1


def _ended(conn) -> None:
    info = conn.connection.info
    if info.pop("count_dirty", False):
        _state["open"] = max(0, _state["open"] - 1)
        bump()


def _checkin(_dbapi_conn, record) -> None:
    # A connection returned without a commit or rollback (a cancelled request) ends its writes.
    if record.info.pop("count_dirty", False):
        _state["open"] = max(0, _state["open"] - 1)
        bump()


event.listen(Engine, "before_cursor_execute", _before_execute)
event.listen(Engine, "commit", _ended)
event.listen(Engine, "rollback", _ended)
event.listen(Pool, "checkin", _checkin)


def _engine_key(session: AsyncSession) -> int:
    return id(session.get_bind())


def _tracks_key() -> Any:
    from kestrel.services import tracks

    return tracks._cache["value"]  # what switched off tracks hold back (queue.not_held)


async def cached(
    session: AsyncSession, name: str, compute: Callable[[], Awaitable[Any]], *, arg: Any = None
) -> Any:
    """`await compute()`, reused while nothing it reads has changed and it is under TTL_SECONDS
    old. The result is shared: callers must not mutate it (copy first)."""
    if session.new or session.dirty or session.deleted:
        return await compute()  # this session's own unflushed changes count (autoflush)
    key = (_engine_key(session), name, arg, _tracks_key())
    now = time.monotonic()
    ver = _state["version"]
    hit = _entries.get(key)
    if hit is not None and hit[0] == ver and now - hit[1] < TTL_SECONDS and not _state["open"]:
        return hit[2]
    value = await compute()
    if not _state["open"] and _state["version"] == ver:
        _entries[key] = (ver, time.monotonic(), value)
        if len(_entries) > 64:
            _entries.pop(next(iter(_entries)))
    return value
