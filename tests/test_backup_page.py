"""The /backup page: download streams the real archive and cleans up its temp copy, export returns
profile.yml alone (404 when only the shipped example exists), import refuses a bad file without
touching the current profile and writes a good one with a .bak of the previous. .env is never read
or changed by any of it. Cloud-safe: file I/O and a real migrated SQLite database only."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.services import backup as backup_service
from kestrel.web.pages import backup as backup_page

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_GOOD_PROFILE = (
    "employment:\n"
    "  track: employment\n"
    "  full_name: Test Operator\n"
    "  employment_terms:\n"
    "    comp_floor_usd: 180000\n"
    "  geo_rules:\n"
    "    allow_remote: true\n"
)
_REPLACEMENT_PROFILE = _GOOD_PROFILE.replace("Test Operator", "Second Operator")


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _write_env() -> tuple[Path, str]:
    dotenv = paths.dotenv_path()
    dotenv.parent.mkdir(parents=True, exist_ok=True)
    text = "OPENROUTER_API_KEY=do-not-leak\n"
    dotenv.write_text(text, encoding="utf-8")
    return dotenv, text


# ---------------------------------------------------------------------------------------- page
def test_backup_page_renders_and_says_what_is_excluded():
    with _client() as c:
        r = c.get("/backup")
    assert r.status_code == 200
    body = r.text
    assert 'href="/backup/download"' in body
    assert "Your saved keys" in body and "API key" in body
    assert "kestrel.db" in body
    assert 'hx-post="/backup/import-profile"' in body
    assert "no profile.yml yet" in body  # only the shipped example exists in a fresh home
    assert 'href="/settings"' in body  # restore lives there; not duplicated here
    assert "—" not in body.split("<main")[1]


# ------------------------------------------------------------------------------------ download
def test_download_streams_a_kestrel_backup_zip_and_discards_the_temp_copy(monkeypatch):
    dotenv, _ = _write_env()
    paths.seed_config()
    discarded: list[Path] = []
    original = backup_service.discard_temp_backup

    def _spy(archive):
        discarded.append(Path(archive))
        original(archive)

    # Patched on the SERVICE module after the page module was imported: the handler binds the
    # function at request time, so this wrapper is what the BackgroundTask runs.
    monkeypatch.setattr(backup_service, "discard_temp_backup", _spy)
    assert not hasattr(backup_page, "_discard")

    try:
        with _client() as c:
            r = c.get("/backup/download")
    finally:
        dotenv.unlink(missing_ok=True)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert "kestrel-backup-" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert "kestrel.db" in names
        assert any(n.startswith("config/") and n.endswith(".yml") for n in names)
        assert not any(".env" in n for n in names)
        for n in names:
            assert b"do-not-leak" not in zf.read(n)
    # The BackgroundTask ran after the response and removed the temp archive and its directory.
    assert len(discarded) == 1
    assert not discarded[0].exists()
    assert not discarded[0].parent.exists()


def test_discard_only_removes_a_staging_directory_it_made(tmp_path):
    keep = tmp_path / "real-data"
    keep.mkdir()
    stray = keep / "kestrel-backup-x.zip"
    stray.write_bytes(b"PK")
    backup_service.discard_temp_backup(stray)
    assert not stray.exists()
    assert keep.exists()  # a non-staging parent is never removed wholesale

    archive = backup_service.create_backup_to_temp()
    assert archive.exists()
    backup_service.discard_temp_backup(archive)
    assert not archive.parent.exists()


# -------------------------------------------------------------------------------------- export
def test_export_profile_is_404_when_only_the_example_exists():
    with _client() as c:
        r = c.get("/backup/export-profile")
    assert r.status_code == 404
    assert "No profile.yml yet" in r.text


def test_export_profile_returns_the_operators_yaml():
    paths.ensure_dirs()
    # newline="\n": the export streams the file's bytes, and Windows text mode would otherwise
    # write CRLF and the comparison below would fail there only.
    backup_service.profile_path().write_text(_GOOD_PROFILE, encoding="utf-8", newline="\n")
    with _client() as c:
        r = c.get("/backup/export-profile")
        page = c.get("/backup")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    assert 'filename="profile.yml"' in r.headers["content-disposition"]
    assert r.text == _GOOD_PROFILE
    assert 'href="/backup/export-profile"' in page.text


# -------------------------------------------------------------------------------------- import
def test_import_rejects_bad_files_and_leaves_the_current_profile_alone():
    paths.ensure_dirs()
    target = backup_service.profile_path()
    target.write_text(_GOOD_PROFILE, encoding="utf-8")
    bak = target.with_name(backup_service.PROFILE_BAK)
    dotenv, env_text = _write_env()

    bad_uploads = {
        "not-yaml.yml": b"employment: [unclosed",
        "no-track.yml": b"something_else:\n  key: value\n",
        "scalar.yml": b"just a string\n",
        "binary.yml": b"\xff\xfe\x00\x01",
        "int-keys.yml": b"employment:\n  1: one\n  2: two\n",
    }
    try:
        with _client() as c:
            for name, data in bad_uploads.items():
                r = c.post(
                    "/backup/import-profile",
                    files={"profile_file": (name, data, "application/yaml")},
                )
                assert r.status_code == 200, name
                assert "warnbox" in r.text, name
                assert target.read_text(encoding="utf-8") == _GOOD_PROFILE, name
                assert not bak.exists() or bak.read_text(encoding="utf-8") == _GOOD_PROFILE, name
            r = c.post("/backup/import-profile", data={})
            assert "Choose a profile.yml" in r.text
        assert dotenv.read_text(encoding="utf-8") == env_text
        assert not target.with_name("profile.yml.incoming").exists()
    finally:
        dotenv.unlink(missing_ok=True)


def test_import_writes_a_valid_profile_and_keeps_the_previous_as_bak():
    from kestrel.pipeline.profile import load_profile

    paths.ensure_dirs()
    target = backup_service.profile_path()
    target.write_text(_GOOD_PROFILE, encoding="utf-8")
    bak = target.with_name(backup_service.PROFILE_BAK)
    dotenv, env_text = _write_env()
    try:
        with _client() as c:
            r = c.post(
                "/backup/import-profile",
                files={"profile_file": ("profile.yml", _REPLACEMENT_PROFILE, "application/yaml")},
            )
        assert r.status_code == 200
        assert "Profile imported for employment" in r.text
        assert "profile.yml.bak" in r.text
        assert target.read_text(encoding="utf-8") == _REPLACEMENT_PROFILE
        assert bak.read_text(encoding="utf-8") == _GOOD_PROFILE
        assert load_profile("employment").full_name == "Second Operator"
        assert dotenv.read_text(encoding="utf-8") == env_text
    finally:
        dotenv.unlink(missing_ok=True)


def test_import_with_no_previous_profile_creates_it_without_a_bak():
    target = backup_service.profile_path()
    assert not target.exists()
    with _client() as c:
        r = c.post(
            "/backup/import-profile",
            files={"profile_file": ("profile.yml", _GOOD_PROFILE, "application/yaml")},
        )
    assert r.status_code == 200
    assert "Profile imported for employment" in r.text
    assert "profile.yml.bak" not in r.text
    assert target.read_text(encoding="utf-8") == _GOOD_PROFILE
    assert not target.with_name(backup_service.PROFILE_BAK).exists()
