"""Everything the shared-file walker refuses is reported in true words, never dropped in silence
and never described as something else: a backup names each file it left out, a refused profile
export says it was refused, a refused download is not called missing, and the diagnostics bundle
names every log it left out."""

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kestrel import paths
from kestrel.cli import app
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import DocRender, PrepPack
from kestrel.services import diagnostics
from tests import role_seed
from tests.symlink_probe import link_folder, symlink_or_skip

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

LINK = "it is a link, and Kestrel does not follow links"
SECOND_NAME = "it has a second name elsewhere on this computer (a hard link)"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _outside(tmp_path: Path, name: str = "outside.txt") -> Path:
    target = tmp_path / "elsewhere" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"INVENTED OUTSIDE BYTES")
    return target


# ----------------------------------------------------------------------------- backup
def test_a_backup_names_every_file_it_left_out_and_says_links_are_not_followed(tmp_path):
    outside = _outside(tmp_path)
    folder = tmp_path / "elsewhere-folder"
    folder.mkdir()
    cfg, docs, packs, ml = (paths.config_dir(), paths.data_dir() / "documents",
                            paths.data_dir() / "prep_packs", paths.ml_dir())
    for d in (cfg, docs, packs, ml):
        d.mkdir(parents=True, exist_ok=True)
    (cfg / "profile.yml").write_bytes(b"name: Invented Person\n")
    symlink_or_skip(cfg / "extra.yml", outside)
    os.link(outside, cfg / "hard.yml")
    link_folder(cfg / "linked", folder)
    symlink_or_skip(docs / "cv.pdf", outside)
    os.link(outside, packs / "pack.pdf")
    symlink_or_skip(ml / "reranker_employment_v9.pkl", outside)
    out = tmp_path / "out"

    result = CliRunner().invoke(app, ["backup", "--out", str(out)])

    assert result.exit_code == 0, result.output
    expected = [
        f"config/extra.yml: {LINK}.",
        f"config/hard.yml: {SECOND_NAME}.",
        f"config/linked: {LINK}.",
        f"documents/documents/cv.pdf: {LINK}.",
        f"documents/prep_packs/pack.pdf: {SECOND_NAME}.",
        f"ml/reranker_employment_v9.pkl: {LINK}.",
    ]
    links_line = ("Kestrel does not follow links when it makes a backup: a link is left out, "
                  "and so is whatever it leads to.")
    for line in [*expected, links_line, "Kestrel left these files out of this backup:"]:
        assert line in result.output, line
    (archive,) = out.glob("*.zip")
    with zipfile.ZipFile(archive) as z:
        note = z.read("LEFT-OUT.txt").decode("utf-8")
        assert b"INVENTED OUTSIDE BYTES" not in b"".join(z.read(n) for n in z.namelist())
    assert note.splitlines() == ["Kestrel left these files out of this backup:", "",
                                 *(f"- {line}" for line in expected), "", links_line]


