"""Backup and restore (kestrel.services.backup, shared by `kestrel backup`/`kestrel restore` and
the /settings web restore action). Cloud-safe: pure file I/O plus a real migrated SQLite db, no
Ollama/Playwright/network.

THE GATE (test_roundtrip_after_wipe_service_level and test_cli_backup_then_restore_roundtrip):
populate the db with a couple of Org rows, run backup, wipe/change the db, restore from the zip,
and assert the original rows are back. Also covers: the zip layout (db + config + documents, never
.env), refusing a non-Kestrel zip and an unsafe path entry before touching anything, and the web
upload endpoint never 500ing on a bad file."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import zipfile

import pytest
import sklearn
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner

from kestrel import paths
from kestrel.cli import app
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import DocMaster, DocRender, MlModel, Opportunity, Org, PrepPack
from kestrel.services.backup import InvalidBackupError, create_backup, restore_backup
from tests.learning_fakes import craft_reranker_evil_model, craft_reranker_inert_artifact

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

runner = CliRunner()


# ------------------------------------------------------------------------------------- helpers
def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = paths.db_path().with_name(paths.db_path().name + suffix)
        p.unlink(missing_ok=True)


async def _seed_two_orgs() -> None:
    """A fresh migrated db (function-scoped _isolate_data_home gives each test its own app-db) with
    two Org rows -- the "couple of rows" the gate populates before backing up."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(name="Acme Robotics", normalized_name="acme robotics"))
        s.add(Org(name="Beacon Health", normalized_name="beacon health"))
        await s.commit()
    await engine.dispose()
    forget_engine()  # the file is about to be zipped/replaced; nothing may hold it open


async def _wipe_and_reseed_other() -> None:
    """Simulate the database changing after the backup was taken: a fresh db with DIFFERENT
    content, so a restore that merely no-ops would be caught."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(name="Someone Else", normalized_name="someone else"))
        await s.commit()
    await engine.dispose()
    forget_engine()


async def _org_names() -> set[str]:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        names = (await s.execute(select(Org.normalized_name))).scalars().all()
    await engine.dispose()
    return set(names)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


async def _pdf_paths() -> tuple[list[str | None], list[str | None]]:
    """The stored pdf_path values of every doc_render and prep_pack row (the issue 150 checks)."""
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        renders = list((await s.execute(select(DocRender.pdf_path))).scalars())
        preps = list((await s.execute(select(PrepPack.pdf_path))).scalars())
    await engine.dispose()
    return renders, preps


# ------------------------------------------------------------------------------------- backup
def test_backup_zip_contains_db_config_documents_never_env(tmp_path):
    asyncio.run(_seed_two_orgs())
    paths.seed_config()  # ensure at least settings.yml exists in the writable config dir

    dotenv = paths.dotenv_path()
    dotenv.write_text("KESTREL_SECRET=do-not-leak\n", encoding="utf-8")

    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake resume\n")
    prep_dir = paths.data_dir() / "prep_packs"
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "1_prep_pack.pdf").write_bytes(b"%PDF-1.4 fake prep pack\n")

    try:
        archive = create_backup(tmp_path)
        assert archive.exists()

        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            assert "kestrel.db" in names
            assert any(n.startswith("config/") and n.endswith(".yml") for n in names)
            assert "documents/documents/resume.pdf" in names
            assert "documents/prep_packs/1_prep_pack.pdf" in names
            assert not any(".env" in n for n in names)  # secrets never leave the machine
            for n in names:
                assert b"do-not-leak" not in zf.read(n)
    finally:
        dotenv.unlink(missing_ok=True)  # never leave a stray .env for other tests


def test_backup_under_an_open_write_transaction_is_a_consistent_readable_db(tmp_path):
    """The database is zipped as a sqlite3 online-backup snapshot, not a file copy: a writer
    mid-transaction (the serve loop) neither blocks the backup nor leaks uncommitted rows into
    it, and the copy is readable on its own with no -wal/-shm beside it."""
    import sqlite3

    asyncio.run(_seed_two_orgs())
    db = paths.db_path()
    writer = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO org (name, normalized_name, first_seen_at, last_seen_at) "
            "VALUES ('Uncommitted Inc', 'uncommitted inc', '2026-10-12', '2026-10-12')"
        )
        archive = create_backup(tmp_path)  # the write transaction is still open
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "kestrel.db" in names
        assert not any(n.endswith(("-wal", "-shm")) for n in names)
        out = tmp_path / "unpacked.db"
        out.write_bytes(zf.read("kestrel.db"))
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = {r[0] for r in conn.execute("SELECT normalized_name FROM org")}
    finally:
        conn.close()
    assert rows == {"acme robotics", "beacon health"}  # committed rows only, every one of them


# ------------------------------------------------------------------------------- THE GATE (1/2)
def test_roundtrip_after_wipe_service_level(tmp_path):
    """Populate -> backup -> wipe/change the db -> restore -> the original rows are back."""
    asyncio.run(_seed_two_orgs())
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}

    archive = create_backup(tmp_path)

    asyncio.run(_wipe_and_reseed_other())
    assert asyncio.run(_org_names()) == {"someone else"}  # confirms the db really changed

    result = restore_backup(archive)
    assert result.db_restored is True
    assert result.pre_restore_backup is not None
    assert result.pre_restore_backup.exists()  # the pre-restore db (the "someone else" state)

    forget_engine()  # a restored file must not be read through a stale cached connection
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


# ------------------------------------------------------------------------------- THE GATE (2/2)
def test_cli_backup_then_restore_roundtrip(tmp_path, monkeypatch):
    """The same round trip, driven through the actual `kestrel backup` / `kestrel restore` CLI."""
    from kestrel import cli as cli_mod

    # `kestrel restore` refuses while the desktop app is running; whether one happens to be open
    # on the machine running the tests must not decide this one.
    monkeypatch.setattr(cli_mod, "_app_is_running", lambda: False)
    asyncio.run(_seed_two_orgs())

    result = runner.invoke(app, ["backup", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "backup written to" in result.output
    zips = list(tmp_path.glob("kestrel-backup-*.zip"))
    assert len(zips) == 1

    asyncio.run(_wipe_and_reseed_other())

    restored = runner.invoke(app, ["restore", str(zips[0])])
    assert restored.exit_code == 0, restored.output
    assert "Database restored: yes." in restored.output
    assert "Earlier data saved as" in restored.output
    assert "Restore complete" in restored.output

    forget_engine()
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


# ------------------------------------------------------------------------------------ safety
def test_restore_refuses_a_non_kestrel_zip_and_touches_nothing(tmp_path):
    bogus = tmp_path / "not-a-backup.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("readme.txt", "just some other zip, not a Kestrel backup")

    db = paths.db_path()
    existed_before = db.exists()
    pre_restore = db.with_name(db.name + ".pre-restore")
    pre_restore.unlink(missing_ok=True)

    with pytest.raises(InvalidBackupError, match="does not look like a Kestrel backup"):
        restore_backup(bogus)

    # Refused before anything real was touched: no pre-restore copy, db existence unchanged.
    assert not pre_restore.exists()
    assert db.exists() == existed_before


def test_restore_refuses_a_path_traversal_entry(tmp_path):
    bogus = tmp_path / "traversal.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("kestrel.db", "looks like a db entry")
        z.writestr("config/../../evil.yml", "pwn: true")

    with pytest.raises(InvalidBackupError, match="unsafe"):
        restore_backup(bogus)


def test_restore_refuses_a_missing_file():
    with pytest.raises(InvalidBackupError, match="The backup file was not found"):
        restore_backup("this/path/does/not/exist.zip")


def test_restore_refuses_a_corrupt_zip(tmp_path):
    not_a_zip = tmp_path / "corrupt.zip"
    not_a_zip.write_bytes(b"this is definitely not a zip file")

    with pytest.raises(InvalidBackupError, match="Not a valid zip"):
        restore_backup(not_a_zip)


def test_restore_refuses_a_db_that_is_not_sqlite(tmp_path):
    """A well-formed zip whose kestrel.db holds garbage. Structure checks pass, so without the
    content check this overwrote a working database and reported db_restored=True."""
    bogus = tmp_path / "garbage-db.zip"
    with zipfile.ZipFile(bogus, "w") as z:
        z.writestr("kestrel.db", b"not a database")

    db = paths.db_path()
    migrate.run_migrations()  # so "the live db is untouched" is a real claim
    db_before = db.read_bytes()
    pre_restore = db.with_name(db.name + ".pre-restore")
    pre_restore.unlink(missing_ok=True)

    with pytest.raises(InvalidBackupError, match="not a usable SQLite database"):
        restore_backup(bogus)

    # Refused during staging: the live database is byte-identical and nothing was copied aside.
    assert db.read_bytes() == db_before
    assert not pre_restore.exists()


def test_restore_refuses_a_db_without_kestrel_tables(tmp_path):
    """A real SQLite file, but not ours. Installing it would look successful and then break
    every query, so the expected tables are checked too."""
    bogus = tmp_path / "wrong-schema.zip"
    other = tmp_path / "other.db"
    con = sqlite3.connect(str(other))
    con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with zipfile.ZipFile(bogus, "w") as z:
        z.write(other, "kestrel.db")

    with pytest.raises(InvalidBackupError, match="not a Kestrel database"):
        restore_backup(bogus)


def test_restore_refuses_an_oversized_entry(tmp_path, monkeypatch):
    """A decompression bomb is refused from the zip's central directory, before extraction."""
    import kestrel.services.backup as backup_mod

    monkeypatch.setattr(backup_mod, "MAX_MEMBER_BYTES", 16)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w") as z:
        z.writestr("kestrel.db", b"x" * 64)

    with pytest.raises(InvalidBackupError, match="oversized entry"):
        restore_backup(bomb)


