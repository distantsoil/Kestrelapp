"""One definition of "another writer holds the SQLite file", shared by the task queue, the worker
and the web retry helper (D-DB-1). Matches the driver's own message (`exc.orig`), never the SQL
text, so a statement that merely mentions "locked" is not mistaken for a lock."""

from __future__ import annotations

_LOCK_MESSAGES = ("database is locked", "database table is locked", "database is busy")


def is_lock_error(exc: BaseException) -> bool:
    """True for a SQLAlchemy or sqlite3 OperationalError that says the file or a table is locked
    or busy. Anything else (no such table, a constraint, a non-database error) is False."""
    from sqlalchemy.exc import OperationalError as SAOperationalError

    if isinstance(exc, SAOperationalError):
        inner: BaseException = exc.orig if exc.orig is not None else exc
    else:
        import sqlite3

        if not isinstance(exc, sqlite3.OperationalError):
            return False
        inner = exc
    text = str(inner).lower()
    return any(m in text for m in _LOCK_MESSAGES)
