"""D-UI-41: in the desktop app the diagnostics zip is saved to the Downloads folder and shown in
Explorer, never handed to WebView2 as a download (the operator saw Edge's downloads flyout); in a
plain browser it stays a normal download. The desktop shell is faked through desktop_bridge; the
bundle build is replaced by a stub (its contents and privacy are test_about_page's job)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.services import diagnostics
from kestrel.web import desktop_bridge

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

NAME = "kestrel-diagnostics-0.0.0-20261015-1200.zip"


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


@pytest.fixture
def downloads(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(paths, "downloads_dir", lambda: folder)
    return folder


@pytest.fixture
def stub_build(monkeypatch) -> list[Path]:
    """build_bundle writes a tiny real zip into whatever folder the route asks for."""
    seen: list[Path] = []

    def _build(settings, *, out_dir, redaction=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        archive = out / NAME
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("README.txt", "stub")
        seen.append(archive)
        return archive

    monkeypatch.setattr(diagnostics, "build_bundle", _build)
    return seen


@pytest.fixture
def shell():
    """A fake desktop shell: records each reveal request instead of opening Explorer."""
    calls: list[Path] = []
    desktop_bridge.set_reveal_handler(calls.append)
    yield calls
    desktop_bridge.set_reveal_handler(None)


def test_plain_browser_keeps_the_normal_download():
    assert not desktop_bridge.can_reveal()
    body = _client().get("/about").text
    assert 'href="/about/diagnostics"' in body and "Download diagnostics" in body
    assert 'data-testid="diagnostics-save"' not in body


def test_desktop_saves_to_downloads_and_opens_the_folder(downloads, stub_build, shell):
    c = _client()
    body = c.get("/about").text
    assert 'data-testid="diagnostics-save"' in body and 'hx-post="/about/diagnostics/save"' in body
    assert 'href="/about/diagnostics"' not in body  # no browser download, so no Edge flyout

    r = c.post("/about/diagnostics/save")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "content-disposition" not in r.headers
    saved = downloads / NAME
    assert saved.is_file() and stub_build == [saved]
    assert "Saved to" in r.text and str(saved) in r.text
    assert 'data-testid="diagnostics-open-folder"' in r.text and NAME in r.text
    assert 'class="okbox"' in r.text  # the theme's success style, not a fixed color

    r = c.post("/about/diagnostics/reveal", data={"name": NAME})
    assert r.status_code == 200 and "Opened the folder" in r.text
    assert shell == [saved]


def test_reveal_refuses_anything_but_a_file_it_saved(downloads, stub_build, shell):
    c = _client()
    for name in ("../secrets.zip", "notes.txt", NAME):  # NAME is not saved yet
        r = c.post("/about/diagnostics/reveal", data={"name": name})
        # 200 so htmx 2 swaps the refusal in; a 404 left Open folder looking dead.
        assert r.status_code == 200, name
        assert "not one Kestrel saved here" in r.text, name
    assert shell == []


def test_a_downloads_folder_that_cannot_be_written_falls_back_to_kestrels_exports(
    downloads, stub_build, shell, monkeypatch
):
    real = diagnostics.build_bundle

    def _build(settings, *, out_dir, redaction=None):
        if Path(out_dir) == downloads:
            raise PermissionError(13, "Access is denied", str(out_dir))
        return real(settings, out_dir=out_dir, redaction=redaction)

    monkeypatch.setattr(diagnostics, "build_bundle", _build)
    c = _client()
    r = c.post("/about/diagnostics/save")
    fallback = paths.exports_dir() / NAME
    assert fallback.is_file() and str(fallback) in r.text
    c.post("/about/diagnostics/reveal", data={"name": NAME})
    assert shell == [fallback]


def test_a_failed_reveal_says_where_the_file_is(downloads, stub_build):
    def _broken(_p: Path) -> None:
        raise OSError("explorer missing")

    desktop_bridge.set_reveal_handler(_broken)
    try:
        c = _client()
        c.post("/about/diagnostics/save")
        r = c.post("/about/diagnostics/reveal", data={"name": NAME})
        assert "Could not open the folder" in r.text and str(downloads / NAME) in r.text
    finally:
        desktop_bridge.set_reveal_handler(None)


def test_downloads_dir_prefers_the_known_folder_then_home_then_exports(tmp_path, monkeypatch):
    known = tmp_path / "D" / "Downloads"
    known.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(paths, "_windows_downloads", lambda: known)
    assert paths.downloads_dir() == known
    monkeypatch.setattr(paths, "_windows_downloads", lambda: None)
    assert paths.downloads_dir() == paths.exports_dir()
    (home / "Downloads").mkdir()
    assert paths.downloads_dir() == home / "Downloads"
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.downloads_dir() == home / "Downloads"


def test_shell_reveal_uses_explorer_select_on_windows(monkeypatch, tmp_path):
    from kestrel.desktop import shell as shell_mod

    started: list = []
    monkeypatch.setattr(shell_mod, "IS_WINDOWS", True)
    monkeypatch.setattr(shell_mod.subprocess, "Popen", lambda cmd, **k: started.append(cmd))
    target = tmp_path / "My Downloads" / NAME
    shell_mod.reveal_file(target)
    assert started == [f'explorer /select,"{target}"']


def test_a_database_error_during_the_build_is_shown_not_swallowed(downloads, monkeypatch, shell):
    """Review finding: only OSError was caught, so a sqlite or zip error was a 500 and the save
    button did nothing visible."""
    import sqlite3

    def _broken(settings, *, out_dir, redaction=None):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(diagnostics, "build_bundle", _broken)
    r = _client().post("/about/diagnostics/save")
    assert r.status_code == 200
    assert "Could not build the diagnostics file" in r.text and "DatabaseError" in r.text


def test_a_failed_build_leaves_no_partial_zip_and_saves_never_overwrite(tmp_path, monkeypatch):
    """Review finding: a build that failed part way left a corrupt zip in Downloads, and the
    per-minute stamp let a second save overwrite the first."""
    from kestrel.config import get_settings

    settings = get_settings()
    out = tmp_path / "out"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    real_packages = diagnostics.packages_text
    monkeypatch.setattr(diagnostics, "packages_text", _boom)
    with pytest.raises(OSError):
        diagnostics.build_bundle(settings, out_dir=out)
    assert list(out.iterdir()) == []  # neither the zip nor its .part file is left behind

    monkeypatch.setattr(diagnostics, "packages_text", real_packages)
    first = diagnostics.build_bundle(settings, out_dir=out)
    second = diagnostics.build_bundle(settings, out_dir=out)
    assert first != second and first.is_file() and second.is_file()
    assert zipfile.ZipFile(first).testzip() is None
