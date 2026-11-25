"""Stop a gate stage at its first failure, under xdist too (D-CI-611).

Loaded by scripts/check.py with `-p tests.failfast_plugin --kestrel-failfast-file <path>`. The
path names a file that does not exist yet, inside a fresh empty folder the gate makes for that
one stage. Without the option the plugin registers nothing.

- A failed report (setup, call or teardown, and the report xdist makes for a crashed worker)
  creates that file with O_CREAT | O_EXCL and writes the test's node ID: the first writer wins,
  so it holds the first recorded failure.
- Before each test, if the file exists, the run is told to stop (`session.shouldstop`) and the
  test is not run. xdist's worker loop stops after the current test when `shouldstop` is set, so
  every worker stops, which `-x` does not do under `--dist worksteal` (D-CI-611).

It never turns a failure into a pass: a test is held back only after a real failure wrote the
file, and the gate passes a stage only when pytest exits 0 and the file does not exist.

The path is a pytest option and never an environment variable: xdist hands options to its
workers, and a pytest that a test starts in a child process does not inherit them. The state
lives on one object registered with this run's own plugin manager, so a pytest run inside a
test (pytest.main in the same process) keeps its own and cannot replace this one.
"""

from __future__ import annotations

import os

import pytest

OPTION = "--kestrel-failfast-file"
STOP_REASON = "fail-fast: another test failed first (D-CI-611)"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(OPTION, default="", metavar="PATH",
                     help="first-failure file for one gate stage (scripts/check.py only)")


def _record(path: str, nodeid: str) -> None:
    try:
        # O_BINARY (Windows only): the ID is written byte for byte, never as CRT text
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except FileExistsError:
        return  # an earlier failure is already recorded
    try:
        os.write(fd, nodeid.encode("utf-8", "replace"))
    finally:
        os.close(fd)


class FailFast:
    """The stage's first-failure file, for one pytest run."""

    def __init__(self, path: str) -> None:
        self.path = path

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            _record(self.path, report.nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item,
                                nextitem: pytest.Item | None) -> bool | None:
        if os.path.exists(self.path):
            item.session.shouldstop = STOP_REASON
            return True  # handled: this test is not run
        return None


def pytest_configure(config: pytest.Config) -> None:
    path = str(config.getoption("kestrel_failfast_file") or "")
    if path:
        config.pluginmanager.register(FailFast(path), "kestrel-failfast")
