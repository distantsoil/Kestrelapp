"""D-TEST-8: the pytest configuration turns silent defects into failures. Each test runs a tiny
pytest session (in a temp folder, with the repository's own pyproject.toml settings) on a broken
test and checks the session fails with the right words."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _session(tmp_path: Path, body: str, *extra: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_broken.py").write_text(body, encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-n",
        "0",
        "-c",
        str(ROOT / "pyproject.toml"),
        "--rootdir",
        str(tmp_path),
        *extra,
        str(tmp_path / "test_broken.py"),
    ]
    return subprocess.run(
        cmd, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=120
    )


def test_a_coroutine_that_is_never_awaited_fails_the_test(tmp_path):
    proc = _session(
        tmp_path,
        (
            "import gc\n"
            "async def work():\n    return 1\n"
            "def test_leaks_a_coroutine():\n"
            "    work()\n"
            "    gc.collect()\n"
        ),
    )
    assert proc.returncode != 0
    assert "never awaited" in proc.stdout


def test_an_awaited_coroutine_passes(tmp_path):
    proc = _session(
        tmp_path,
        (
            "import asyncio\n"
            "async def work():\n    return 1\n"
            "def test_awaits():\n"
            "    assert asyncio.run(work()) == 1\n"
        ),
    )
    assert proc.returncode == 0, proc.stdout


def test_other_runtime_warnings_are_not_promoted_to_errors(tmp_path):
    proc = _session(
        tmp_path,
        (
            "import warnings\n"
            "def test_other_warning():\n"
            "    warnings.warn('something else', RuntimeWarning)\n"
            "    assert True\n"
        ),
    )
    assert proc.returncode == 0, proc.stdout


def test_a_test_that_hangs_is_stopped_with_a_stack_dump(tmp_path):
    proc = _session(
        tmp_path, ("import time\ndef test_hangs():\n    time.sleep(30)\n"), "-o", "timeout=2"
    )
    assert proc.returncode != 0
    assert "Timeout" in proc.stdout and "test_hangs" in proc.stdout


def test_a_timeout_under_xdist_fails_that_test_and_the_worker_lives_on(tmp_path):
    """A test past its limit must fail on its own. The thread method ends the whole worker process,
    so xdist reports a crashed node and the tests after it on that worker are lost."""
    (tmp_path / "test_slow.py").write_text(
        "import time\n"
        "def test_a_slow():\n    time.sleep(30)\n"
        "def test_b_after():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(tmp_path),
        "-n", "1", "-o", "timeout=2", str(tmp_path / "test_slow.py"),
    ]
    proc = subprocess.run(
        cmd, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=120
    )
    out = proc.stdout + proc.stderr
    assert "1 failed, 1 passed" in out, out[-2000:]
    # Where the signal method exists the worker survives. Windows has only the thread method,
    # which ends the worker; xdist replaces it, so the run still finishes as asserted above.
    worker_ends = not hasattr(signal, "SIGALRM")
    assert ("crashed" in out) is worker_ends, out[-2000:]


def test_the_default_timeout_is_sixty_seconds_and_browser_files_get_longer(tmp_path):
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["timeout"] == 60

    from tests import conftest

    class Item:
        def __init__(self, name: str):
            self.path = Path(name)
            self.markers: list[object] = []

        def get_closest_marker(self, _name):
            return None

        def add_marker(self, marker):
            self.markers.append(marker)

    browser, plain = Item("tests/test_x_browser.py"), Item("tests/test_x.py")
    conftest._timeout_for_browser_files([browser, plain])
    assert [m.args for m in browser.markers] == [(conftest.BROWSER_TEST_TIMEOUT_SECONDS,)]
    assert conftest.BROWSER_TEST_TIMEOUT_SECONDS == 180
    assert plain.markers == []


@pytest.mark.parametrize("name", ["pytest-timeout", "pyright"])
def test_the_new_dev_tools_are_declared(name):
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"{name}"' in text
