"""D-TEST-800 (issue #237): a test run never writes into the real Downloads folder or the real
home. Every test gets its own Downloads folder (tests/conftest.py, _downloads_in_tmp); the
diagnostics save, the one route that writes there, lands in it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.web import desktop_bridge


def _under(child: Path, parent: Path) -> bool:
    return parent.resolve() in child.resolve().parents or child.resolve() == parent.resolve()


def test_downloads_dir_is_this_tests_own_folder(_downloads_in_tmp, tmp_path_factory):
    found = paths.downloads_dir()
    assert found == _downloads_in_tmp
    assert _under(found, tmp_path_factory.getbasetemp())
    # The real Downloads folder is never the target. Not "outside the real home": on Windows the
    # pytest base temp (%LOCALAPPDATA%\\Temp) is inside the home folder (PC run, 80de522f).
    assert not _under(found, Path.home() / "Downloads")


def test_the_windows_known_folder_is_not_asked(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths._windows_downloads() is None


def test_a_diagnostics_save_lands_in_the_test_folder(_downloads_in_tmp):
    # The real folder may already hold zips from the operator's own saves; nothing new may appear.
    real = Path.home() / "Downloads"
    before = set(real.glob("kestrel-diagnostics-*.zip"))
    desktop_bridge.set_reveal_handler(lambda _p: None)
    try:
        from kestrel.web.app import build_app

        r = TestClient(build_app(worker=False)).post("/about/diagnostics/save")
    finally:
        desktop_bridge.set_reveal_handler(None)
    assert r.status_code == 200
    zips = list(_downloads_in_tmp.glob("kestrel-diagnostics-*.zip"))
    assert len(zips) == 1, r.text
    assert set(real.glob("kestrel-diagnostics-*.zip")) == before


@pytest.mark.real_downloads_dir
def test_the_marker_runs_the_real_resolution(tmp_path, monkeypatch, _downloads_in_tmp):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.downloads_dir() == home / "Downloads"
    assert paths.downloads_dir() != _downloads_in_tmp
