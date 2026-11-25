"""Faster, never weaker (D-CI-610): the fail-fast gate runs every test the old gate ran.

The whole-suite stage (S7) must be the old suite command plus exactly three items that select
nothing: `-p tests.failfast_plugin`, `--kestrel-failfast-file <path>` and `--cov-append`. This
file proves it twice: structurally (the command with those items removed equals the old command,
frozen below from scripts/check.py before the change) and by a real `pytest --collect-only` of
both command lines, compared as sets of test IDs. The stages that run first (S4 to S6) must
collect only IDs the old command collects.

Collection does not prove execution (a midnight test is collected but runs its body only with
KESTREL_BOUNDARY=1). So the junit comparison below, `outcomes`, is what the acceptance run uses
on a green gate: the (test id, outcome) pairs of the old suite command and of S7 must be equal.
Its own test shows it catches one test that passed before and is skipped now.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The gate stand-ins below replace subprocess.run (as tests/test_check_runner.py does); the real
# collection keeps its own handle on Popen.
REAL_POPEN = subprocess.Popen
OPTION = "--kestrel-failfast-file"

# The suite command of scripts/check.py's full gate before the fail-fast change (_gate, before
# D-CI-610), after the interpreter, with eight workers. Do not edit this to match a new
# command: a change here is a change to what the gate runs and needs its own decision.
OLD_SUITE = ["-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-n", "8",
             "--dist", "worksteal", "--max-worker-restart", "0", "--cov=kestrel", "--cov-report="]
# Options that would drop or reorder-and-drop tests. None may appear in a stage command.
NARROWING = ("--deselect", "-k", "-m", "--ignore", "--ignore-glob", "--lf", "--last-failed",
             "--ff", "--failed-first", "-x", "--exitfirst", "--maxfail", "--sw", "--stepwise",
             "--co", "--collect-only")


def _load_check(monkeypatch, tmp_path):
    monkeypatch.delenv("KESTREL_KEEP_GOING", raising=False)
    spec = importlib.util.spec_from_file_location("check_invariant", ROOT / "scripts" / "check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    for name in ("_coverage_step", "_red_first_gate", "_browser_evidence_gate", "_export_lint",
                 "_stress_gate", "_run"):
        monkeypatch.setattr(check, name, lambda *args: True)
    monkeypatch.setattr(check, "_static_gates", lambda: True)
    monkeypatch.setattr(check, "_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(check, "FAILFAST_RECORD", tmp_path / "failfast-last.json")
    monkeypatch.setattr(check.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout="", returncode=0))
    return check


def _stage_commands(monkeypatch, tmp_path, *, plan, changed, argv=("--workers", "8")):
    """Run the full gate with stand-in pytest runs; return every pytest command, in order."""
    check = _load_check(monkeypatch, tmp_path)
    monkeypatch.setattr(check, "_plan_diff_guards", lambda: plan)
    monkeypatch.setattr(check, "_changed_selection", lambda: changed)
    monkeypatch.setattr(check.sys, "argv", ["check.py", *argv])
    commands: list[list[str]] = []

    def run(cmd, extra_env=None):
        commands.append(cmd)
        check.LAST_RUN["exit"], check.LAST_RUN["passed"] = 0, 1
        return True, []

    monkeypatch.setattr(check, "_run_pytest", run)
    assert check.main() == 0
    return commands


def without_failfast_items(cmd: list[str]) -> list[str]:
    """The command minus the three items fail-fast adds (each removed once, where it is)."""
    out = list(cmd)
    at = out.index("tests.failfast_plugin")
    assert out[at - 1] == "-p"
    del out[at - 1:at + 1]
    at = out.index(OPTION)
    del out[at:at + 2]
    out.remove("--cov-append")
    return out


def _suite(commands: list[list[str]]) -> list[str]:
    suites = [c for c in commands if "--cov=kestrel" in c and not c[-1].endswith(".py")]
    assert len(suites) == 1, suites
    return suites[0]


def test_the_suite_stage_is_the_old_command_plus_three_items_that_select_nothing(
        monkeypatch, tmp_path):
    commands = _stage_commands(monkeypatch, tmp_path, plan=([], {}, []), changed=([], []))
    suite = _suite(commands)
    assert suite[0] == sys.executable
    assert without_failfast_items(suite)[1:] == OLD_SUITE
    assert not set(suite[3:]) & set(NARROWING)  # after `python -m pytest`
    assert not any(arg.startswith(("tests/", "tests\\")) for arg in suite)


def test_the_suite_stage_is_unchanged_whatever_ran_before_it(monkeypatch, tmp_path):
    """S4 to S6 add commands; they never change S7's."""
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _stage_commands(monkeypatch, tmp_path, plan=plan,
                               changed=(["tests/test_phone.py"], ["tests/test_x_browser.py"]))
    assert len(commands) == 3
    assert without_failfast_items(_suite(commands))[1:] == OLD_SUITE
    for cmd in commands:
        assert not set(cmd[3:]) & set(NARROWING), cmd


