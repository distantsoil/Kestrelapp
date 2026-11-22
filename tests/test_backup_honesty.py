"""A backup says what it refused, in true words.

The database rule looks only at the database file itself: a data folder moved to another drive
behind a link is a normal setup and is backed up as usual, while a database file that is a link
or has a second name stops the backup with a sentence naming exactly that."""

from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.services import backup as backup_service
from kestrel.services.backup import create_backup
from tests.symlink_probe import link_folder, symlink_or_skip

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _other_program_db(path) -> bytes:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notes (body TEXT)")
    con.execute("INSERT INTO notes VALUES ('INVENTED OUTSIDE ROW')")
    con.commit()
    con.close()
    return path.read_bytes()


def test_a_data_folder_moved_behind_a_link_is_backed_up(tmp_path):
    """The whole data folder lives on another drive and Kestrel's data folder is a link to it:
    the database and documents are backed up, not refused as "a link"."""
    moved = tmp_path / "other-drive" / "data"
    moved.mkdir(parents=True)
    data = paths.data_dir()
    shutil.rmtree(data, ignore_errors=True)
    data.parent.mkdir(parents=True, exist_ok=True)
    link_folder(data, moved)
    (moved / "documents").mkdir()
    (moved / "documents" / "resume.pdf").write_bytes(b"%PDF invented")
    migrate.run_migrations()

    archive = create_backup(tmp_path / "out")

    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert backup_service.DB_NAME in names
        assert z.read(f"{backup_service.DOCS_PREFIX}documents/resume.pdf") == b"%PDF invented"


def test_a_database_file_that_is_a_link_is_refused_in_those_words(tmp_path):
    other = tmp_path / "other-program.db"
    before = _other_program_db(other)
    db = paths.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    symlink_or_skip(db, other)

    with pytest.raises(PermissionError) as info:
        create_backup(tmp_path / "out")

    assert str(info.value) == "the database file is a link, so Kestrel did not copy it"
    assert other.read_bytes() == before  # refused before migrations could write through it


def test_a_database_file_with_a_second_name_is_refused_in_those_words(tmp_path):
    migrate.run_migrations()
    db = paths.db_path()
    os.link(db, tmp_path / "second-name.db")

    with pytest.raises(PermissionError) as info:
        create_backup(tmp_path / "out")

    assert str(info.value) == (
        "the database file has a second name elsewhere on this computer (a hard link), "
        "so Kestrel did not copy it")


def test_a_second_name_made_after_migrations_is_still_refused(tmp_path, monkeypatch):
    """The check inside the archive build: a hard link made after the first check (here, by the
    migration step) still stops the copy, and no archive is left behind."""
    real = migrate.run_migrations

    def migrate_then_link() -> None:
        real()
        os.link(paths.db_path(), tmp_path / "late-name.db")

    monkeypatch.setattr(migrate, "run_migrations", migrate_then_link)
    out = tmp_path / "out"
    with pytest.raises(PermissionError) as info:
        create_backup(out)
    assert "a second name elsewhere on this computer (a hard link)" in str(info.value)


def test_the_backup_page_says_why_the_database_was_refused(tmp_path):
    migrate.run_migrations()
    os.link(paths.db_path(), tmp_path / "second-name.db")

    for url in ("/backup/download", "/settings/data/backup"):
        r = _client().get(url)
        assert r.status_code == 409, url
        assert ("Could not build the backup: the database file has a second name elsewhere on "
                "this computer (a hard link), so Kestrel did not copy it.") in r.text, url
        assert "permission was denied" not in r.text
