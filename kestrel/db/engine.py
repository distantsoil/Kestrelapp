"""Async engine (aiosqlite) for the app and a sync engine for Alembic, against the SAME file.

Every connection sets WAL, synchronous=NORMAL, and a 5s busy timeout (SPEC.md, D-UI-174,
CLAUDE.md). Foreign keys are enforced. The worker, scheduler, and every session live on one loop
(SPEC-INTERFACES.md 1.1); this module holds a single app engine, resettable for tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kestrel import paths

log = structlog.get_logger()

# A write transaction that stays open longer than this blocks every other writer for that long
# (D-DB-1). Logged when it ends, with the first write statement and the asyncio task that held it,
# so a future long holder is named in the log instead of found by guessing (#89, #92).
WRITE_HOLD_WARN_S = 2.0
_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLAC", "BEGIN ")


def _install_write_hold_timer(engine: Engine) -> None:
    """Time each write transaction from its first write statement to commit or rollback. Costs one
    string-prefix test per statement."""

    def before_execute(conn, _cursor, statement, _params, _context, _executemany) -> None:
        info = conn.connection.info
        if "write_t0" not in info and statement[:6].upper().startswith(_WRITE_VERBS):
            info["write_t0"] = time.monotonic()
            info["write_stmt"] = " ".join(statement.split())[:80]

    def ended(conn) -> None:
        info = conn.connection.info
        t0 = info.pop("write_t0", None)
        stmt = info.pop("write_stmt", "")
        if t0 is None:
            return
        held = time.monotonic() - t0
        if held < WRITE_HOLD_WARN_S:
            return
        try:
            holder = asyncio.current_task()
            holder_name = holder.get_name() if holder else ""
        except RuntimeError:
            holder_name = ""
        log.warning(
            "write_transaction_held_long",
            held_s=round(held, 1),
            first_statement=stmt,
            task=holder_name,
        )

    def forget(_dbapi_conn, record) -> None:
        # A connection returned to the pool (a cancelled request never reached commit or rollback)
        # must not carry its timer into the next, unrelated use.
        record.info.pop("write_t0", None)
        record.info.pop("write_stmt", None)

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "commit", ended)
    event.listen(engine, "rollback", ended)
    event.listen(engine.pool, "checkin", forget)


def _apply_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _url(db_path: str | Path, *, async_: bool) -> str:
    driver = "sqlite+aiosqlite" if async_ else "sqlite"
    return f"{driver}:///{Path(db_path).as_posix()}"


def make_async_engine(db_path: str | Path) -> AsyncEngine:
    engine = create_async_engine(_url(db_path, async_=True))
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    _install_write_hold_timer(engine.sync_engine)
    return engine


def make_sync_engine(db_path: str | Path) -> Engine:
    engine = create_engine(_url(db_path, async_=False))
    event.listen(engine, "connect", _apply_pragmas)
    return engine


# ---------------------------------------------------------------------------- app-wide singleton
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        paths.ensure_dirs()
        _engine = make_async_engine(paths.db_path())
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def reset_engine() -> None:
    """Dispose the app engine (tests, or a data-dir change)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def forget_engine() -> None:
    """Drop the cached app engine WITHOUT awaiting dispose. For when its owning event loop is gone
    (e.g. after a background serve thread stops): awaiting dispose from another loop would error, so
    the refs are cleared and the dead engine is left to GC. A later caller then builds a fresh one
    on its own loop, avoiding the cross-loop aiosqlite trap."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