def test_config_and_documents_are_restored_to_their_real_locations(tmp_path):
    asyncio.run(_seed_two_orgs())
    paths.seed_config()

    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "resume.pdf").write_bytes(b"original resume bytes")

    archive = create_backup(tmp_path)

    # Wipe the config and the document too, so restore is proven to recreate them, not merely
    # leave them alone.
    settings_yml = paths.config_dir() / "settings.yml"
    original_settings_text = settings_yml.read_text(encoding="utf-8")
    settings_yml.unlink()
    (docs_dir / "resume.pdf").unlink()

    result = restore_backup(archive)
    assert "settings.yml" in result.config_files
    assert "documents/resume.pdf" in result.document_files
    assert settings_yml.read_text(encoding="utf-8") == original_settings_text
    assert (docs_dir / "resume.pdf").read_bytes() == b"original resume bytes"


# ---------------------------------------------------------------------------------- issue 150
def _hostile_files(tmp_path) -> dict[str, str]:
    """The sentinel files and their stored spellings, made before the engine: the path
    arithmetic (os.path.relpath) must not run inside the coroutine (ASYNC240)."""
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "id_rsa"
    secret.write_bytes(b"PRIVATE KEY SENTINEL")
    straggler = outside / "straggler_prep.pdf"
    straggler.write_bytes(b"PREP PACK SENTINEL")

    docs_dir = paths.data_dir() / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    inside_doc = docs_dir / "resume.pdf"
    inside_doc.write_bytes(b"%PDF-1.4 inside resume\n")
    preps_dir = paths.data_dir() / "prep_packs"
    preps_dir.mkdir(parents=True, exist_ok=True)
    inside_prep = preps_dir / "1_prep_pack.pdf"
    inside_prep.write_bytes(b"%PDF-1.4 inside prep pack\n")
    # A relative path that starts inside the folder and climbs out of it.
    prep_traversal = os.path.relpath(str(straggler.resolve()), str(preps_dir.resolve()))
    return {
        "secret": str(secret),
        "inside_doc": str(inside_doc),
        "prep_traversal": prep_traversal,
        "inside_prep": str(inside_prep),
    }


