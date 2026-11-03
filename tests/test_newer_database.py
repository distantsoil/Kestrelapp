"""Issue #105: older Kestrel on a newer database shows a clear window, not an abort.

When the database revision is newer than the app knows, the migration fails with
"Can't locate revision identified by '0026'". The fix detects this and raises a
StartupError with code "newer_database" and a clear message, so the splash window
shows a Download button instead of a retry (which would fail the same way).
"""

from __future__ import annotations

import pytest
from alembic.util.exc import CommandError


def test_newer_database_error_detected(monkeypatch):
    """A 'Can't locate revision' error is detected and mapped to 'newer_database'."""
    import kestrel.db.migrate as migrate
    import kestrel.desktop.shell as shell

    # Simulate the migration failing with a newer-database error
    def fake_migrations():
        raise CommandError("Can't locate revision identified by '0026'")

    monkeypatch.setattr(migrate, "run_migrations", fake_migrations)

    # Call _start_backend and verify the StartupError has the right code
    with pytest.raises(shell.StartupError) as exc_info:
        shell._start_backend(shell._Splash())

    assert exc_info.value.code == "newer_database"
    assert "newer Kestrel" in exc_info.value.reason
    assert "Download" in exc_info.value.details


def test_generic_migration_error_not_newer_database(monkeypatch):
    """A non-revision migration error is NOT mapped to 'newer_database'."""
    import kestrel.db.migrate as migrate
    import kestrel.desktop.shell as shell

    def fake_migrations():
        raise OSError("disk full")

    monkeypatch.setattr(migrate, "run_migrations", fake_migrations)

    with pytest.raises(shell.StartupError) as exc_info:
        shell._start_backend(shell._Splash())

    assert exc_info.value.code == "migrations_failed"
    assert "newer Kestrel" not in exc_info.value.reason
