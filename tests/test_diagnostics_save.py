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

    def _build(settings, *, out_dir, redaction=None, **_):
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
    assert 'class="ab-saved"' in r.text  # a theme-token style, not a fixed color

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

    def _build(settings, *, out_dir, redaction=None, **_):
        if Path(out_dir) == downloads:
            raise PermissionError(13, "Access is denied", str(out_dir))
        return real(settings, out_dir=out_dir, redaction=redaction, **_)

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


class _Fn:
    """One persistent stand-in for a DLL function: ctypes code sets argtypes and restype on it,
    and each call is recorded and answered from `results`."""

    def __init__(self, name: str, calls: list, results: dict):
        self.name, self._calls, self._results = name, calls, results
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self._calls.append((self.name, args))
        r = self._results.get(self.name, 0)
        if isinstance(r, BaseException):
            raise r
        return r


class _Dll:
    def __init__(self, calls: list, results: dict):
        self._calls, self._results, self.fns = calls, results, {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self.fns.setdefault(name, _Fn(name, self._calls, self._results))


def _windows_shell(monkeypatch, shell_mod, calls, *, shell=None, ole=None, startfile=None):
    """Windows as the shell module sees it: private WinDLL instances, os.startfile, all stubbed.
    Returns (opened folders, the two DLL stand-ins)."""
    import ctypes

    monkeypatch.setattr(shell_mod, "IS_WINDOWS", True)
    monkeypatch.setattr(shell_mod, "_win_dlls", None)
    dlls = {
        "shell32": _Dll(calls, {"ILCreateFromPathW": 1234, "SHOpenFolderAndSelectItems": 0,
                                **(shell or {})}),
        "ole32": _Dll(calls, {"CoInitializeEx": 0, **(ole or {})}),
    }
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, *a, **k: dlls[name], raising=False)
    opened: list = []
    monkeypatch.setattr(shell_mod.os, "startfile",
                        startfile or (lambda p: opened.append(p)), raising=False)
    return opened, dlls


def test_shell_reveal_selects_the_file_through_the_shell_api_on_windows(monkeypatch, tmp_path):
    import ctypes

    from structlog.testing import capture_logs

    from kestrel.desktop import shell as shell_mod

    calls: list = []
    opened, dlls = _windows_shell(monkeypatch, shell_mod, calls)
    monkeypatch.setattr(shell_mod.subprocess, "Popen", lambda *a, **k: pytest.fail("explorer"))
    target = tmp_path / "My Downloads" / NAME
    with capture_logs() as logs:
        assert shell_mod.reveal_file(target) is True
    assert logs == [] and opened == []  # success: nothing logged, no folder fallback
    names = [n for n, _ in calls]
    assert names == ["CoInitializeEx", "ILCreateFromPathW", "SHOpenFolderAndSelectItems",
                     "ILFree", "CoUninitialize"]
    assert calls[1][1] == (str(target),) and calls[3][1] == (1234,)
    make = dlls["shell32"].fns["ILCreateFromPathW"]
    assert make.argtypes == [ctypes.c_wchar_p] and make.restype is ctypes.c_void_p
    # The DLL objects are made once and cached, and are the code's own, not the shared windll.
    shell_mod.reveal_file(target)
    assert shell_mod._win_dlls == (dlls["shell32"], dlls["ole32"])


def test_shell_reveal_falls_back_to_the_folder_and_reports_what_happened(monkeypatch, tmp_path):
    from structlog.testing import capture_logs

    from kestrel.desktop import shell as shell_mod

    target = tmp_path / "Downloads" / NAME
    calls: list = []
    opened, _ = _windows_shell(monkeypatch, shell_mod, calls,
                               shell={"SHOpenFolderAndSelectItems": -2147467259})  # E_FAIL
    with capture_logs() as logs:
        assert shell_mod.reveal_file(target) is True
    assert opened == [str(target.parent)]
    assert any(e["event"] == "reveal_file_select_failed" for e in logs)
    assert "ILFree" in [n for n, _ in calls]
    assert str(target) not in repr(logs)

    # NULL item id for the path: fall back to the folder, and free nothing.
    calls.clear()
    opened, _ = _windows_shell(monkeypatch, shell_mod, calls, shell={"ILCreateFromPathW": 0})
    assert shell_mod.reveal_file(target) is True and opened == [str(target.parent)]
    assert "ILFree" not in [n for n, _ in calls]

    # Every way fails: the answer is False, so the page shows where the file is.
    def _fail(_p):
        raise OSError(2, "gone", "C:\\secret\\path")

    calls.clear()
    _windows_shell(monkeypatch, shell_mod, calls, shell={"SHOpenFolderAndSelectItems": -1},
                   startfile=_fail)
    with capture_logs() as logs:
        assert shell_mod.reveal_file(target) is False
    assert any(e["event"] == "reveal_file_failed" and e["errno"] == 2 for e in logs)