async def _seed_hostile_pdf_paths(tmp_path) -> dict[str, int]:
    """A migrated db the way a crafted backup leaves it (issue #150): a doc_render naming an
    absolute path outside the data folder (the issue's own case), a prep_pack escaping through
    `..`, and one row per table whose path IS inside the data folder, which a restore must keep.
    The outside files carry sentinel bytes the assertions refuse to see served."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()

    files = _hostile_files(tmp_path)
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        org = Org(name="Acme Robotics", normalized_name="acme robotics")
        opp = Opportunity(track="job", opp_type="posted", title="Engineering Manager")
        master = DocMaster(track="job", name="Resume", doc_kind="resume")
        s.add_all([org, opp, master])
        await s.flush()
        doc_outside = DocRender(doc_master_id=master.id, opportunity_id=opp.id,
                                pdf_path=files["secret"])
        doc_inside = DocRender(doc_master_id=master.id, opportunity_id=opp.id,
                               pdf_path=files["inside_doc"])
        prep_outside = PrepPack(opportunity_id=opp.id, content_md="Invented.",
                                pdf_path=files["prep_traversal"])
        prep_inside = PrepPack(opportunity_id=opp.id, content_md="Invented.",
                               pdf_path=files["inside_prep"])
        s.add_all([doc_outside, doc_inside, prep_outside, prep_inside])
        await s.commit()
        ids = {"doc_outside": doc_outside.id, "doc_inside": doc_inside.id,
               "prep_outside": prep_outside.id, "prep_inside": prep_inside.id}
    await engine.dispose()
    forget_engine()  # the file is about to be zipped/replaced; nothing may hold it open
    return ids


def test_restore_clears_pdf_paths_that_point_outside_the_data_folder(tmp_path):
    """A backup whose database names a file outside the data folder restores with those values
    cleared to NULL (the files they point at are never read or touched), while a path inside the
    folder survives. Afterwards the issue's own link, GET /prep/<id>/download, is a 404, and its
    bytes are served by nothing. The summary names the count, never the path."""
    ids = asyncio.run(_seed_hostile_pdf_paths(tmp_path))
    secret = tmp_path / "elsewhere" / "id_rsa"
    straggler = tmp_path / "elsewhere" / "straggler_prep.pdf"

    archive = create_backup(tmp_path)

    asyncio.run(_wipe_and_reseed_other())  # the live db changes; restore replaces it whole

    result = restore_backup(archive)
    assert result.db_restored is True
    assert result.paths_cleared == 2  # the two outside values; the two inside ones are kept
    assert any("pointed outside the data folder" in line for line in result.summary_lines())
    assert "id_rsa" not in " ".join(result.summary_lines())  # a path is never quoted

    forget_engine()  # a restored file must not be read through a stale cached connection
    renders, preps = asyncio.run(_pdf_paths())

    docs_dir = paths.data_dir() / "documents"
    preps_dir = paths.data_dir() / "prep_packs"
    assert None in renders and str(docs_dir / "resume.pdf") in renders
    assert None in preps and str(preps_dir / "1_prep_pack.pdf") in preps

    # The files the hostile rows pointed at were never read, moved or deleted.
    assert secret.read_bytes() == b"PRIVATE KEY SENTINEL"
    assert straggler.read_bytes() == b"PREP PACK SENTINEL"

    with _client() as c:
        r = c.get(f"/prep/{ids['prep_outside']}/download")
    assert r.status_code == 404
    assert b"PREP PACK SENTINEL" not in r.content

    with _client() as c:
        r = c.get(f"/prep/{ids['prep_inside']}/download")
    assert r.status_code == 200  # the path that belonged there still works
    assert r.content == b"%PDF-1.4 inside prep pack\n"


# ---------------------------------------------------------------------------------- issue 173
async def _seed_hostile_reranker(tmp_path):
    """A migrated db the way a crafted backup leaves it (issue #173): an ACTIVE reranker whose
    model artifact is a foreign pickle (outside the data folder) whose LOAD writes a sentinel
    file. On current code a restore keeps it active and `load_active_reranker` unpickles it, so
    the sentinel appears; after the fix the restore deactivates it and clears its paths, so it
    can never be loaded. Returns the sentinel path."""
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()

    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    sentinel = outside / "SENTINEL_WRITTEN_BY_UNPICKLE"
    model_pickle = outside / "evil_model.pkl"
    feature_pickle = outside / "evil_features.pkl"
    craft_reranker_evil_model(model_pickle, sentinel)  # load-side effect: writes the sentinel
    craft_reranker_inert_artifact(feature_pickle)      # the PCA pickle is loadable

    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Org(name="Acme Robotics", normalized_name="acme robotics"))
        s.add(MlModel(
            kind="reranker", track="job", version="v1", n_samples=100,
            metrics={"embed_model": "nomic-embed-text"},
            artifact_path=str(model_pickle), pca_artifact_path=str(feature_pickle),
            sklearn_version=sklearn.__version__, feature_names=[], is_active=True,
        ))
        await s.commit()
    await engine.dispose()
    forget_engine()  # the file is about to be zipped/replaced; nothing may hold it open
    return sentinel


def test_restore_never_leaves_a_reranker_that_can_unpickle_a_foreign_file(tmp_path):
    """Issue #173, primary defense: a crafted backup whose reranker row points at a pickle whose
    load has a visible side effect (it writes a sentinel file) restores with the reranker
    deactivated and its artifact paths cleared, and scoring afterwards never triggers the side
    effect. It must fail on the current code (the row stays active and the foreign pickle is
    unpickled). The summary names the count, never the path."""
    sentinel = asyncio.run(_seed_hostile_reranker(tmp_path))
    assert not sentinel.exists()  # nothing has been unpickled yet

    archive = create_backup(tmp_path)
    asyncio.run(_wipe_and_reseed_other())  # the live db changes; restore replaces it whole

    result = restore_backup(archive)
    assert result.db_restored is True
    assert result.rerankers_disabled == 1  # the one reranker row, neutralized
    lines = " ".join(result.summary_lines())
    assert "learned reranker" in lines and "turned off" in lines  # the count is on the page
    assert "evil_model.pkl" not in lines  # a path is never quoted

    # The restored row is deactivated and its paths cleared (defense in depth): even a later
    # reactivation could no longer point at the foreign file.
    async def _row_state():
        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s:
                row = (
                    await s.execute(select(MlModel).where(MlModel.kind == "reranker"))
                ).scalar_one()
                return row.is_active, row.artifact_path, row.pca_artifact_path
        finally:
            await engine.dispose()

    is_active, artifact, pca = asyncio.run(_row_state())
    assert is_active is False
    assert artifact is None and pca is None

    # Scoring after restore must never unpickle the foreign file.
    async def _load():
        from kestrel.learning import reranker as rr

        engine = make_async_engine(paths.db_path())
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s:
                return await rr.load_active_reranker(s, "job")
        finally:
            await engine.dispose()

    assert asyncio.run(_load()) is None  # no active reranker -> fall back to the LLM tier
    assert not sentinel.exists(), "the restored backup made Kestrel unpickle a foreign file"
    # And the foreign file itself was never read, moved or deleted.
    assert (tmp_path / "elsewhere" / "evil_model.pkl").exists()


def test_web_restore_names_the_number_of_deactivated_rerankers(tmp_path):
    """Issue #173 through the Settings > Your data endpoint: the crafted archive (a reranker row
    pointing at a foreign pickle) restores with the count of deactivated rerankers on the page,
    and the page names the count, never the path."""
    sentinel = asyncio.run(_seed_hostile_reranker(tmp_path))
    archive = create_backup(tmp_path)
    with _client() as c:
        r = c.post(
            "/settings/restore",
            data={"from_settings": "1"},
            files={"backup_file": ("kestrel-backup.zip", archive.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200
    assert "1 learned reranker" in r.text  # the count is on the page
    assert "turned off" in r.text
    assert "evil_model.pkl" not in r.text  # a path is never quoted
    assert not sentinel.exists()


# ------------------------------------------------------------------------------------ web page
def test_web_restore_endpoint_round_trips(tmp_path):
    asyncio.run(_seed_two_orgs())
    archive = create_backup(tmp_path)
    zip_bytes = archive.read_bytes()

    asyncio.run(_wipe_and_reseed_other())
    assert asyncio.run(_org_names()) == {"someone else"}

    with _client() as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("kestrel-backup.zip", zip_bytes, "application/zip")},
        )
    assert r.status_code == 200
    assert "Database restored: yes." in r.text

    forget_engine()
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


def test_web_restore_of_a_crafted_backup_cannot_serve_the_file_it_points_at(tmp_path):
    """Issue #150 through the endpoint the Settings > Your data page posts to: the crafted
    archive is posted with the form's own body (from_settings set, the zip as backup_file), the
    page says what was cleared, and the /prep/<id>/download that opens afterwards serves nothing
    of the file the row once pointed at. This pins the endpoint, not the button: the Restore
    button is a submit button in the dialog's hx-post form (st_data.html), and TestClient
    bypasses the page's JavaScript and htmx, so the button wiring is covered by the browser
    tests, not here."""
    ids = asyncio.run(_seed_hostile_pdf_paths(tmp_path))
    archive = create_backup(tmp_path)

    with _client() as c:
        r = c.post(
            "/settings/restore",
            data={"from_settings": "1"},
            files={"backup_file": ("kestrel-backup.zip", archive.read_bytes(),
                                   "application/zip")},
        )
    assert r.status_code == 200
    assert "2 stored document paths pointed outside the data folder and were cleared" in r.text
    assert "id_rsa" not in r.text  # the page names a count, never the path

    with _client() as c:
        r = c.get(f"/prep/{ids['prep_outside']}/download")
    assert r.status_code == 404
    assert b"PREP PACK SENTINEL" not in r.content


def test_web_restore_endpoint_never_500s_on_a_bad_upload(tmp_path):
    with _client() as c:
        # Not a zip at all.
        r1 = c.post(
            "/settings/restore",
            files={"backup_file": ("not-a-zip.zip", b"just some bytes", "application/zip")},
        )
        assert r1.status_code == 200
        assert "warnbox" in r1.text

        # A well-formed zip that is not a Kestrel backup.
        bogus = tmp_path / "bogus.zip"
        with zipfile.ZipFile(bogus, "w") as z:
            z.writestr("readme.txt", "hello")
        r2 = c.post(
            "/settings/restore",
            files={"backup_file": ("bogus.zip", bogus.read_bytes(), "application/zip")},
        )
        assert r2.status_code == 200
        assert "does not look like a Kestrel backup" in r2.text

        # No file chosen at all.
        r3 = c.post("/settings/restore", data={})
        assert r3.status_code == 200
        assert "Choose a backup" in r3.text


def test_web_restore_never_shows_the_archive_entry_name_in_its_error(tmp_path):
    """A rejected entry name is the uploader's to choose. Quoted unescaped, a crafted archive
    executed markup in the app's own origin -- the one holding the LAN shared secret and every
    mutating endpoint. The refusal is now a fixed sentence (D-SEC-4); the name goes to the log."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("kestrel.db", b"x")
        # Rejected for the "..", so it reaches the message carrying live markup.
        z.writestr("config/<img src=x onerror=alert(1)>/../../evil.yml", "pwn: true")

    with _client() as c:
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("evil.zip", evil.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200
    # The page says the fixed sentence and nothing of the name, escaped or not.
    assert "Refusing an unsafe path entry in the backup." in r.text
    assert "<img" not in r.text and "&lt;img" not in r.text and "onerror" not in r.text


def test_a_missing_backup_file_is_reported_without_its_path(tmp_path):
    """The message reaches a page (Settings > Restore), so it names no path (D-SEC-1)."""
    from kestrel.services.backup import InvalidBackupError, restore_backup

    missing = tmp_path / "secret-dir" / "gone.zip"
    with pytest.raises(InvalidBackupError) as err:
        restore_backup(missing)
    assert "secret-dir" not in str(err.value) and "gone.zip" not in str(err.value)
    assert str(err.value) == "The backup file was not found."


def test_an_unreadable_backup_entry_is_reported_without_the_error_text(tmp_path, monkeypatch):
    """An OSError while unpacking carries a staging path; the page gets a fixed sentence and the
    log gets the entry and the error type (D-SEC-1, D-SEC-4)."""
    from kestrel.services import backup

    zpath = tmp_path / "b.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("kestrel.db", b"x")

    def refuse(fsrc, fdst):
        raise PermissionError(13, "Permission denied", r"C:\secret-dir\staging\kestrel.db")

    monkeypatch.setattr(backup.shutil, "copyfileobj", refuse)
    with pytest.raises(InvalidBackupError) as err:
        restore_backup(zpath)
    assert "secret-dir" not in str(err.value) and "Permission denied" not in str(err.value)
    assert str(err.value) == "Could not read a file from the backup."


# ---------------------------------------------------------------------------------- D-SEC-10
# A restored backup is hostile input. It never changes what only this computer may change (the
# model route and keys, the server and MCP switches), never writes outside the document folders,
# and never carries an online confirmation. Invented values only.


EVIL_URL = "https://models.invalid.example/v1"


def _put_setting(key: str, value) -> None:
    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("DELETE FROM setting WHERE key = ?", (key,))
        con.execute(
            "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, '2026-01-01')",
            (key, json.dumps(value)),
        )
        con.commit()
    finally:
        con.close()


def _get_setting(key: str):
    con = sqlite3.connect(str(paths.db_path()))
    try:
        row = con.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return json.loads(row[0]) if isinstance(row[0], (str, bytes)) else row[0]


def _fresh_db() -> None:
    asyncio.run(reset_engine())
    _wipe_db()
    migrate.run_migrations()
    forget_engine()


def test_restore_keeps_this_computers_local_only_settings_and_restores_the_rest(tmp_path):
    _fresh_db()
    hostile = {
        "llm.backends.local.base_url": EVIL_URL, "llm.mode": "hosted",
        "llm.backends.frontier.base_url": EVIL_URL, "mcp.enabled": True, "server.bind_all": True,
        "hosted_tiers_note": "x", "updates.enabled": True, "keep_awake": True,
        "ui.autostart": True,
        "ui.scheme": "dark", "max_posting_age_days": 30,  # ordinary: restored
    }
    for k, v in hostile.items():
        _put_setting(k, v)
    archive = create_backup(tmp_path)

    _fresh_db()  # this computer: its own, different values
    _put_setting("llm.backends.local.base_url", "http://127.0.0.1:11434/v1")
    _put_setting("llm.mode", "local")
    _put_setting("keep_awake", False)

    result = restore_backup(archive)

    assert _get_setting("llm.backends.local.base_url") == "http://127.0.0.1:11434/v1"
    assert _get_setting("llm.mode") == "local"
    assert _get_setting("keep_awake") is False
    for gone in ("llm.backends.frontier.base_url", "mcp.enabled", "server.bind_all",
                 "hosted_tiers_note", "updates.enabled", "ui.autostart"):
        assert _get_setting(gone) is None, gone
    assert _get_setting("ui.scheme") == "dark"
    assert _get_setting("max_posting_age_days") == 30
    assert result.settings_kept == 9  # every local-only key the backup carried that differed
    assert "9 settings" in " ".join(result.summary_lines())
    assert EVIL_URL not in " ".join(result.summary_lines())  # a value is never quoted


def test_restore_never_turns_confidential_mode_off_but_may_turn_it_on(tmp_path):
    _fresh_db()
    _put_setting("confidential_mode", False)
    off_backup = create_backup(tmp_path)
    _fresh_db()
    _put_setting("confidential_mode", True)
    restore_backup(off_backup)
    assert _get_setting("confidential_mode") is True  # live ON stays ON

    _fresh_db()
    _put_setting("confidential_mode", True)
    on_backup = create_backup(tmp_path)
    _fresh_db()
    _put_setting("confidential_mode", False)
    restore_backup(on_backup)
    assert _get_setting("confidential_mode") is True  # stricter is always allowed


def _config_zip(tmp_path, settings_text: str, extra: dict[str, bytes] | None = None):
    """A normal backup whose config/settings.yml (and extra entries) are replaced."""
    _fresh_db()
    archive = create_backup(tmp_path)
    out = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename != "config/settings.yml":
                dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("config/settings.yml", settings_text)
        for name, data in (extra or {}).items():
            dst.writestr(name, data)
    return out


def test_restore_merges_settings_yml_and_keeps_this_computers_route_and_keys(tmp_path):
    import yaml

    paths.seed_config()
    live_text = (paths.config_dir() / "settings.yml").read_text(encoding="utf-8")
    live = yaml.safe_load(live_text)
    crafted = yaml.safe_load(live_text)
    crafted["llm"]["backends"]["local"]["base_url"] = EVIL_URL
    first_hosted = next(n for n, b in crafted["llm"]["backends"].items() if b.get("external"))
    crafted["llm"]["backends"][first_hosted]["base_url"] = EVIL_URL
    crafted["llm"]["backends"]["sneaky"] = {"base_url": EVIL_URL, "external": False}
    crafted["mcp"] = {"enabled": True, "port": 4999}
    crafted["server"] = {"bind_all": True}
    crafted.setdefault("ui", {})["autostart"] = True
    crafted["max_posting_age_days"] = 31  # ordinary: restored
    archive = _config_zip(tmp_path, yaml.safe_dump(crafted))
    paths.seed_config()

    result = restore_backup(archive)

    got = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    assert got["llm"] == live["llm"]  # the whole route: urls, backends, tiers, keys' env names
    assert "sneaky" not in got["llm"]["backends"]
    assert got.get("mcp") == live.get("mcp") and got.get("server") == live.get("server")
    assert (got.get("ui") or {}).get("autostart") == (live.get("ui") or {}).get("autostart")
    assert got["max_posting_age_days"] == 31
    assert EVIL_URL not in (paths.config_dir() / "settings.yml").read_text(encoding="utf-8")
    assert result.settings_kept >= 6


def test_restore_of_an_unchanged_settings_yml_is_copied_verbatim(tmp_path):
    paths.seed_config()
    text = (paths.config_dir() / "settings.yml").read_text(encoding="utf-8")
    archive = _config_zip(tmp_path, text + "\n# a comment the operator wrote\n")
    result = restore_backup(archive)
    assert (paths.config_dir() / "settings.yml").read_text(encoding="utf-8").endswith(
        "# a comment the operator wrote\n")
    assert result.settings_kept == 0


def test_a_restored_backup_brings_no_task_and_no_queued_run_mode(tmp_path):
    """D-SEC-20: the task queue is never restored, so no restored task can carry an online
    confirmation (or anything else), and the queued run mode row is dropped."""
    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    payload = {"opportunity_id": 7, "consented_model": "m", "consented_destination": "d",
               "online_approval": {"tier1": ["x"]}, "nested": {"online_approval": {}}}
    con.execute(
        "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, created_at, "
        "updated_at) VALUES ('score', ?, 'pending', 0, 0, 3, '2026-01-01', '2026-01-01')",
        (json.dumps(payload),),
    )
    con.commit()
    con.close()
    _put_setting("pending_run_mode", {"mode": "hosted", "online_approval": {"tier1": ["x"]}})
    archive = create_backup(tmp_path)
    _fresh_db()

    result = restore_backup(archive)

    con = sqlite3.connect(str(paths.db_path()))
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
    con.close()
    assert _get_setting("pending_run_mode") is None
    assert result.tasks_not_restored == 1


def test_documents_entries_only_land_as_pdfs_in_the_two_document_folders(tmp_path):
    """The crafted documents/ branch (D-SEC-10): documents/kestrel.db, documents/ml/x.pkl and a
    stray file are skipped and counted; the one legitimate PDF lands. The live database is the
    scrubbed staged one, not the planted file."""
    paths.seed_config()
    _fresh_db()
    _put_setting("ui.scheme", "dark")
    archive = create_backup(tmp_path)
    crafted = tmp_path / "crafted_docs.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(crafted, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("documents/kestrel.db", b"NOT A DATABASE")
        dst.writestr("documents/ml/x.pkl", b"planted pickle")
        dst.writestr("documents/documents/a.pdf", b"%PDF-1.4 legit")
        dst.writestr("documents/prep_packs/p.pdf", b"%PDF-1.4 legit prep")
        dst.writestr("documents/documents/run.exe", b"MZ")
        dst.writestr("documents/documents/sub/deep.pdf", b"%PDF too deep")
        dst.writestr("documents/settings.yml", b"llm: {}")
    _fresh_db()
    live_before = paths.db_path().read_bytes()

    result = restore_backup(crafted)

    data = paths.data_dir()
    assert (data / "documents" / "a.pdf").read_bytes() == b"%PDF-1.4 legit"
    assert (data / "prep_packs" / "p.pdf").read_bytes() == b"%PDF-1.4 legit prep"
    assert not (data / "ml" / "x.pkl").exists()
    assert not (data / "documents" / "run.exe").exists()
    assert not (data / "documents" / "sub").exists()
    assert not (data / "settings.yml").exists()
    assert sorted(result.document_files) == ["documents/a.pdf", "prep_packs/p.pdf"]
    assert result.entries_refused == 5
    assert "5 files" in " ".join(result.summary_lines())
    con = sqlite3.connect(str(paths.db_path()))  # still a real, restored database
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert _get_setting("ui.scheme") == "dark"
    assert paths.db_path().read_bytes() != b"NOT A DATABASE"
    assert live_before != b""


def test_a_documents_entry_that_climbs_out_is_refused_and_nothing_lands(tmp_path):
    """A `..` entry makes the whole archive unsafe (refused before anything is written)."""
    _fresh_db()
    archive = create_backup(tmp_path)
    crafted = tmp_path / "crafted_up.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(crafted, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("documents/../x", b"escaped")
        dst.writestr("documents/documents/a.pdf", b"%PDF-1.4 legit")
    with pytest.raises(InvalidBackupError):
        restore_backup(crafted)
    assert not (paths.data_dir() / "x").exists()
    assert not (paths.data_dir() / "documents" / "a.pdf").exists()


def test_a_document_folder_that_is_a_link_out_cannot_be_written_through(tmp_path, monkeypatch):
    """The documents folder as a link that leads out: `confined_file` resolves both the folder and
    the target through the link, so the target looks "inside" its own folder. The restore also
    requires the file to sit in the folder as it resolves from the data folder. Simulated by what
    `confined_file` answers for such a link, so it needs no symbolic-link privilege."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _fresh_db()
    archive = create_backup(tmp_path)
    crafted = tmp_path / "crafted_link.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(crafted, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr("documents/documents/a.pdf", b"%PDF-1.4 legit")
    monkeypatch.setattr(paths, "confined_file", lambda stored, root: outside / "a.pdf")
    result = restore_backup(crafted)
    assert not (outside / "a.pdf").exists()
    assert result.entries_refused == 1 and result.document_files == []


def test_a_normal_backup_round_trips_fully_except_kept_local_only_settings(tmp_path):
    """The invariant: restore on the machine that made the backup loses nothing. Documents and
    prep packs, every config file, ordinary settings and rows all come back; nothing is counted
    as kept or refused."""
    paths.seed_config()
    asyncio.run(_seed_two_orgs())
    _put_setting("ui.scheme", "dark")
    _put_setting("llm.mode", "local")
    docs = paths.data_dir() / "documents"
    packs = paths.data_dir() / "prep_packs"
    docs.mkdir(parents=True, exist_ok=True)
    packs.mkdir(parents=True, exist_ok=True)
    (docs / "resume.pdf").write_bytes(b"%PDF resume")
    (packs / "1_pack.pdf").write_bytes(b"%PDF pack")
    archive = create_backup(tmp_path)
    originals = {p.name: p.read_bytes() for p in paths.config_dir().glob("*.yml")}

    asyncio.run(_wipe_and_reseed_other())
    _put_setting("llm.mode", "local")  # this computer's own value: unchanged since the backup
    (docs / "resume.pdf").unlink()
    (packs / "1_pack.pdf").unlink()
    for p in paths.config_dir().glob("*.yml"):
        p.unlink()

    result = restore_backup(archive)

    assert result.settings_kept == 0 and result.entries_refused == 0
    assert result.consents_cleared == 0
    assert (docs / "resume.pdf").read_bytes() == b"%PDF resume"
    assert (packs / "1_pack.pdf").read_bytes() == b"%PDF pack"
    assert {p.name: p.read_bytes() for p in paths.config_dir().glob("*.yml")} == originals
    assert _get_setting("ui.scheme") == "dark"
    assert asyncio.run(_org_names()) == {"acme robotics", "beacon health"}


def test_web_restore_names_the_number_of_kept_settings(tmp_path):
    _fresh_db()
    _put_setting("llm.mode", "hosted")
    _put_setting("mcp.enabled", True)
    archive = create_backup(tmp_path)
    _fresh_db()
    with _client() as c:
        r = c.post(
            "/settings/restore",
            data={"from_settings": "1"},
            files={"backup_file": ("kestrel-backup.zip", archive.read_bytes(), "application/zip")},
        )
    assert r.status_code == 200
    assert "2 settings that only this computer can change were kept" in r.text
    assert "hosted" not in r.text.lower().replace("kestrel-backup", "")


OUTBOUND_KEYS = {
    "notify.webhook_url": "http://127.0.0.1:9/hook", "notify.ntfy_topic": "evil-topic",
    "research.searx_url": "http://127.0.0.1:9/s", "ingest.user_agent": "Evil/1.0",
    "ingest.respect_robots": False, "updates.feed_url": "http://127.0.0.1:9/latest.json",
}


def test_restore_keeps_this_computers_outbound_destinations(tmp_path):
    """Where a notice, a search or a fetch goes, and how politely, is this computer's choice."""
    _fresh_db()
    for k, v in OUTBOUND_KEYS.items():
        _put_setting(k, v)
    archive = create_backup(tmp_path)
    _fresh_db()
    _put_setting("notify.ntfy_topic", "mine")
    restore_backup(archive)
    assert _get_setting("notify.ntfy_topic") == "mine"
    others = [k for k in OUTBOUND_KEYS if k != "notify.ntfy_topic"]
    assert [k for k in others if _get_setting(k) is not None] == []


def test_restore_keeps_this_computers_outbound_destinations_in_settings_yml(tmp_path):
    import yaml

    paths.seed_config()
    live = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    crafted = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    for k, v in OUTBOUND_KEYS.items():
        a, b = k.split(".")
        crafted.setdefault(a, {})[b] = v
    archive = _config_zip(tmp_path, yaml.safe_dump(crafted))
    paths.seed_config()
    restore_backup(archive)
    got = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    for k in OUTBOUND_KEYS:
        a, b = k.split(".")
        assert (got.get(a) or {}).get(b) == (live.get(a) or {}).get(b), k


# ------------------------------------------------------------- restore opt-in (D-SEC-11)
PUBLIC_URL = "http://203.0.113.50:8000/v1"  # a documentation address: invented, never reached


def _crafted_settings_archive(tmp_path):
    """A backup whose rows send the LOCAL model to an internet address, turn MCP on and move the
    notice, search and update destinations, plus Hosted mode switched on."""
    _fresh_db()
    for k, v in {
        "llm.backends.local.base_url": PUBLIC_URL, "llm.mode": "hosted", "mcp.enabled": True,
        "mcp.port": 4999, "notify.webhook_url": "https://hooks.example.invalid/abc/SECRETTOKEN",
        "research.searx_url": "http://192.168.1.9:8080/", "updates.feed_url": "http://10.0.0.2/f",
        "keep_awake": True, "ui.autostart": True, "server.bind_all": True,
        "llm.hosted_mode.enabled": True, "llm.hosted_mode.acknowledged_at": "2026-01-01",
        "ui.scheme": "dark",
    }.items():
        _put_setting(k, v)
    return create_backup(tmp_path)


def _mine() -> None:
    _fresh_db()
    _put_setting("llm.backends.local.base_url", "http://127.0.0.1:11434/v1")
    _put_setting("llm.mode", "local")
    _put_setting("keep_awake", False)
    _put_setting("llm.hosted_mode.enabled", False)
    _put_setting("server.bind_all", False)


def test_restore_unticked_keeps_the_live_values(tmp_path):
    archive = _crafted_settings_archive(tmp_path)
    _mine()
    result = restore_backup(archive)  # the default: the box is not ticked
    assert _get_setting("llm.backends.local.base_url") == "http://127.0.0.1:11434/v1"
    assert _get_setting("llm.mode") == "local"
    assert _get_setting("mcp.enabled") is None and _get_setting("keep_awake") is False
    assert not result.local_settings_restored
    assert _get_setting("ui.scheme") == "dark"


def test_restore_ticked_restores_the_ai_and_connection_settings(tmp_path):
    archive = _crafted_settings_archive(tmp_path)
    _mine()
    result = restore_backup(archive, restore_local_settings=True)
    assert result.local_settings_restored
    assert _get_setting("llm.backends.local.base_url") == PUBLIC_URL
    assert _get_setting("llm.mode") == "hosted"
    assert _get_setting("mcp.enabled") is True and _get_setting("mcp.port") == 4999
    assert _get_setting("keep_awake") is True and _get_setting("ui.autostart") is True
    assert _get_setting("updates.feed_url") == "http://10.0.0.2/f"
    # What never comes from a backup, ticked or not: consent state and the listening switch.
    assert _get_setting("llm.hosted_mode.enabled") is False
    assert _get_setting("llm.hosted_mode.acknowledged_at") is None
    assert _get_setting("server.bind_all") is False
    assert PUBLIC_URL not in " ".join(result.summary_lines())


def test_restore_ticked_still_brings_no_task(tmp_path):
    _fresh_db()
    con = sqlite3.connect(str(paths.db_path()))
    payload = {"opportunity_id": 7, "consented_model": "m", "online_approval": {"tier1": ["x"]}}
    con.execute(
        "INSERT INTO task (type, payload, status, priority, attempts, max_attempts, created_at, "
        "updated_at) VALUES ('t1', ?, 'pending', 0, 0, 3, '2026-01-01', '2026-01-01')",
        (json.dumps(payload),))
    con.commit()
    con.close()
    archive = create_backup(tmp_path)
    _fresh_db()
    result = restore_backup(archive, restore_local_settings=True)
    con = sqlite3.connect(str(paths.db_path()))
    try:
        assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
    finally:
        con.close()
    assert result.tasks_not_restored == 1


def test_restore_ticked_takes_settings_yml_local_keys_from_the_backup(tmp_path):
    import yaml

    paths.seed_config()
    crafted = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    crafted["llm"]["backends"]["local"]["base_url"] = PUBLIC_URL
    crafted["llm"]["hosted_mode"] = {"enabled": True, "acknowledged_at": "2026-01-01"}
    crafted["server"] = {"bind_all": True}
    crafted["mcp"] = {"enabled": True, "port": 4999}
    archive = _config_zip(tmp_path, yaml.safe_dump(crafted))
    paths.seed_config()
    restore_backup(archive, restore_local_settings=True)
    got = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    assert got["llm"]["backends"]["local"]["base_url"] == PUBLIC_URL
    assert got["mcp"] == {"enabled": True, "port": 4999}
    assert not (got.get("server") or {}).get("bind_all")
    assert not (got["llm"].get("hosted_mode") or {}).get("enabled")


def test_the_preview_lists_the_internet_address_for_the_local_backend(tmp_path):
    from kestrel.services.backup_preview import preview_backup

    archive = _crafted_settings_archive(tmp_path)
    _mine()
    live_before = _get_setting("llm.backends.local.base_url")
    pv = preview_backup(archive)
    text = " ".join(f"{i.label} {i.value}" for i in pv.servers)
    assert "203.0.113.50:8000" in text and "the internet" in text
    assert pv.any_internet
    assert any(i.label == "Update feed" for i in pv.destinations)
    shown = " ".join(i.value for i in pv.destinations)
    assert "SECRETTOKEN" not in shown and "hooks.example.invalid" in shown  # host only
    assert any(i.label.startswith("AI assistant") for i in pv.switches)
    assert _get_setting("llm.backends.local.base_url") == live_before  # nothing was applied


def test_the_preview_reads_the_settings_yml_and_never_looks_a_name_up(tmp_path, monkeypatch):
    import socket

    import yaml

    from kestrel.services.backup_preview import preview_backup

    paths.seed_config()
    crafted = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    crafted["llm"]["backends"]["local"]["base_url"] = "http://models.example.invalid:1/v1"
    archive = _config_zip(tmp_path, yaml.safe_dump(crafted))
    looked_up: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: looked_up.append(host) or [])
    pv = preview_backup(archive)
    assert looked_up == []
    assert any("models.example.invalid" in i.value and i.warn for i in pv.servers)


