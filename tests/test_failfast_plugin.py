"""The fail-fast plugin of the gate stages (tests/failfast_plugin.py, D-CI-611).

Each test runs a small invented suite in a child pytest, in its own folder, so nothing here
depends on this repository's own tests. Every test body creates a file named after itself in a
log folder (one file each: appends from four processes to one file can be lost on Windows), so
the tests count the bodies that really ran, not only pytest's exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASSING = '''
import os, pytest

@pytest.mark.parametrize("n", range(40))
def test_one(n):
    open(os.path.join(os.environ["TOY_LOG"], str(n)), "x").close()
'''

FAILING = '''
import os, time, pytest

@pytest.mark.parametrize("n", range(40))
def test_one(n):
    open(os.path.join(os.environ["TOY_LOG"], str(n)), "x").close()
    if os.path.exists(os.environ["TOY_FILE"]):
        open(os.path.join(os.environ["TOY_LOG"], f"late-{n}"), "x").close()
    if n == 2:
        assert n == 0, "the one invented failure"
    time.sleep(0.2)
'''

NESTED = '''
import os, subprocess, sys

def test_a_child_pytest_does_not_see_the_parent_file(tmp_path):
    child = tmp_path / "test_child.py"
    child.write_text("def test_child():\\n    assert 1 == 2\\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
    seen = [k for k, v in os.environ.items() if k != "TOY_FILE" and os.environ["TOY_FILE"] in v]
    open(os.path.join(os.environ["TOY_LOG"], "env-" + "-".join(seen)), "x").close()
    child_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.failfast_plugin", "-c", str(tmp_path / "pytest.ini"), str(child)],
        cwd=tmp_path, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert child_run.returncode == 1, child_run.stdout  # the child ran and its test failed
    assert not os.path.exists(os.environ["TOY_FILE"]), "the child wrote the parent's file"
'''

IN_PROCESS = '''
import os, pytest

def test_0_runs_a_pytest_inside_this_process(tmp_path):
    open(os.path.join(os.environ["TOY_LOG"], "0"), "x").close()
    inner = tmp_path / "test_inner.py"
    inner.write_text("def test_inner():\\n    pass\\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
    code = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "tests.failfast_plugin",
                        "-c", str(tmp_path / "pytest.ini"), str(inner)])
    assert code == 0

def test_1_fails():
    open(os.path.join(os.environ["TOY_LOG"], "1"), "x").close()
    assert 1 == 2

def test_2_must_not_run():
    open(os.path.join(os.environ["TOY_LOG"], "2"), "x").close()
'''


def _toy_folder(tmp_path: Path, source: str) -> Path:
    """The invented suite with a pytest.ini of its own. The ini pins the toy's root: with
    `-c os.devnull` pytest lists every folder up to the drive root, so a folder another process
    deletes from the temp folder meanwhile failed the toy's collection (seen while browser tests
    ran beside these)."""
    (tmp_path / "toy").mkdir()
    (tmp_path / "toy" / "test_toy.py").write_text(source, encoding="utf-8")
    ini = tmp_path / "toy" / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    return ini


def _toy(tmp_path: Path, source: str, *extra: str) -> tuple[int, list[str], Path, str]:
    """Run the invented suite with the plugin; return (exit code, bodies that ran, the
    first-failure file, output)."""
    ini = _toy_folder(tmp_path, source)
    log = tmp_path / "bodies"
    log.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    first = stage / "first-failure"
    env = {**os.environ, "TOY_LOG": str(log), "TOY_FILE": str(first),
           "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ini),
         "-p", "tests.failfast_plugin", "--kestrel-failfast-file", str(first), *extra,
         str(tmp_path / "toy")],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=300,
    )
    bodies = sorted((p.name for p in log.iterdir()), key=lambda n: (len(n), n))
    return proc.returncode, bodies, first, proc.stdout + proc.stderr


def test_a_clean_run_runs_every_test_and_writes_no_failure_file(tmp_path):
    """G2: only a failed report writes the file, so a clean run is never cut short."""
    code, bodies, first, out = _toy(tmp_path, PASSING, "-n", "4", "--dist", "worksteal",
                                    "--max-worker-restart", "0")
    assert code == 0, out
    assert bodies == [str(n) for n in range(40)]
    assert not first.exists()


def test_after_the_first_failure_no_further_test_body_runs(tmp_path):
    """G3: serially, the failing test is the third, so exactly three bodies ran; the run does
    not exit 0 and the file names the failing test."""
    code, bodies, first, out = _toy(tmp_path, FAILING, "-p", "no:xdist")
    assert code != 0, out
    assert bodies == ["0", "1", "2"]  # serially no body starts after the file exists
    assert first.read_text(encoding="utf-8") == "test_toy.py::test_one[2]"


def test_under_worksteal_every_worker_stops_after_the_first_failure(tmp_path):
    """G3 under the gate's own scheduler: -x alone let 32 of 39 run on (D-CI-611); with the
    plugin the workers stop after their current test, so the run is cut far short of 40."""
    code, bodies, first, out = _toy(tmp_path, FAILING, "-n", "4", "--dist", "worksteal",
                                    "--max-worker-restart", "0")
    assert code != 0, out
    assert "2" in bodies
    started = [b for b in bodies if not b.startswith("late-")]
    late = [b for b in bodies if b.startswith("late-")]
    assert len(started) <= 30, f"{len(started)} of 40 bodies ran after a failure"
    # A body may start after the file exists only in the instant between a worker's check and
    # its test: at most one per worker. A worker that ignored the file would start many.
    assert len(late) <= 4, late
    assert first.read_text(encoding="utf-8") == "test_toy.py::test_one[2]"


def test_the_file_is_an_option_that_a_child_pytest_never_inherits(tmp_path):
    """G7: the path reaches the xdist worker as a pytest option, never through the environment,
    so a failing pytest that a test starts cannot stop or fail the gate's stage."""
    code, bodies, first, out = _toy(tmp_path, NESTED, "-n", "1", "--max-worker-restart", "0")
    assert code == 0, out
    assert bodies == ["env-"]
    assert not first.exists()


def test_without_the_option_the_plugin_does_nothing(tmp_path):
    ini = _toy_folder(tmp_path, FAILING)
    log = tmp_path / "bodies"
    log.mkdir()
    env = {**os.environ, "TOY_LOG": str(log), "TOY_FILE": str(tmp_path / "no-file-here"),
           "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ini),
         "-p", "tests.failfast_plugin", "-p", "no:xdist",
         str(tmp_path / "toy")],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=300,
    )
    assert proc.returncode == 1, proc.stdout
    assert len(list(log.iterdir())) == 40
    assert "1 failed, 39 passed" in proc.stdout


def test_a_pytest_run_inside_a_test_keeps_its_own_state(tmp_path):
    """Codex review: a module-level config let an in-process pytest.main that loads the plugin
    replace the outer run's settings, so the outer failure wrote no file and later tests ran."""
    code, bodies, first, out = _toy(tmp_path, IN_PROCESS, "-p", "no:xdist")
    assert code != 0, out
    assert bodies == ["0", "1"]
    assert first.read_text(encoding="utf-8") == "test_toy.py::test_1_fails"