@pytest.mark.parametrize(("init", "paired"), [(0, True), (1, True), (-2147417850, False)])
def test_shell_reveal_pairs_couninitialize_with_a_successful_coinitialize(
        monkeypatch, tmp_path, init, paired):
    from kestrel.desktop import shell as shell_mod

    calls: list = []
    _windows_shell(monkeypatch, shell_mod, calls, ole={"CoInitializeEx": init})
    assert shell_mod.reveal_file(tmp_path / NAME) is True
    assert [n for n, _ in calls].count("CoUninitialize") == (1 if paired else 0)


def test_failure_logs_carry_the_error_type_and_code_never_text_or_a_path(monkeypatch, tmp_path):
    from structlog.testing import capture_logs

    from kestrel.desktop import shell as shell_mod

    def _boom(*_a, **_k):
        raise OSError(2, "gone", "C:\\secret\\path")

    with capture_logs() as logs:
        # Windows: the select call and the folder fallback both raise with a path in the error.
        _windows_shell(monkeypatch, shell_mod, [],
                       shell={"ILCreateFromPathW": OSError(2, "gone", "C:\\secret\\path")},
                       startfile=_boom)
        assert shell_mod.reveal_file(tmp_path / NAME) is False
        # Linux and macOS: the opener cannot start.
        monkeypatch.setattr(shell_mod, "IS_WINDOWS", False)
        monkeypatch.setattr(shell_mod.subprocess, "run", _boom)
        monkeypatch.setattr(shell_mod.sys, "platform", "linux")
        assert shell_mod._open_folder(tmp_path) is False
        monkeypatch.setattr(shell_mod.sys, "platform", "darwin")
        assert shell_mod.reveal_file(tmp_path / NAME) is False
        # The bridge: a handler that raises.
        desktop_bridge.set_reveal_handler(_boom)
        desktop_bridge.set_folder_handler(_boom)
        try:
            assert desktop_bridge.request_reveal(tmp_path / NAME) is False
            assert desktop_bridge.request_open_folder(tmp_path) is False
        finally:
            desktop_bridge.set_reveal_handler(None)
            desktop_bridge.set_folder_handler(None)
    assert len(logs) >= 5
    text = repr(logs).lower()
    assert "secret" not in text and "gone" not in text and str(tmp_path).lower() not in text


def test_linux_open_folder_checks_xdg_open_like_macos(monkeypatch, tmp_path):
    import subprocess
    import types

    from structlog.testing import capture_logs

    from kestrel.desktop import shell as shell_mod

    monkeypatch.setattr(shell_mod, "IS_WINDOWS", False)
    monkeypatch.setattr(shell_mod.sys, "platform", "linux")
    seen: list = []
    code = {"rc": 0}

    def _run(argv, **kw):
        seen.append((argv, kw.get("timeout")))
        return types.SimpleNamespace(returncode=code["rc"])

    monkeypatch.setattr(shell_mod.subprocess, "run", _run)
    monkeypatch.setattr(shell_mod.subprocess, "Popen", lambda *a, **k: pytest.fail("unchecked"))
    assert shell_mod._open_folder(tmp_path) is True
    assert seen == [(["xdg-open", str(tmp_path)], 10)]
    assert shell_mod.reveal_file(tmp_path / NAME) is True  # opens the folder
    code["rc"] = 3
    with capture_logs() as logs:
        assert shell_mod._open_folder(tmp_path) is False
    assert logs and logs[0]["exit_code"] == 3 and str(tmp_path) not in repr(logs)

    def _slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(shell_mod.subprocess, "run", _slow)
    with capture_logs() as slow_logs:
        # The launcher is still running (a foreground file manager): the folder is open.
        assert shell_mod._open_folder(tmp_path) is True
    assert slow_logs and slow_logs[0]["log_level"] == "info"
    assert str(tmp_path) not in repr(slow_logs)

    def _missing(argv, **kw):
        raise FileNotFoundError(2, "No such file", "xdg-open")

    monkeypatch.setattr(shell_mod.subprocess, "run", _missing)
    assert shell_mod._open_folder(tmp_path) is False


