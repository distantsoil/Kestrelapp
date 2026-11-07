"""Programmatic Alembic migration (SPEC-INTERFACES.md 1.4): migrations run at startup via
command.upgrade against a SYNC sqlite:// engine, NOT sqlite+aiosqlite://. The migrations directory
is package data resolved through importlib.resources, so the frozen read-only install works too."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

from kestrel import paths

log = structlog.get_logger()


def _config(script_location: str, db_path: str | Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{Path(db_path).as_posix()}")
    return cfg


def run_migrations(db_path: str | Path | None = None) -> None:
    """Upgrade the database at db_path (default: the app DB) to head."""
    paths.ensure_dirs()
    target = Path(db_path) if db_path else paths.db_path()
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        command.upgrade(_config(str(mig_dir), target), "head")
    if db_path is None:
        stamp_saved_by(target)


# The release that last opened this database, kept so an OLDER build that meets a revision it does
# not know can name the newer release (#105). A plain setting row; nothing else reads it.
SAVED_BY_KEY = "app.saved_by_version"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _version_tuple(text_value: object) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text_value) if isinstance(text_value, str) else None
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def saved_by_version(db_path: str | Path | None = None) -> str | None:
    """The release recorded as the last to open the database, or None when it cannot be known
    (no file, no such row, a database saved before the row existed, an unreadable value). Read
    with plain sqlite3 so it works on a schema this build does not understand, and read-only."""
    target = Path(db_path) if db_path else paths.db_path()
    if not target.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT value FROM setting WHERE key = ?", (SAVED_BY_KEY,)
            ).fetchone()
        finally:
            conn.close()
        value = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    return value if _version_tuple(value) else None


def newer_than_this_build(db_path: str | Path | None = None) -> str | None:
    """The recorded release when it is strictly newer than this build, else None: a database an
    equal or older release saved is not "newer", so no number is claimed for it."""
    from kestrel import __version__

    saved = saved_by_version(db_path)
    current = _version_tuple(__version__)
    seen = _version_tuple(saved)
    return saved if saved and seen and current and seen > current else None


def stamp_saved_by(db_path: str | Path) -> None:
    """Record this build as the last to open the database, never lowering an existing record.
    Best effort: a database that cannot take the write must not stop Kestrel from opening."""
    from kestrel import __version__
    from kestrel.db.base import utcnow_iso

    current = _version_tuple(__version__)
    if current is None:
        return
    recorded = _version_tuple(saved_by_version(db_path))
    if recorded is not None and recorded > current:
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute(
                "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (SAVED_BY_KEY, json.dumps(__version__), utcnow_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("saved_by_stamp_failed", error_type=type(exc).__name__)


def head_revision() -> str | None:
    """The newest revision the SHIPPED migration scripts define, independent of any database.

    doctor compares this against current_revision() so a database left behind by an older build is
    reported as pending rather than as clean: "has some revision" is not the same as "up to date".
    Returns None if the script directory cannot be read, which the caller treats as "cannot tell"
    rather than as a failure.
    """
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    try:
        with paths.resource_path(paths.packaged_migrations()) as mig_dir:
            return ScriptDirectory.from_config(_config(str(mig_dir), ":memory:")).get_current_head()
    except (OSError, RuntimeError, ValueError, CommandError):
        # CommandError is what alembic actually raises for a missing script directory and for a
        # multi-head graph, and it derives straight from Exception, so it must be named explicitly.
        # Missing migrations is exactly the frozen-packaging failure doctor exists to diagnose:
        # doctor must report it, never traceback on it.
        return None


def known_revisions() -> frozenset[str] | None:
    """Every revision the SHIPPED migration scripts define, or None if they cannot be read.

    A restore uses this to refuse a backup made by a NEWER Kestrel: its database is stamped with a
    revision this build has never heard of, and `command.upgrade` would fail on it only AFTER the
    restore had already replaced the live data, leaving an app that will not start."""
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    try:
        with paths.resource_path(paths.packaged_migrations()) as mig_dir:
            script = ScriptDirectory.from_config(_config(str(mig_dir), ":memory:"))
            return frozenset(rev.revision for rev in script.walk_revisions())
    except (OSError, RuntimeError, ValueError, CommandError):
        return None


def current_revision(db_path: str | Path | None = None) -> str | None:
    """The applied head revision, or None if the DB is not initialized. Used by doctor."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    target = Path(db_path) if db_path else paths.db_path()
    if not Path(target).exists():
        return None
    engine = create_engine(f"sqlite:///{Path(target).as_posix()}")
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row else None
    except OperationalError:
        # Expected when the DB exists but has no alembic_version table yet (not initialized).
        # Any other error is a real fault and should propagate rather than masquerade as "clean".
        return None
    finally:
        engine.dispose()
