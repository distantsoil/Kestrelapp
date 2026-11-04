"""The local gate runs the same tests in parallel and serial modes."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


def _load_check():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check.py"
    spec = importlib.util.spec_from_file_location("check_runner_raw", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.delenv("KESTREL_TEST_WORKERS", raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "check.py"
    spec = importlib.util.spec_from_file_location("check_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # No real git or coverage in these tests: an empty diff and a passing coverage step.
    monkeypatch.setattr(module, "_plan_diff_guards", lambda: ([], {}, []))
    monkeypatch.setattr(module, "_coverage_step", lambda *args: True)
    monkeypatch.setattr(module, "_static_gates", lambda: True)  # D-TEST-8: tested on its own
    monkeypatch.setattr(module, "_red_first_gate", lambda: True)
    monkeypatch.setattr(module, "_browser_evidence_gate", lambda *args: True)
    return module


@pytest.mark.parametrize("cpus,expected", [(None, 1), (1, 1), (2, 1), (8, 4), (32, 8)])
def test_default_workers_use_available_cpus_with_a_limit(runner, monkeypatch, cpus, expected):
    monkeypatch.setattr(runner.os, "cpu_count", lambda: cpus)
    assert runner.worker_count("auto") == expected


@pytest.mark.parametrize("value", ["-1", "many", "1.5"])
def test_invalid_worker_counts_fail_before_running_tests(runner, value):
    with pytest.raises(argparse.ArgumentTypeError):
        runner.worker_count(value)


@pytest.mark.parametrize("workers", ["0", "2"])
def test_serial_and_parallel_run_identical_selection(runner, monkeypatch, workers):
    commands = []
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", workers,
                                           "--junitxml", "outcomes.xml"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, **_kw: (commands.append(cmd) or True, []))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout=""))
    assert runner.main() == 0
    command = commands[0]
    assert command[command.index("-n") + 1] == workers
    assert command[command.index("--dist") + 1] == "worksteal"
    assert command[command.index("--max-worker-restart") + 1] == "0"
    assert command[command.index("--junitxml") + 1] == "outcomes.xml"
    assert not any(value.startswith("tests/") for value in command)


def test_parallel_failures_never_report_ready(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner.sys, "argv", ["check.py"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (False, []))
    assert runner.main() == 1
    assert "Ready to push." not in capsys.readouterr().out


@pytest.mark.parametrize("configured,expected", [(None, "8"), ("3", "3"), ("0", "0")])
def test_worker_default_respects_environment(runner, monkeypatch, configured, expected):
    if configured is not None:
        monkeypatch.setenv("KESTREL_TEST_WORKERS", configured)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(runner.sys, "argv", ["check.py"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    commands = []
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, **_kw: (commands.append(cmd) or False, []))
    assert runner.main() == 1
    assert commands[0][commands[0].index("-n") + 1] == expected


def test_explicit_worker_count_overrides_environment(runner, monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_WORKERS", "3")
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", "2"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    commands = []
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, **_kw: (commands.append(cmd) or False, []))
    assert runner.main() == 1
    assert commands[0][commands[0].index("-n") + 1] == "2"


def test_invalid_environment_worker_count_fails_before_running(runner, monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_WORKERS", "many")
    monkeypatch.setattr(runner.sys, "argv", ["check.py"])
    commands = []
    monkeypatch.setattr(runner, "_run", lambda *args: commands.append(args))
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert not commands


def test_parallel_skip_summary_preserves_counts_and_reasons(runner, capsys):
    runner._report_skips([(2, "tests/test_example.py:1", "platform only"),
                          (1, "tests/test_other.py:2", "platform only")])
    assert "3  platform only" in capsys.readouterr().out


@pytest.mark.parametrize("quick", [False, True])
def test_only_the_full_gate_runs_the_midnight_boundary_run(runner, monkeypatch, quick):
    seen = []
    monkeypatch.setattr(runner.sys, "argv", ["check.py", *(["--quick"] if quick else [])])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, extra_env=None: (seen.append(extra_env) or True, []))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout=""))
    assert runner.main() == 0
    assert (seen[0] or {}).get("KESTREL_BOUNDARY") == (None if quick else "1")


def _full_gate(runner, monkeypatch, plan, pytest_results):
    """Run main() as the full gate with canned pytest runs; returns the pytest commands."""
    commands = []
    results = iter(pytest_results)
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", "0"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_plan_diff_guards", lambda: plan)
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, **kw: (commands.append((cmd, kw)) or next(results)))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout=""))
    return commands


def test_unmapped_ui_file_fails_before_any_test_runs(runner, monkeypatch, capsys):
    commands = _full_gate(runner, monkeypatch, ([], {}, ["kestrel/web/pages/mystery.py"]), [])
    assert runner.main() == 1
    out = capsys.readouterr().out
    assert "kestrel/web/pages/mystery.py" in out and "Ready to push." not in out
    assert commands == []


def test_full_gate_runs_selected_browser_files_live_and_with_coverage(runner, monkeypatch):
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    assert runner.main() == 0
    (main_cmd, main_kw), (browser_cmd, browser_kw) = commands
    assert "--cov=kestrel" in main_cmd and "tests/test_nav_browser.py" not in main_cmd
    assert browser_cmd[-1] == "tests/test_nav_browser.py" and "--cov-append" in browser_cmd
    assert browser_kw["extra_env"]["KESTREL_LIVE_BROWSER"] == "1"


def test_the_browser_stage_runs_one_worker_by_default(runner, monkeypatch):
    # Two live browsers against servers under coverage timed out on a 4-CPU machine (two
    # browser tests failed in the gate and passed alone and in order): one worker by default.
    monkeypatch.delenv("KESTREL_BROWSER_WORKERS", raising=False)
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    assert runner.main() == 0
    (_main_cmd, _), (browser_cmd, _) = commands
    assert browser_cmd[browser_cmd.index("-n") + 1] == "1"


def test_browser_files_with_no_passed_test_do_not_pass(runner, monkeypatch, capsys):
    plan = (["tests/test_nav_browser.py"], {}, [])
    _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    monkeypatch.setattr(runner, "_browser_evidence_gate", lambda *args: False)
    assert runner.main() == 1
    assert "Ready to push." not in capsys.readouterr().out


def _junit(tmp_path, body):
    path = tmp_path / "j.xml"
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    return path


def test_evidence_gate_names_the_all_skipped_file_and_reasons(tmp_path, capsys):
    path = _junit(
        tmp_path,
        '<testcase classname="tests.test_nav_browser" name="a">'
        '<skipped message="no Edge here"/></testcase>',
    )
    check = _load_check()
    assert check._browser_evidence_gate(path, ["tests/test_nav_browser.py"]) is False
    out = capsys.readouterr().out
    assert "tests/test_nav_browser.py" in out and "no Edge here" in out


def test_evidence_gate_passes_with_one_passed_test(tmp_path):
    path = _junit(
        tmp_path,
        '<testcase classname="tests.test_nav_browser" name="a"><skipped message="x"/></testcase>'
        '<testcase classname="tests.test_nav_browser" name="b"/>',
    )
    assert _load_check()._browser_evidence_gate(path, ["tests/test_nav_browser.py"]) is True


def test_evidence_gate_fails_on_missing_or_broken_report(tmp_path, capsys):
    check = _load_check()
    assert check._browser_evidence_gate(tmp_path / "none.xml", ["tests/x_browser.py"]) is False
    bad = tmp_path / "bad.xml"
    bad.write_text("<not xml", encoding="utf-8")
    assert check._browser_evidence_gate(bad, ["tests/x_browser.py"]) is False
    assert "Ready to push." not in capsys.readouterr().out


def test_diff_coverage_with_nothing_to_measure_says_so(runner, tmp_path, capsys):
    cov = tmp_path / "c.json"
    cov.write_text('{"files": {}}', encoding="utf-8")
    assert runner._diff_coverage_gate(cov, {"README.md": {1}}) is True
    assert (
        "Diff coverage: no Python lines changed under kestrel/ (nothing to measure)"
        in capsys.readouterr().out
    )


def test_low_diff_coverage_never_reports_ready(runner, monkeypatch, capsys):
    plan = ([], {"kestrel/a.py": {1}}, [])
    _full_gate(runner, monkeypatch, plan, [(True, [])])
    monkeypatch.setattr(runner, "_coverage_step", lambda *args: False)
    assert runner.main() == 1
    assert "Ready to push." not in capsys.readouterr().out


def test_quick_runs_no_coverage_and_no_browser_tests(runner, monkeypatch, capsys):
    plan = (["tests/test_nav_browser.py"], {}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, [])])
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--quick"])
    assert runner.main() == 0
    assert len(commands) == 1 and "--cov=kestrel" not in commands[0][0]
    assert "tests/test_nav_browser.py" in capsys.readouterr().out


def test_fast_runs_the_whole_suite_without_coverage_or_browser_and_ends_with_its_line(
    runner, monkeypatch, capsys
):
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, [])])
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--fast"])
    assert runner.main() == 0
    out = capsys.readouterr().out
    assert len(commands) == 1
    assert not any(a.startswith("--cov") for a in commands[0][0])
    assert not any(a.startswith("tests/") for a in commands[0][0])  # the whole suite
    assert out.strip().splitlines()[-1] == "Fast checks passed; run the full check before pushing."
    assert "Ready to push." not in out


def test_quick_ends_with_its_own_line(runner, monkeypatch, capsys):
    _full_gate(runner, monkeypatch, ([], {}, []), [(True, [])])
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--quick"])
    assert runner.main() == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == "Quick checks passed; run the full check before pushing."


def test_quick_and_fast_together_are_refused(runner, monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--quick", "--fast"])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


@pytest.mark.parametrize("flag", [[], ["--quick"], ["--fast"]])
def test_a_failing_static_check_stops_every_tier_before_the_suite(runner, monkeypatch, flag):
    commands = _full_gate(runner, monkeypatch, ([], {}, []), [(True, [])])
    monkeypatch.setattr(runner, "_static_gates", lambda: False)
    monkeypatch.setattr(runner.sys, "argv", ["check.py", *flag])
    assert runner.main() == 1
    assert commands == []


def test_a_failing_red_first_proof_blocks_ready_in_the_full_gate(runner, monkeypatch, capsys):
    _full_gate(runner, monkeypatch, ([], {}, []), [(True, [])])
    monkeypatch.setattr(runner, "_red_first_gate", lambda: False)
    assert runner.main() == 1
    assert "Ready to push." not in capsys.readouterr().out


def test_the_static_gate_prints_every_problem_with_its_rule(runner, monkeypatch, capsys):
    monkeypatch.undo()  # the fixture's stubs would hide the real gate
    runner = _load_check()
    monkeypatch.setattr(runner.diff_gate, "diff_base", lambda: ("abc", "note"))
    monkeypatch.setattr(runner.diff_gate, "git_changes", lambda base: (["kestrel/a.py"], {}))
    monkeypatch.setattr(runner.static_checks, "type_check_changed",
                        lambda names: ["RULE type check: kestrel/a.py:3: bad. Fix: x"])
    monkeypatch.setattr(runner.static_checks, "lint_changed", lambda lines: [])
    monkeypatch.setattr(runner.test_floor, "run_static", lambda *a: (
        ["RULE assertion floor: tests/test_a.py lost asserts"], ["WARNING assert with or"]))
    assert runner._static_gates() is False
    out = capsys.readouterr().out
    assert "RULE type check" in out and "RULE assertion floor" in out
    assert "WARNING assert with or" in out and "2 static check problem(s)" in out