def test_macos_reveal_and_open_report_a_failed_command(monkeypatch, tmp_path):
    import subprocess
    import types

    from kestrel.desktop import shell as shell_mod

    monkeypatch.setattr(shell_mod, "IS_WINDOWS", False)
    monkeypatch.setattr(shell_mod.sys, "platform", "darwin")
    code = {"rc": 0}
    seen: list = []

    def _run(argv, **kw):
        seen.append(argv)
        return types.SimpleNamespace(returncode=code["rc"])

    monkeypatch.setattr(shell_mod.subprocess, "run", _run)
    assert shell_mod.reveal_file(tmp_path / NAME) is True
    assert seen[-1][:2] == ["open", "-R"]
    code["rc"] = 1
    assert shell_mod.reveal_file(tmp_path / NAME) is False
    assert shell_mod._open_folder(tmp_path) is False

    def _boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(shell_mod.subprocess, "run", _boom)
    assert shell_mod.reveal_file(tmp_path / NAME) is False


def test_a_handler_that_reports_failure_makes_the_page_say_where_the_file_is(downloads,
                                                                              stub_build):
    desktop_bridge.set_reveal_handler(lambda _p: False)
    try:
        c = _client()
        c.post("/about/diagnostics/save")
        r = c.post("/about/diagnostics/reveal", data={"name": NAME})
        assert "Opened the folder" not in r.text
        assert "Could not open the folder" in r.text and str(downloads / NAME) in r.text
        desktop_bridge.set_reveal_handler(lambda _p: True)
        assert "Opened the folder." in c.post("/about/diagnostics/reveal",
                                              data={"name": NAME}).text
    finally:
        desktop_bridge.set_reveal_handler(None)


def test_a_database_error_during_the_build_is_shown_not_swallowed(downloads, monkeypatch, shell):
    """Review finding: only OSError was caught, so a sqlite or zip error was a 500 and the save
    button did nothing visible."""
    import sqlite3

    def _broken(settings, *, out_dir, redaction=None, **_):
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


def test_tray_open_folder_runs_off_the_tray_thread(monkeypatch, tmp_path):
    import threading

    from kestrel.desktop import shell as shell_mod

    done = threading.Event()
    seen: dict = {}
    release = threading.Event()

    def _slow_open(p):
        seen["thread"], seen["path"] = threading.current_thread(), p
        release.wait(5)
        done.set()
        return True

    monkeypatch.setattr(shell_mod, "_open_folder", _slow_open)
    shell_mod._open_folder_async(tmp_path)  # returns at once although the opener is blocked
    assert not done.is_set()
    release.set()
    assert done.wait(5)
    assert seen["thread"] is not threading.current_thread() and seen["thread"].daemon


def _info(i: int) -> str:
    return f'2026-11-01 10:00:00,000 INFO MainThread {{"event": "tick {i}"}}'






def test_a_ctypes_argument_error_falls_back_to_the_folder(monkeypatch, tmp_path):
    import ctypes

    from kestrel.desktop import shell as shell_mod

    calls: list = []
    opened, _ = _windows_shell(
        monkeypatch, shell_mod, calls,
        shell={"ILCreateFromPathW": ctypes.ArgumentError("argument 1: bad type")})
    assert shell_mod.reveal_file(tmp_path / NAME) is True
    assert opened == [str(tmp_path)]
