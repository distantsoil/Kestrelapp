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


# ---- the release number in the message (#105) -------------------------------------------------
def _db_from_a_newer_release(saved_by: str | None):
    """The app database as a newer Kestrel leaves it: a revision this build has never heard of,
    and (when it recorded one) the release that saved it. Built by the real migration."""
    import sqlite3

    from kestrel import paths
    from kestrel.db import migrate

    migrate.run_migrations()
    conn = sqlite3.connect(paths.db_path())
    try:
        conn.execute("UPDATE alembic_version SET version_num = '9999'")
        conn.execute("DELETE FROM setting WHERE key = ?", (migrate.SAVED_BY_KEY,))
        if saved_by is not None:
            conn.execute(
                "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, '2026-11-05')",
                (migrate.SAVED_BY_KEY, f'"{saved_by}"'),
            )
        conn.commit()
    finally:
        conn.close()


def _startup_error():
    import kestrel.desktop.shell as shell

    with pytest.raises(shell.StartupError) as exc_info:
        shell._start_backend(shell._Splash())
    return exc_info.value


def test_the_message_names_the_release_that_saved_the_data():
    _db_from_a_newer_release("99.1.0")
    err = _startup_error()
    assert err.code == "newer_database"
    assert err.reason == (
        "This data was saved by a newer Kestrel (99.1.0). Install the latest version to open it."
    )


@pytest.mark.parametrize("saved_by", [None, "not-a-version", "0.0.1"])
def test_without_a_known_newer_release_no_number_is_invented(saved_by):
    """No record (a database saved before the record existed), an unreadable one, or one that is
    not newer than this build: the message says "a newer Kestrel" and no number."""
    _db_from_a_newer_release(saved_by)
    err = _startup_error()
    assert err.code == "newer_database"
    assert err.reason == (
        "This data was saved by a newer Kestrel. Install the latest version to open it."
    )


def test_opening_a_database_records_the_release_and_never_lowers_it(monkeypatch):
    import sqlite3

    monkeypatch.setenv("KESTREL_REAL_MIGRATIONS", "1")  # the suite otherwise copies a template

    from kestrel import __version__, paths
    from kestrel.db import migrate

    migrate.run_migrations()
    assert migrate.saved_by_version() == __version__
    conn = sqlite3.connect(paths.db_path())
    conn.execute(
        "UPDATE setting SET value = '\"99.1.0\"' WHERE key = ?", (migrate.SAVED_BY_KEY,)
    )
    conn.commit()
    conn.close()
    migrate.stamp_saved_by(paths.db_path())
    assert migrate.saved_by_version() == "99.1.0"  # an older build never lowers the record
    assert migrate.newer_than_this_build() == "99.1.0"
    # reading is read-only and the data is otherwise untouched
    conn = sqlite3.connect(paths.db_path())
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] != "9999"
    conn.close()


def test_the_newer_database_is_never_modified():
    import hashlib

    from kestrel import paths

    _db_from_a_newer_release("99.1.0")
    # The WAL is folded into the file by closing connections; compare the main file.
    before = hashlib.sha256(paths.db_path().read_bytes()).hexdigest()
    _startup_error()
    assert hashlib.sha256(paths.db_path().read_bytes()).hexdigest() == before
