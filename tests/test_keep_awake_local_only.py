"""Keep awake is this computer's setting, under the key it is really stored at (D-SEC-10).

INVARIANT: a backup cannot make this computer stay awake during runs; a ticked restore lists the
switch before it is taken, and a key holder cannot change it."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.services import keep_awake
from kestrel.services.backup import create_backup, restore_backup
from kestrel.web import settings_io

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _fresh_db() -> None:
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _put(key, value) -> None:
    con = sqlite3.connect(str(paths.db_path()))
    con.execute("DELETE FROM setting WHERE key = ?", (key,))
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, '2026-01-01')",
                (key, json.dumps(value)))
    con.commit()
    con.close()


def _get(key):
    con = sqlite3.connect(str(paths.db_path()))
    row = con.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    con.close()
    return None if row is None else json.loads(row[0])


def test_the_stored_key_is_local_only_and_undoable():
    assert settings_io.needs_local(keep_awake.KEY, True)
    assert settings_io.undo_url(keep_awake.KEY, False, True) is not None


def test_an_unticked_restore_keeps_this_computers_keep_awake(tmp_path):
    _fresh_db()
    paths.seed_config()
    _put(keep_awake.KEY, True)
    archive = create_backup(tmp_path)
    _fresh_db()
    _put(keep_awake.KEY, False)

    restore_backup(archive)

    assert _get(keep_awake.KEY) is False


def test_the_preview_lists_keep_awake(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    _fresh_db()
    paths.seed_config()
    _put(keep_awake.KEY, True)
    archive = create_backup(tmp_path)

    preview = preview_backup(archive)

    assert any(i.label == "Keep the computer awake during runs" and i.value == "on"
               for i in preview.switches)
