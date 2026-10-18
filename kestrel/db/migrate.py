"""Programmatic Alembic migration (SPEC-INTERFACES.md 1.4): migrations run at startup via
command.upgrade against a SYNC sqlite:// engine, NOT sqlite+aiosqlite://. The migrations directory
is package data resolved through importlib.resources, so the frozen read-only install works too."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from kestrel import paths


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