def test_keep_going_gives_the_old_suite_command_exactly(monkeypatch, tmp_path):
    commands = _stage_commands(monkeypatch, tmp_path, plan=([], {}, []), changed=([], []),
                               argv=("--workers", "8", "--keep-going"))
    assert commands == [[sys.executable, *OLD_SUITE]]


# ---- the real collection ---------------------------------------------------------------------

def _collect_args(args: list[str], first: Path) -> list[str]:
    """A stage's arguments for `pytest --collect-only`: serial and without coverage on both
    sides alike; the plugin and its option stay, with a fresh file path."""
    keep: list[str] = []
    skip = 0
    for arg in args:
        if skip:
            skip -= 1
        elif arg in ("-n", "--dist", "--max-worker-restart"):
            skip = 1
        elif arg == OPTION:
            keep += [arg, str(first)]
            skip = 1
        elif not (arg.startswith("--cov") or arg == "-rs"):
            keep.append(arg)
    return [sys.executable, *keep[1:], "--collect-only", "-p", "no:xdist"]


def _collect(jobs: list[tuple[list[str], dict[str, str]]], tmp_path: Path) -> list[set[str]]:
    """Collect every (stage arguments, extra environment) at once, in parallel; return each as a
    set of node IDs. A collection error fails the test, never shrinks a side."""
    procs = []
    for n, (args, extra_env) in enumerate(jobs):
        env = {**os.environ, "KESTREL_HEADLESS": "1", "KESTREL_BOUNDARY": "1", **extra_env}
        env.pop("PYTEST_XDIST_WORKER", None)
        procs.append(REAL_POPEN(
            _collect_args(args, tmp_path / f"collect{n}-first-failure"), cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        ))
    results = []
    for proc in procs:
        out, _ = proc.communicate(timeout=600)
        assert proc.returncode == 0, out[-3000:]
        ids = {line.strip() for line in out.splitlines() if "::" in line}
        assert ids, out[-2000:]
        results.append(ids)
    return results


def test_the_suite_stage_collects_exactly_the_old_test_ids(monkeypatch, tmp_path):
    commands = _stage_commands(monkeypatch, tmp_path, plan=([], {}, []), changed=([], []))
    new, old = _collect([(_suite(commands), {}), ([sys.executable, *OLD_SUITE], {})], tmp_path)
    assert len(new) == len(old)
    assert new == old, (sorted(old - new)[:20], sorted(new - old)[:20])
    assert len(old) > 1000  # the whole suite, not an empty or broken collection


def test_the_early_stages_collect_only_ids_the_old_command_collects(monkeypatch, tmp_path):
    plan = (["tests/test_review_backend_browser.py"], {}, [])
    commands = _stage_commands(monkeypatch, tmp_path, plan=plan,
                               changed=(["tests/test_phone.py"], []))
    early = [c for c in commands if c is not _suite(commands)]
    assert len(early) == 2  # S4 and S6
    # The old command restricted to the same two files; the test above proves the old and new
    # suite stages collect the same whole set, which holds these.
    old, *stages = _collect(
        [([sys.executable, *OLD_SUITE, "tests/test_phone.py",
           "tests/test_review_backend_browser.py"], {}),
         *[(cmd, {"KESTREL_LIVE_BROWSER": "1"}) for cmd in early]], tmp_path)
    union = set().union(*stages)
    assert all(stages)
    assert union <= old, sorted(union - old)[:20]


# ---- executed outcomes (the acceptance comparison) ----------------------------------------------

def outcomes(junit_xml: str) -> set[tuple[str, str]]:
    """(test id, outcome) pairs of a junit report: passed, failed, error or skipped."""
    pairs: set[tuple[str, str]] = set()
    for case in ET.fromstring(junit_xml).iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        pairs.add((test_id, outcome))
    return pairs


def _junit(*cases: str) -> str:
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


@pytest.mark.parametrize("new_second,same", [
    ('<testcase classname="tests.test_a" name="t2"/>', True),
    ('<testcase classname="tests.test_a" name="t2"><skipped message="x"/></testcase>', False),
    ('<testcase classname="tests.test_a" name="t2"><failure message="x"/></testcase>', False),
    ("", False),
])
def test_the_outcome_comparison_catches_a_test_that_no_longer_passes(new_second, same):
    first = '<testcase classname="tests.test_a" name="t1"/>'
    old = outcomes(_junit(first, '<testcase classname="tests.test_a" name="t2"/>'))
    new = outcomes(_junit(first, new_second))
    assert (old == new) is same
    assert ("tests.test_a::t1", "passed") in new