def test_web_preview_then_restore_ticked_and_unticked(tmp_path):
    archive = _crafted_settings_archive(tmp_path)
    _mine()
    files = {"backup_file": ("b.zip", archive.read_bytes(), "application/zip")}
    with _client() as c:
        r = c.post("/settings/restore/preview", files=files)
        assert r.status_code == 200
        assert "203.0.113.50" in r.text and "st-restore-local" in r.text
        assert "SECRETTOKEN" not in r.text and "checked" not in r.text.split("<input")[1]
        assert _get_setting("llm.mode") == "local"  # previewing applied nothing
        r = c.post("/settings/restore", data={"from_settings": "1"}, files=files)
        assert r.status_code == 200 and _get_setting("llm.mode") == "local"
        r = c.post("/settings/restore", data={"from_settings": "1", "restore_local": "1"},
                   files=files)
        assert "restored, as you chose" in r.text
        assert _get_setting("llm.mode") == "hosted"


def test_a_ticked_restore_never_takes_a_backends_key_name(tmp_path):
    """D-SEC-11: a backup that points a LAN backend at an online provider's key name must not
    make the real key travel there: key fields are always this computer's."""
    _fresh_db()
    _put_setting("llm.backends.local.api_key_env", "OPENROUTER_API_KEY")
    _put_setting("llm.backends.local.api_key", "inline-secret")
    _put_setting("llm.backends.lan_x", {"base_url": "http://192.168.2.25:8000/v1",
                                        "api_key_env": "OPENROUTER_API_KEY", "api_key": "z"})
    archive = create_backup(tmp_path)
    _fresh_db()
    _put_setting("llm.backends.local.api_key_env", "MY_LOCAL_KEY")
    restore_backup(archive, restore_local_settings=True)
    assert _get_setting("llm.backends.local.api_key_env") == "MY_LOCAL_KEY"
    assert _get_setting("llm.backends.local.api_key") is None
    row = _get_setting("llm.backends.lan_x")
    assert row["base_url"] == "http://192.168.2.25:8000/v1"  # the rest is restored
    assert "api_key_env" not in row and "api_key" not in row


def test_a_ticked_restore_keeps_key_names_in_settings_yml(tmp_path):
    import yaml

    paths.seed_config()
    crafted = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    crafted["llm"]["backends"]["local"]["api_key_env"] = "OPENROUTER_API_KEY"
    crafted["llm"]["backends"]["local"]["base_url"] = "http://192.168.2.25:8000/v1"
    archive = _config_zip(tmp_path, yaml.safe_dump(crafted))
    paths.seed_config()
    restore_backup(archive, restore_local_settings=True)
    got = yaml.safe_load((paths.config_dir() / "settings.yml").read_text(encoding="utf-8"))
    assert got["llm"]["backends"]["local"]["base_url"] == "http://192.168.2.25:8000/v1"
    assert got["llm"]["backends"]["local"].get("api_key_env") != "OPENROUTER_API_KEY"
