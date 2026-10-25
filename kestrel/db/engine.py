"""Async engine (aiosqlite) for the app and a sync engine for Alembic, against the SAME file.

Every connection sets WAL, synchronous=NORMAL, and a 5s busy timeout (SPEC.md, D-UI-174,
CLAUDE.md). Foreign keys are enforced. The worker, scheduler, and every session live on one loop
(SPEC-INTERFACES.md 1.1); this module holds a single app engine, resettable for tests."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kestrel import paths


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
