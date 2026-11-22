"""tests/sql_row_guard.py tells this repository's frames from a library's on Windows too
(D-TEST-585): frame filenames there use backslashes, and the drive letter's case can differ
from the resolved root's."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest
import sql_row_guard as guard


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(guard, "Path", PureWindowsPath)
    monkeypatch.setattr(guard, "ROOT", PureWindowsPath(r"C:\work\kestrel"))


@pytest.mark.parametrize("filename", [
    r"C:\work\kestrel\kestrel\services\install_id.py",
    r"C:\work\kestrel\tests\test_install_id_contract.py",
    r"c:\work\kestrel\kestrel\web\app.py",
])
def test_a_repository_frame_on_windows_is_ours(windows, filename):
    assert guard._ours(filename) is True


@pytest.mark.parametrize("filename", [
    r"C:\work\kestrel\.venv\Lib\site-packages\sqlalchemy\engine\base.py",
    r"C:\Python312\Lib\sqlite3\__init__.py",
    r"C:\work\kestrel-other\kestrel\paths.py",
    r"C:\work\kestrel",
    "<frozen importlib._bootstrap>",
])
def test_a_library_or_outside_frame_on_windows_is_not_ours(windows, filename):
    assert guard._ours(filename) is False


def test_posix_frames_are_judged_as_before(monkeypatch):
    monkeypatch.setattr(guard, "Path", PurePosixPath)
    monkeypatch.setattr(guard, "ROOT", PurePosixPath("/work/kestrel"))
    assert guard._ours("/work/kestrel/kestrel/paths.py") is True
    assert guard._ours("/work/kestrel/.venv/lib/python3.12/site-packages/x.py") is False
    assert guard._ours("/work/kestrel-other/kestrel/paths.py") is False
    assert guard._ours("/usr/lib/python3.12/sqlite3/__init__.py") is False


def test_the_guard_module_itself_is_never_ours():
    assert guard._ours(guard.__file__) is False


def test_the_guard_captures_stacks_without_reading_source_lines(monkeypatch):
    """The guard runs on every statement; reading source lines stat-checks every file on the stack
    and made the install ID contract test hang past its timeout on Windows."""
    import linecache

    def no_reads(*a, **kw):
        raise AssertionError("the guard read source lines")

    monkeypatch.setattr(linecache, "checkcache", no_reads)
    monkeypatch.setattr(linecache, "getline", no_reads)
    monkeypatch.setattr(linecache, "lazycache", no_reads)
    stack = guard._stack()
    assert stack[-1].name == "test_the_guard_captures_stacks_without_reading_source_lines"
    assert all(frame.lineno for frame in stack)