def test_a_backup_with_nothing_left_out_carries_no_note(tmp_path):
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "profile.yml").write_bytes(b"name: Invented Person\n")
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["backup", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "left these files out" not in result.output
    (archive,) = out.glob("*.zip")
    with zipfile.ZipFile(archive) as z:
        assert "LEFT-OUT.txt" not in z.namelist()


def test_a_documents_folder_that_is_a_link_is_named_as_left_out(tmp_path):
    folder = tmp_path / "elsewhere-docs"
    folder.mkdir()
    docs = paths.data_dir() / "documents"
    docs.parent.mkdir(parents=True, exist_ok=True)
    link_folder(docs, folder)
    result = CliRunner().invoke(app, ["backup", "--out", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert ("documents/documents/ (the whole folder): its folder is a link or is not in its "
            "own place.") in result.output


# ---------------------------------------------------------------------- profile export
def test_a_profile_yml_that_is_a_link_is_said_to_be_refused_not_missing(tmp_path):
    cfg = paths.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(cfg / "profile.yml", _outside(tmp_path, "profile.yml"))
    said = f"Your profile.yml was not exported: {LINK}."

    with _client() as c:
        r = c.get("/backup/export-profile")
        page = c.get("/backup").text

    assert r.status_code == 404
    assert said in r.text and "No profile.yml yet" not in r.text
    assert b"INVENTED OUTSIDE BYTES" not in r.content
    assert said in page and "no profile.yml yet" not in page


def test_a_profile_yml_with_a_second_name_is_said_to_be_refused(tmp_path):
    cfg = paths.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "profile.yml").write_bytes(b"name: Invented Person\n")
    os.link(cfg / "profile.yml", tmp_path / "copy.yml")
    with _client() as c:
        r = c.get("/backup/export-profile")
    assert r.status_code == 404
    assert f"Your profile.yml was not exported: {SECOND_NAME}." in r.text


def test_no_profile_yml_still_says_there_is_none_yet():
    with _client() as c:
        r = c.get("/backup/export-profile")
    assert r.status_code == 404 and "No profile.yml yet" in r.text


# --------------------------------------------------------------------------- downloads
def _db(coro_fn):
    async def go():
        async with get_sessionmaker()() as s:
            return await coro_fn(s)

    return asyncio.run(go())


def test_a_resume_pdf_with_a_second_name_is_said_so_not_missing(tmp_path):
    """(A resume PDF that is a symlink resolves outside the folder, so the route makes the PDF
    again instead; a hard link stays inside and is refused with its reason.)"""
    ids = asyncio.run(role_seed.seed_async())

    async def stored(s):
        return (await s.get(DocRender, ids["render"])).pdf_path

    pdf = Path(_db(stored))
    pdf.unlink()
    os.link(_outside(tmp_path, "resume.pdf"), pdf)
    with _client() as c:
        r = c.get(f"/renders/{ids['render']}/download")
    assert r.status_code == 404
    assert r.text == f"The PDF was not served: {SECOND_NAME}."
    assert b"INVENTED OUTSIDE BYTES" not in r.content


def test_a_prep_pack_pdf_that_is_a_link_is_said_to_be_a_link_not_missing(tmp_path):
    ids = asyncio.run(role_seed.seed_async())
    packs = paths.prep_packs_dir()
    packs.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(packs / "linked.pdf", _outside(tmp_path, "pack.pdf"))

    async def add(s):
        pack = PrepPack(opportunity_id=ids["strong"], content_md="x",
                        pdf_path=str(packs / "linked.pdf"))
        s.add(pack)
        await s.commit()
        return pack.id

    pack_id = _db(add)
    with _client() as c:
        r = c.get(f"/prep/{pack_id}/download")
    assert r.status_code == 404
    assert f"The PDF was not served: {LINK}." in r.text and "PDF not found." not in r.text
    assert b"INVENTED OUTSIDE BYTES" not in r.content


def test_a_prep_pack_pdf_with_a_second_name_is_said_so_and_a_missing_one_is_missing(tmp_path):
    ids = asyncio.run(role_seed.seed_async())
    packs = paths.prep_packs_dir()
    packs.mkdir(parents=True, exist_ok=True)
    outside = _outside(tmp_path, "pack.pdf")
    os.link(outside, packs / "hard.pdf")

    async def add(s, name):
        pack = PrepPack(opportunity_id=ids["strong"], content_md="x",
                        pdf_path=str(packs / name))
        s.add(pack)
        await s.commit()
        return pack.id

    hard = _db(lambda s: add(s, "hard.pdf"))
    gone = _db(lambda s: add(s, "never-made.pdf"))
    with _client() as c:
        r = c.get(f"/prep/{hard}/download")
        missing = c.get(f"/prep/{gone}/download")
    assert r.status_code == 404
    assert f"The PDF was not served: {SECOND_NAME}." in r.text
    assert b"INVENTED OUTSIDE BYTES" not in r.content
    assert missing.status_code == 404 and "PDF not found." in missing.text


# ------------------------------------------------------------------------- diagnostics
def test_the_diagnostics_bundle_names_every_log_it_left_out(tmp_path):
    paths.ensure_dirs()
    logs = paths.logs_dir()
    (logs / "kestrel.log").write_text("real line\n", encoding="utf-8")
    outside = _outside(tmp_path, "notes.txt")
    symlink_or_skip(logs / "kestrel.log.3", outside)
    os.link(outside, logs / "other.log")

    tails = dict(diagnostics.log_tails(diagnostics.make_scrubber(None)))

    assert tails["logs/LEFT-OUT.txt"].splitlines() == [
        "Kestrel left these log files out of this bundle:",
        f"- kestrel.log.3: {LINK}.",
        f"- other.log: {SECOND_NAME}.",
        "Kestrel does not follow links when it collects logs: a link is left out, and so is "
        "whatever it leads to.",
    ]
    assert f"# kestrel.log.3 left out: {LINK}" in tails["logs/kestrel.log"]
    assert "INVENTED OUTSIDE BYTES" not in "".join(tails.values())


def _summary() -> dict:
    from kestrel.config import load_settings

    return diagnostics.summary(load_settings(), diagnostics.make_scrubber(None))


def test_the_bundle_leaves_out_a_database_with_a_second_name_and_says_so(tmp_path):
    from kestrel.db import migrate

    migrate.run_migrations()
    os.link(paths.db_path(), tmp_path / "second-name.db")
    got = _summary()
    said = "left out: the database file has a second name elsewhere on this computer (a hard link)"
    assert got["counts"] == said and got["tasks"] == said


def test_the_bundle_reads_a_database_in_a_data_folder_moved_behind_a_link(tmp_path):
    import shutil

    from kestrel.db import migrate

    moved = tmp_path / "other-drive" / "data"
    moved.mkdir(parents=True)
    shutil.rmtree(paths.data_dir(), ignore_errors=True)
    paths.data_dir().parent.mkdir(parents=True, exist_ok=True)
    link_folder(paths.data_dir(), moved)
    migrate.run_migrations()
    assert isinstance(_summary()["counts"], dict)
