"""The local gate runs the same tests in parallel and serial modes."""

from __future__ import annotations

import argparse
import importlib.util
import sys
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
    monkeypatch.setattr(module, "_export_lint", lambda *args: True)  # D-CI-27: tested below
    monkeypatch.setattr(module, "_stress_gate", lambda *args: True)  # D-CI-26: tested below
    # No host cgroup either: the quota tests below write their own files (D-CI-590).
    monkeypatch.setattr(module, "CGROUP_ROOT", Path(__file__).resolve().parent / "no-cgroup-here")
    monkeypatch.setattr(module, "PROC_CGROUP", Path(__file__).resolve().parent / "no-cgroup-here")
    return module


@pytest.mark.parametrize("tier", [[], ["--quick"], ["--fast"]])
def test_every_tier_lints_the_public_export_and_stops_when_it_fails(
        runner, monkeypatch, capsys, tier):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", *tier])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    commands = []
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda cmd, **_kw: (commands.append(cmd) or True, []))
    monkeypatch.setattr(runner, "_export_lint", lambda *args: False)
    assert runner.main() == 1
    assert commands == []
    out = capsys.readouterr().out
    assert "passed; run the full check" not in out and "Ready to push." not in out


def test_full_gate_runs_the_stress_run_and_stops_when_it_fails(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner.sys, "argv", ["check.py"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (True, []))
    called = []
    monkeypatch.setattr(runner, "_stress_gate", lambda *args: called.append(1) or False)
    assert runner.main() == 1
    assert called == [1]
    assert "Ready to push." not in capsys.readouterr().out


@pytest.mark.parametrize("tier", [["--quick"], ["--fast"]])
def test_light_tiers_leave_the_stress_run_to_the_full_gate(runner, monkeypatch, tier):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", *tier])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (True, []))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout=""))
    called = []
    monkeypatch.setattr(runner, "_stress_gate", lambda *args: called.append(1) or True)
    assert runner.main() == 0
    assert called == []


def _stress_tier(runner, monkeypatch, ok):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--stress"])
    other = []
    monkeypatch.setattr(runner, "_run", lambda *args: other.append(args) or True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (other.append(cmd) or True, []))
    monkeypatch.setattr(runner, "_stress_gate", lambda *args: ok)
    return other


def test_stress_tier_runs_only_the_stress_run_and_ends_with_its_pass_line(
        runner, monkeypatch, capsys):
    other = _stress_tier(runner, monkeypatch, True)
    assert runner.main() == 0
    assert other == []
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "Stress checks passed; run the full check before pushing."


def test_a_failed_stress_tier_never_prints_a_pass_line(runner, monkeypatch, capsys):
    other = _stress_tier(runner, monkeypatch, False)
    assert runner.main() == 1
    assert other == []
    assert "passed" not in capsys.readouterr().out


def test_stress_tier_cannot_combine_with_another(runner, monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--stress", "--quick"])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


def _stress_with_runs(runner, monkeypatch, results):
    check = runner
    monkeypatch.setattr(check.diff_gate, "diff_base", lambda: ("base", "note"))
    monkeypatch.setattr(check.diff_gate, "git_changes",
                        lambda base: (["kestrel/a.py"], {}))
    monkeypatch.setattr(check.stress, "read_test_texts",
                        lambda root: {"tests/test_a.py":
                                      "import kestrel.a\nfrom clock_seed import ago"})
    monkeypatch.setattr(check.stress, "read_texts", lambda folder, root: {})
    held = []
    real_holder = check.stress.PortHolder

    class Holder(real_holder):
        def __enter__(self):
            held.append("in")
            return super().__enter__()

    monkeypatch.setattr(check.stress, "PortHolder", lambda: Holder(0))
    class Calls(list):
        counts: list[int | None]

    calls = Calls()
    outcomes = iter(results)

    def fake_run(cmd, extra_env=None):
        calls.append((cmd, extra_env, list(held)))
        ok = next(outcomes)
        check.LAST_RUN["passed"] = counts.pop(0) if counts else (5 if ok else None)
        return ok, []

    counts: list[int | None] = []  # a test sets how many passed in each run, in order
    monkeypatch.setattr(check, "_run_pytest", fake_run)
    calls.counts = counts
    return calls


def test_stress_gate_runs_every_condition_over_the_affected_files(monkeypatch, capsys):
    check = _load_check()
    calls = _stress_with_runs(check, monkeypatch, [True] * 8)
    assert check._stress_gate("py") is True
    # order 1, port held with the fast clock, 00:00:39 and 23:59:30, then four more orders
    assert len(calls) == 8
    assert all(cmd[-1] == "tests/test_a.py" and "-n" in cmd for cmd, _env, _h in calls)
    assert [h for _c, _e, h in calls[:2]] == [[], ["in"]]  # only the second run holds the port
    assert calls[1][1]["KESTREL_CLOCK_RATE"] == "10"
    assert [env["KESTREL_FROZEN_AT"][11:19] for _c, env, _h in calls[2:4]] == [
        "00:00:39", "23:59:30"]
    seeds = {calls[0][1]["KESTREL_STRESS_SEED"]} | {
        env["KESTREL_STRESS_SEED"] for _c, env, _h in calls[4:]}
    assert len(seeds) == 5
    assert "5 random order(s)" in capsys.readouterr().out


def test_stress_gate_stops_repeating_at_the_budget_but_keeps_every_condition(
        monkeypatch, capsys):
    check = _load_check()
    calls = _stress_with_runs(check, monkeypatch, [True] * 8)
    # A fake clock where each run takes 1 s: the real one can read the same value before and
    # after a stubbed run (Windows ticks every ~15.6 ms), and 0 + 0 <= 0 would allow a repeat.
    fake = check._run_pytest
    now = [1000.0]

    def timed_run(cmd, extra_env=None):
        now[0] += 1.0
        return fake(cmd, extra_env=extra_env)

    monkeypatch.setattr(check, "_run_pytest", timed_run)
    monkeypatch.setattr(check.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(check.stress, "BUDGET_S", 0)
    assert check._stress_gate("py") is True
    assert len(calls) == 4  # the first order, port and clock, both midnight runs
    out = capsys.readouterr().out
    assert "1 of 5 random orders ran" in out and "1 random order(s)" in out


def test_a_first_order_that_fails_in_seconds_does_not_make_repeats_look_cheap(
        monkeypatch, capsys):
    check = _load_check()
    # D-CI-591: order 1 fails after 1 s (a collection error), the port-and-clock run over the
    # same files takes 700 s, the midnight runs 10 s each. Judged by order 1, every repeat would
    # fit a 1200 s budget; judged by the longest full run, none does.
    calls = _stress_with_runs(check, monkeypatch, [False] + [True] * 7)
    fake = check._run_pytest
    now = [1000.0]
    durations = iter([1.0, 700.0, 10.0, 10.0, 700.0, 700.0, 700.0, 700.0])

    def timed_run(cmd, extra_env=None):
        now[0] += next(durations)
        return fake(cmd, extra_env=extra_env)

    monkeypatch.setattr(check, "_run_pytest", timed_run)
    monkeypatch.setattr(check.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(check.stress, "BUDGET_S", 1200)
    assert check._stress_gate("py") is False  # order 1 failed
    assert len(calls) == 4  # no repeat: 721 s spent plus a 700 s run is past 1200 s
    out = capsys.readouterr().out
    assert "1 of 5 random orders ran; the longest full run took 700s" in out


def test_stress_gate_runs_every_condition_after_a_failure_and_reports_them_all(
        monkeypatch, capsys):
    check = _load_check()
    # order 1 passes; port-and-clock FAILS; 00:00:39 passes; 23:59:30 FAILS; repeats pass
    calls = _stress_with_runs(check, monkeypatch,
                              [True, False, True, False, True, True, True, True])
    assert check._stress_gate("py") is False
    assert len(calls) == 8  # nothing after the first failure was skipped
    out = capsys.readouterr().out
    assert "2 of the conditions that ran failed" in out
    assert out.count("  FAILED: ") == 2 and out.count("  Replay: ") == 2
    assert "FAILED: port 8765 held, clock at 10x" in out
    assert "FAILED: clock from 23:59:30 UTC" in out
    assert "clock from 00:00:39 UTC" not in out.split("2 of the conditions")[1]
    assert "KESTREL_CLOCK_RATE=10" in out and "KESTREL_FROZEN_AT=" in out
    assert "Not ready to push: the stress run failed (2 condition(s): port 8765 held" in out
    assert "stress run ok" not in out


def test_stress_gate_one_failure_still_fails_the_gate_with_its_replay(monkeypatch, capsys):
    check = _load_check()
    calls = _stress_with_runs(check, monkeypatch, [True, False] + [True] * 6)
    assert check._stress_gate("py") is False
    assert len(calls) == 8
    out = capsys.readouterr().out
    assert "Not ready to push: the stress run failed (1 condition(s): port 8765 held, clock" in out
    assert "Replay: KESTREL_STRESS_SEED=" in out and "KESTREL_CLOCK_RATE=10" in out


def test_a_stress_condition_that_ran_no_test_fails_the_gate_with_its_label(monkeypatch, capsys):
    check = _load_check()
    calls = _stress_with_runs(check, monkeypatch, [True] * 8)
    # order 1 passes 5; port-and-clock exits 0 having run nothing; 00:00:39 only skipped (0 passed);
    # 23:59:30 printed no count at all; the repeats pass
    calls.counts.extend([5, 0, 0, None, 5, 5, 5, 5])
    assert check._stress_gate("py") is False
    out = capsys.readouterr().out
    assert "3 of the conditions that ran failed" in out
    assert "FAILED: port 8765 held, clock at 10x real speed (ran no test that passed)" in out
    assert "FAILED: clock from 00:00:39 UTC (ran no test that passed)" in out
    assert "FAILED: clock from 23:59:30 UTC (ran no test that passed)" in out
    assert "FAILED: random order 1 of 5" not in out
    assert "stress run ok" not in out


def test_the_pytest_runner_reads_how_many_tests_passed(runner, monkeypatch, capsys):
    class Proc:
        stdout = iter(["....\n", "SKIPPED [1] tests/a.py:3: why\n", "4 passed, 1 skipped in 1s\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Proc())
    ok, skips = runner._run_pytest(["pytest"])
    assert ok is True and skips == [(1, "tests/a.py:3", "why")]
    assert runner.LAST_RUN["passed"] == 4
    Proc.stdout = iter(["no tests ran in 0.1s\n"])
    runner._run_pytest(["pytest"])
    assert runner.LAST_RUN["passed"] is None


def test_export_lint_steps_aside_in_the_public_tree(monkeypatch, tmp_path, capsys):
    check = _load_check()
    monkeypatch.setattr(check.diff_gate, "ROOT", tmp_path)  # no scripts/publicize.py here
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: pytest.fail("ran something"))
    assert check._export_lint("py") is True
    assert "this tree is the public export" in capsys.readouterr().out


def _private_tree(check, monkeypatch, tmp_path):
    """A tree with scripts/publicize.py, as the private repo has; the public export has none
    and steps aside (tested above), so these tests set the private case themselves."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "publicize.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(check.diff_gate, "ROOT", tmp_path)


def test_export_lint_fails_when_lint_fails_only_on_the_export(monkeypatch, capsys, tmp_path):
    check = _load_check()
    _private_tree(check, monkeypatch, tmp_path)
    calls = []

    results = iter([
        argparse.Namespace(returncode=0, stdout="wrote 3 files", stderr=""),
        argparse.Namespace(returncode=1, stdout="tests/test_x.py:4:101: E501", stderr=""),
    ])
    monkeypatch.setattr(check.subprocess, "run",
                        lambda cmd, **kwargs: calls.append(cmd) or next(results))
    assert check._export_lint("py") is False
    out = capsys.readouterr().out
    assert "E501" in out and "Not ready to push: lint fails on the public export" in out
    publicize, lint = calls
    assert publicize[1].endswith("publicize.py") and "--lint-only" in publicize
    assert lint[1:4] == ["-m", "ruff", "check"]


def test_export_lint_stops_when_the_export_is_refused(monkeypatch, capsys, tmp_path):
    check = _load_check()
    _private_tree(check, monkeypatch, tmp_path)
    monkeypatch.setattr(check.subprocess, "run", lambda cmd, **kw: argparse.Namespace(
        returncode=2, stdout="", stderr="EXPORT REFUSED: text that must never be public"))
    assert check._export_lint("py") is False
    assert "Not ready to push: the public export failed" in capsys.readouterr().out


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


@pytest.mark.parametrize("cpus,expected", [(None, 1), (1, 1), (2, 2), (4, 4), (32, 4)])
def test_the_browser_stage_defaults_to_one_worker_per_cpu_up_to_four(
        runner, monkeypatch, cpus, expected):
    # D-CI-35: a browser test is CPU bound, so workers beyond the CPUs only slow every page.
    monkeypatch.setattr(runner.os, "cpu_count", lambda: cpus)
    monkeypatch.delattr(runner.os, "sched_getaffinity", raising=False)  # the CPUs this one may use
    assert runner.browser_worker_count("auto") == expected
    assert runner.browser_worker_count("2") == 2
    with pytest.raises(argparse.ArgumentTypeError):
        runner.browser_worker_count("many")


def test_the_browser_stage_runs_in_parallel_by_default_and_honors_the_override(
        runner, monkeypatch):
    monkeypatch.delenv("KESTREL_BROWSER_WORKERS", raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 4)
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    assert runner.main() == 0
    (_main_cmd, _), (browser_cmd, _) = commands
    assert browser_cmd[browser_cmd.index("-n") + 1] == "4"
    assert "--dist" in browser_cmd and "worksteal" in browser_cmd
    commands = _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", "0",
                                             "--browser-workers", "1"])
    assert runner.main() == 0
    assert commands[1][0][commands[1][0].index("-n") + 1] == "1"


def test_the_browser_stage_measures_coverage_through_sys_monitoring(runner, monkeypatch):
    plan = (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, [])
    commands = _full_gate(runner, monkeypatch, plan, [(True, []), (True, [])])
    assert runner.main() == 0
    (_main_cmd, main_kw), (browser_cmd, browser_kw) = commands
    assert "--cov=kestrel" in browser_cmd and "--cov-append" in browser_cmd
    expected = "sysmon" if runner.sys.version_info >= (3, 12) else None
    assert browser_kw["extra_env"].get("COVERAGE_CORE") == expected
    # the main run keeps coverage's default tracer: this change is for the browser stage only
    assert "COVERAGE_CORE" not in (main_kw.get("extra_env") or {})


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
    # "abc" is no real commit; the error code check has its own tests below (D-ERR-6).
    monkeypatch.setattr(runner.error_code_base, "problems", lambda base: [])
    assert runner._static_gates() is False
    out = capsys.readouterr().out
    assert "RULE type check" in out and "RULE assertion floor" in out
    assert "WARNING assert with or" in out and "2 static check problem(s)" in out


def _stub_plumbing(runner, monkeypatch, flag):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", *([flag] if flag else [])])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (True, []))
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout="", returncode=0))


@pytest.mark.parametrize("flag", ["--quick", "--fast", None])
def test_a_stage_that_prints_not_ready_but_reports_success_cannot_exit_zero(
    runner, monkeypatch, capsys, flag
):
    """A real stage (the static checks) prints "Not ready to push" yet returns success, the shape
    of the 0.7.5 push-on-failure. The gate must exit non-zero and end on a "Not ready" line."""
    _stub_plumbing(runner, monkeypatch, flag)

    def lying_static_gates() -> bool:
        print("Not ready to push: 1 static check problem(s) above.")
        return True

    monkeypatch.setattr(runner, "_static_gates", lying_static_gates)
    rc = runner.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out[-1].startswith("Not ready to push")


@pytest.mark.parametrize("flag", ["--quick", "--fast", None])
def test_a_failing_static_stage_exits_nonzero_with_its_words_last(
    runner, monkeypatch, capsys, flag
):
    _stub_plumbing(runner, monkeypatch, flag)

    def failing_static_gates() -> bool:
        print("Not ready to push: 1 static check problem(s) above.")
        return False

    monkeypatch.setattr(runner, "_static_gates", failing_static_gates)
    assert runner.main() == 1
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("Not ready to push")


@pytest.mark.parametrize("abbrev", ["--qu", "--fa"])
def test_an_abbreviated_tier_flag_is_refused_not_guessed(runner, monkeypatch, abbrev):
    """argparse would accept --qu as --quick; the tier would then be misjudged."""
    _stub_plumbing(runner, monkeypatch, abbrev)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2


def test_real_pass_still_exits_zero(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--quick"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd, **_kw: (True, []))
    assert runner.main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        "Quick checks passed; run the full check before pushing.")


def test_browser_workers_honor_the_cpus_this_process_may_use_and_the_environment(
        runner, monkeypatch):
    if not hasattr(runner.os, "sched_getaffinity"):
        pytest.skip("no CPU affinity on this platform")
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda pid: {0, 1})
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 16)
    assert runner.browser_worker_count("auto") == 2
    monkeypatch.setenv("KESTREL_BROWSER_WORKERS", "1")
    parser = runner._build_parser()
    assert parser.parse_args([]).browser_workers == 1
    assert parser.parse_args(["--browser-workers", "0"]).browser_workers == 0


@pytest.mark.parametrize("flags,pass_line", [
    (["--quick"], "Quick checks passed"),
    (["--fast"], "Fast checks passed"),
    ([], "Ready to push."),
])
def test_no_tier_passes_without_a_git_base(monkeypatch, capsys, flags, pass_line):
    runner = _load_check()  # the real plan and gates, not the fixture's stand-ins
    # D-CI-37: with nothing to compare with, the static checks, the red-first proof and the
    # diff guards cannot run, so no tier may print its pass line.
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", "0", *flags])
    monkeypatch.setattr(runner.diff_gate, "diff_base",
                        lambda: (None, "neither origin/main nor main exists"))
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    monkeypatch.setattr(runner, "_run_pytest",
                        lambda *a, **k: pytest.fail("ran tests with no base"))
    assert runner.main() == 1
    out = capsys.readouterr().out
    assert "Not ready to push" in out and "git fetch origin main" in out
    assert pass_line not in out


def test_the_static_and_red_first_gates_refuse_with_no_base(monkeypatch, capsys):
    runner = _load_check()
    monkeypatch.setattr(runner.diff_gate, "diff_base", lambda: (None, "no base"))
    assert runner._static_gates() is False
    assert runner._red_first_gate() is False
    assert capsys.readouterr().out.count("git fetch origin main") == 2


def test_a_failing_affinity_call_falls_back_to_the_cpu_count(runner, monkeypatch):
    def refuse(pid):
        raise OSError("affinity not permitted")

    monkeypatch.setattr(runner.os, "sched_getaffinity", refuse, raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 8)
    assert runner.browser_worker_count("auto") == 4
    monkeypatch.setattr(runner.os, "cpu_count", lambda: None)
    assert runner.browser_worker_count("auto") == 1
    parser = runner._build_parser()
    assert parser.parse_args([]).browser_workers == 1


@pytest.mark.parametrize("files,expected", [
    ({"cpu.max": "200000 100000\n"}, 2),          # cgroup v2: two CPUs' worth
    ({"cpu.max": "150000 100000\n"}, 2),          # a part CPU rounds up
    ({"cpu.max": "50000 100000\n"}, 1),           # never below one
    ({"cpu.max": "max 100000\n"}, 8),             # no limit
    ({"cpu/cpu.cfs_quota_us": "300000\n", "cpu/cpu.cfs_period_us": "100000\n"}, 3),  # v1
    ({"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"}, 8),       # v1 none
    ({"cpu.max": "garbage\n"}, 8),                # unreadable is no quota, never a guess
    ({}, 8),                                      # no cgroup files at all
])
def test_a_cgroup_cpu_quota_caps_the_browser_workers(runner, monkeypatch, tmp_path, files,
                                                     expected):
    # D-CI-590: a container limited to two CPUs by a quota still reports the host's CPUs to
    # affinity; one browser worker per reported CPU would push every page past its waits.
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(runner, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 8)
    assert runner._usable_cpus() == expected
    assert runner.browser_worker_count("auto") == min(expected, runner.BROWSER_WORKER_CAP)


@pytest.mark.parametrize("own,files,expected", [
    # cgroup v2 without a cgroup namespace: the process sits below the mount, whose own cpu.max
    # does not exist, so the quota is only found under the path /proc/self/cgroup names.
    ("0::/user.slice/job.scope", {"user.slice/job.scope/cpu.max": "200000 100000\n"}, 2),
    # a quota on a parent limits the child even when the child's own file says "max"
    ("0::/user.slice/job.scope", {"user.slice/job.scope/cpu.max": "max 100000\n",
                                  "user.slice/cpu.max": "300000 100000\n"}, 3),
    # the tightest of the own folder and its ancestors wins
    ("0::/a/b", {"a/b/cpu.max": "300000 100000\n", "a/cpu.max": "100000 100000\n"}, 1),
    # cgroup v1, cpu controller joined with cpuacct, below the cpu mount
    ("4:cpu,cpuacct:/docker/abc\n3:memory:/docker/abc",
     {"cpu/docker/abc/cpu.cfs_quota_us": "250000\n",
      "cpu/docker/abc/cpu.cfs_period_us": "100000\n"}, 3),
    # a v1 line for another controller only is not read as a cpu quota
    ("3:memory:/docker/abc", {"memory/docker/abc/cpu.cfs_quota_us": "100000\n",
                              "memory/docker/abc/cpu.cfs_period_us": "100000\n"}, 8),
])
def test_the_cgroup_quota_is_read_from_this_process_own_cgroup(runner, monkeypatch, tmp_path,
                                                                own, files, expected):
    # D-CI-594: resolve the process's own cgroup from /proc/self/cgroup, then its ancestors.
    root = tmp_path / "fs"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    proc = tmp_path / "self-cgroup"
    proc.write_text(own + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "CGROUP_ROOT", root)
    monkeypatch.setattr(runner, "PROC_CGROUP", proc)
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 8)
    assert runner._usable_cpus() == expected


# ---- error codes stay permanent across branches (D-ERR-6, D-ERR-8) ------------------------------

def _load_error_code_base():
    """The checker, loaded once: a fixture's patch of its ROOT must not be undone by a reload."""
    if "error_code_base_under_test" in sys.modules:
        return sys.modules["error_code_base_under_test"]
    path = Path(__file__).resolve().parents[1] / "scripts" / "error_code_base.py"
    spec = importlib.util.spec_from_file_location("error_code_base_under_test", path)
    module = importlib.util.module_from_spec(spec)
    loaded = sys.modules.setdefault("error_code_base_under_test", module)
    spec.loader.exec_module(loaded)
    return loaded


def _registry_source(live, retired=(), extra: str = "") -> str:
    """A registry built from the checker's own reviewed form, with these entries in it."""
    entries = "".join(
        f'    _code("{c}", "{t}", "Sample meaning.", "Sample fix."),\n' for c, t in live
    )
    old = "".join(
        f'    _retired(_code("{c}", "{t}", "Sample meaning.", "Sample fix."), "{d}"),\n'
        for c, t, d in retired
    )
    source = _load_error_code_base().CANONICAL
    for empty, filled in (
        ("_ALL: tuple[ErrorCode, ...] = ()", f"_ALL: tuple[ErrorCode, ...] = (\n{entries})"),
        ("_RETIRED_ALL: tuple[RetiredCode, ...] = ()",
         f"_RETIRED_ALL: tuple[RetiredCode, ...] = (\n{old})"),
    ):
        assert source.count(empty) == 1
        source = source.replace(empty, filled)
    return source + extra


_SAMPLE_TITLES = {
    "STA-001": "First sample",
    "STA-002": "Second sample",
    "STA-003": "Third sample",
    "APP-001": "Old sample",
}


def _issued_source(codes, extra: str = "", titles=None, ever_retired=None) -> str:
    """The three frozen lists of the guard test, for these codes."""
    titles = {c: _SAMPLE_TITLES[c] for c in codes} if titles is None else titles
    ever_retired = [c for c in codes if c == "APP-001"] if ever_retired is None else ever_retired
    issued = "".join(f'    "{c}",\n' for c in codes)
    frozen = "".join(f'    "{c}": "{t}",\n' for c, t in titles.items())
    retired = "".join(f'    "{c}",\n' for c in ever_retired)
    return (
        f"ISSUED: tuple[str, ...] = (\n{issued})\n"
        f"EVER_RETIRED: tuple[str, ...] = (\n{retired})\n"
        f"FROZEN_TITLES: dict[str, str] = {{\n{frozen}}}\n{extra}"
    )


_BASE_LIVE = [("STA-001", "First sample"), ("STA-002", "Second sample")]
_BASE_OLD = [("APP-001", "Old sample", "2026-01-02")]
_BASE_ISSUED = ["STA-001", "STA-002", "APP-001"]


def _git_in(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "kestrel").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git_in(repo, "init", "-q")
    _git_in(repo, "config", "user.email", "tester@example.test")
    _git_in(repo, "config", "user.name", "Tester")
    return repo


@pytest.fixture
def code_repo(tmp_path, monkeypatch):
    """A throwaway git repo whose first commit is the base: two live codes, one retired."""
    module = _load_error_code_base()
    repo = _new_repo(tmp_path)
    registry = repo / "kestrel" / "error_codes.py"
    issued = repo / "tests" / "test_error_codes.py"
    registry.write_text(_registry_source(_BASE_LIVE, _BASE_OLD), encoding="utf-8")
    issued.write_text(_issued_source(_BASE_ISSUED), encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "base")
    monkeypatch.setattr(module, "ROOT", repo)
    return module, _git_in(repo, "rev-parse", "HEAD"), registry, issued


def test_error_code_base_passes_an_unchanged_registry_and_head_as_the_base(code_repo):
    module, base, _registry, _issued = code_repo
    assert module.problems(base) == []


def test_error_code_base_passes_a_new_code_appended_in_its_area(code_repo):
    module, base, registry, issued = code_repo
    registry.write_text(
        _registry_source([*_BASE_LIVE, ("STA-003", "Third sample")], _BASE_OLD), encoding="utf-8"
    )
    issued.write_text(_issued_source(["STA-001", "STA-002", "STA-003", "APP-001"]),
                      encoding="utf-8")
    assert module.problems(base) == []


def test_error_code_base_refuses_swapped_titles(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text(
        _registry_source([("STA-001", "Second sample"), ("STA-002", "First sample")], _BASE_OLD),
        encoding="utf-8",
    )
    found = module.problems(base)
    assert len(found) == 2
    assert all("title changed" in p and "never reused" in p for p in found)


def test_error_code_base_refuses_a_deleted_code_even_with_the_lists_edited(code_repo):
    module, base, registry, issued = code_repo
    registry.write_text(_registry_source(_BASE_LIVE[:1], _BASE_OLD), encoding="utf-8")
    issued.write_text(_issued_source(["STA-001", "APP-001"]), encoding="utf-8")
    found = module.problems(base)
    assert any("STA-002" in p and "is gone" in p for p in found)
    assert any("ISSUED" in p and "area STA" in p for p in found)


def test_error_code_base_refuses_a_retired_code_brought_back(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text(_registry_source([*_BASE_LIVE, ("APP-001", "Old sample")]),
                        encoding="utf-8")
    found = module.problems(base)
    assert found == [p for p in found if "APP-001" in p]
    assert any("never comes back" in p for p in found)


def test_error_code_base_refuses_a_retired_date_edited(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text(
        _registry_source(_BASE_LIVE, [("APP-001", "Old sample", "2026-03-04")]), encoding="utf-8"
    )
    found = module.problems(base)
    assert len(found) == 1 and "APP-001 was retired on main (2026-01-02)" in found[0]


def test_error_code_base_refuses_a_code_both_live_and_retired(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text(
        _registry_source([*_BASE_LIVE, ("APP-001", "Old sample")], _BASE_OLD), encoding="utf-8"
    )
    assert any("APP-001 is both live and retired" in p for p in module.problems(base))


def test_error_code_base_refuses_a_duplicate_code(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text(_registry_source([*_BASE_LIVE, _BASE_LIVE[1]], _BASE_OLD),
                        encoding="utf-8")
    assert any("STA-002 is defined more than once" in p for p in module.problems(base))


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("_code takes its arguments in another order",
         "def _code(code: str, title: str, meaning: str,",
         "def _code(code: str, meaning: str, title: str,"),
        ("an extra statement in _code",
         "    return ErrorCode(\n        code=code,",
         "    (title := 'Reused') if code == 'STA-001' else None\n    return ErrorCode(\n"
         "        code=code,"),
        ("a decorator on _code", "def _code(", "@staticmethod\ndef _code("),
        ("another decorator on ErrorCode", "@dataclass(frozen=True)\nclass ErrorCode:",
         "@dataclass(frozen=False)\nclass ErrorCode:"),
        ("a base class", "class ErrorCode:", "class ErrorCode(Exception):"),
        ("a method on ErrorCode", "    since: str\n",
         "    since: str\n\n    def __post_init__(self):\n        pass\n"),
        ("an import that shadows a name", "from dataclasses import dataclass",
         "from dataclasses import dataclass\nfrom types import SimpleNamespace as ErrorCode"),
        ("a nested redefinition", "_ALL: tuple[ErrorCode, ...] = (",
         "if True:\n    def _code(*a):\n        return None\n_ALL: tuple[ErrorCode, ...] = ("),
        ("a walrus in an annotation", "_ALL: tuple[ErrorCode, ...] = (",
         "_x: (_y := ErrorCode) = None\n_ALL: tuple[ErrorCode, ...] = ("),
        ("CODES built another way", "{entry.code: entry for entry in _ALL}",
         "{entry.code: entry for entry in _ALL[:1]}"),
        ("CODES changed in place after it is built", "\ndef get(",
         '\nCODES["STA-001"] = CODES["STA-002"]\n\n\ndef get('),
    ],
)
def test_error_code_base_refuses_any_change_outside_the_entries(code_repo, label, old, new):
    module, base, registry, _issued = code_repo
    source = _registry_source(_BASE_LIVE, _BASE_OLD)
    assert source.count(old) == 1, label
    registry.write_text(source.replace(old, new, 1), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1, label
    assert "differs from the reviewed form" in found[0], label


@pytest.mark.parametrize(
    ("label", "old", "new", "said"),
    [
        ("a non-literal title", '_code("STA-002", "Second sample"', '_code("STA-002", NAME',
         "title that is not a plain string"),
        ("a direct ErrorCode entry", '    _code("STA-002", "Second sample", "Sample meaning.", '
         '"Sample fix."),', '    ErrorCode("STA-002", "STA", "New", "M.", "F.", "0.7.7"),',
         "not a literal _code(...) call"),
        ("a retired entry in another form", '_retired(_code("APP-001", "Old sample", '
         '"Sample meaning.", "Sample fix."), "2026-01-02")', '_retired("APP-001", "2026-01-02")',
         "not a literal _code(...) call"),
    ],
)
def test_error_code_base_refuses_an_entry_that_is_not_a_literal(code_repo, label, old, new, said):
    module, base, registry, _issued = code_repo
    source = _registry_source(_BASE_LIVE, _BASE_OLD)
    assert source.count(old) == 1, label
    registry.write_text(source.replace(old, new), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1, label
    assert said in found[0], label


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ("ISSUED = ISSUED[:1]\n", "bound exactly once"),
        ('ISSUED += ("STA-001",)\n', "bound exactly once"),
        ("\ndef f():\n    global ISSUED\n", "bound exactly once"),
        ('globals()["ISSUED"] = ()\n', "reached indirectly"),
        ('setattr(sys.modules[__name__], "ISSUED", ())\n', "reached indirectly"),
    ],
)
def test_error_code_base_refuses_issued_rebound(code_repo, extra, said):
    module, base, _registry, issued = code_repo
    issued.write_text(_issued_source(_BASE_ISSUED, extra=extra), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and said in found[0]


def test_error_code_base_refuses_a_base_it_cannot_read(code_repo):
    module, _base, _registry, _issued = code_repo
    found = module.problems("0" * 40)
    assert len(found) == 1 and "cannot read the base commit" in found[0]


def test_error_code_base_with_no_registry_at_the_base_checks_only_this_branch(
    tmp_path, monkeypatch
):
    module = _load_error_code_base()
    repo = _new_repo(tmp_path)
    (repo / "README.md").write_text("Sample.\n", encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "before codes")
    base = _git_in(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(module, "ROOT", repo)
    registry = repo / "kestrel" / "error_codes.py"
    (repo / "tests" / "test_error_codes.py").write_text(_issued_source(["STA-001"]),
                                                        encoding="utf-8")
    registry.write_text(_registry_source(_BASE_LIVE[:1]), encoding="utf-8")
    assert module.problems(base) == []
    registry.write_text(_registry_source([_BASE_LIVE[0], _BASE_LIVE[0]]), encoding="utf-8")
    assert any("defined more than once" in p for p in module.problems(base))


def test_the_real_registry_reads_cleanly():
    module = _load_error_code_base()
    root = Path(__file__).resolve().parents[1]
    reg = module.parse_registry((root / "kestrel" / "error_codes.py").read_text(encoding="utf-8"))
    from kestrel.error_codes import CODES, RETIRED

    assert reg.live == list(CODES)
    assert reg.retired == {c: item.retired for c, item in RETIRED.items()}
    assert reg.duplicates == []
    issued = module.parse_issued(
        (root / "tests" / "test_error_codes.py").read_text(encoding="utf-8")
    )
    assert set(issued) == set(CODES) | set(RETIRED)


def test_the_static_gate_runs_the_error_code_check(monkeypatch, capsys):
    runner = _load_check()
    monkeypatch.setattr(runner.diff_gate, "diff_base", lambda: ("abc", "note"))
    monkeypatch.setattr(runner.diff_gate, "git_changes", lambda base: ([], {}))
    monkeypatch.setattr(runner.static_checks, "type_check_changed", lambda names: [])
    monkeypatch.setattr(runner.static_checks, "lint_changed", lambda lines: [])
    monkeypatch.setattr(runner.test_floor, "run_static", lambda *a: ([], []))
    seen = []
    monkeypatch.setattr(runner.error_code_base, "problems",
                        lambda base: seen.append(base) or ["RULE error codes: STA-001 is gone."])
    assert runner._static_gates() is False
    assert seen == ["abc"]
    assert "RULE error codes: STA-001 is gone." in capsys.readouterr().out


def test_the_error_code_check_is_a_protected_path():
    """Weakening the check or its reviewed form needs a decision ID in a commit (D-ERR-9)."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "test_floor.py"
    spec = importlib.util.spec_from_file_location("test_floor_for_error_codes", path)
    fresh = importlib.util.module_from_spec(spec)
    floor = sys.modules.setdefault("test_floor_for_error_codes", fresh)
    spec.loader.exec_module(floor)
    decisions = "D-SAMPLE-1 (2026-11-13): a sample decision.\n### D-SAMPLE-2 - another one\n"
    changed = ["scripts/error_code_base.py"]
    found = floor.protected_problems(changed, {}, {}, "feat: something", decisions)
    assert len(found) == 1 and "scripts/error_code_base.py" in found[0]
    for recorded in ("D-SAMPLE-1", "D-SAMPLE-2"):
        line = f"{recorded}: scripts/error_code_base.py now holds a new reviewed form"
        assert floor.protected_problems(changed, {}, {}, line, decisions) == []
    made_up = "D-SAMPLE-9: scripts/error_code_base.py now holds a new reviewed form"
    found = floor.protected_problems(changed, {}, {}, made_up, decisions)
    assert len(found) == 1 and "scripts/error_code_base.py" in found[0]
    # Other protected paths keep the old rule: any decision ID on the line (D-ERR-13).
    assert floor.protected_problems(["scripts/check.py"], {}, {},
                                    "D-SAMPLE-9: scripts/check.py changes", decisions) == []


# ---- round 3: encoding, shadowing, ASCII codes, the integration base (D-ERR-11, D-ERR-12) -------


def test_error_code_base_refuses_an_encoding_cookie(code_repo):
    """A raw_unicode_escape cookie makes the import system run text that ast.parse on a str
    never sees; the check reads bytes and refuses any cookie but UTF-8."""
    module, base, registry, _issued = code_repo
    source = _registry_source(_BASE_LIVE, _BASE_OLD)
    sneaky = "# -*- coding: raw_unicode_escape -*-\n" + source + (
        '# \\u000aCODES["STA-001"] = CODES["STA-002"]\n'
    )
    registry.write_text(sneaky, encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and "declares the encoding raw_unicode_escape" in found[0]


def test_error_code_base_refuses_a_bom_and_a_cookie_in_the_test_lists(code_repo):
    module, base, registry, issued = code_repo
    registry.write_bytes(b"\xef\xbb\xbf" + _registry_source(_BASE_LIVE, _BASE_OLD).encode())
    assert "declares the encoding utf-8-sig" in module.problems(base)[0]
    registry.write_text(_registry_source(_BASE_LIVE, _BASE_OLD), encoding="utf-8")
    issued.write_text("# coding: latin-1\n" + _issued_source(_BASE_ISSUED), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and "declares the encoding iso-8859-1" in found[0]


def test_a_plain_utf8_cookie_is_accepted(code_repo):
    module, base, registry, _issued = code_repo
    registry.write_text("# -*- coding: utf-8 -*-\n" + _registry_source(_BASE_LIVE, _BASE_OLD),
                        encoding="utf-8")
    assert module.problems(base) == []


@pytest.mark.parametrize(
    "shadow", ["error_codes/__init__.py", "error_codes.pyi", "error_codes.cpython-312.so"]
)
def test_error_code_base_refuses_a_file_that_would_shadow_the_registry(code_repo, shadow):
    module, base, registry, _issued = code_repo
    target = registry.parent / shadow
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CODES = {}\n", encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and "would shadow kestrel/error_codes.py" in found[0]


def test_a_sibling_module_with_a_longer_name_is_not_a_shadow(code_repo):
    module, base, registry, _issued = code_repo
    (registry.parent / "error_codes_view.py").write_text("X = 1\n", encoding="utf-8")
    assert module.problems(base) == []


def test_error_code_base_refuses_a_lookalike_code(code_repo):
    module, base, registry, issued = code_repo
    lookalike = "STA-00\uff13"  # a full-width digit three
    registry.write_text(
        _registry_source([*_BASE_LIVE, (lookalike, "Third sample")], _BASE_OLD), encoding="utf-8"
    )
    issued.write_text(
        _issued_source(["STA-001", "STA-002", lookalike, "APP-001"],
                       titles={**{c: _SAMPLE_TITLES[c] for c in _BASE_ISSUED},
                               lookalike: "Third sample"}),
        encoding="utf-8",
    )
    found = module.problems(base)
    assert any("is not a code" in p and "ISSUED" not in p for p in found)
    assert any("in ISSUED is not a code" in p for p in found)


def test_error_code_base_refuses_frozen_titles_or_ever_retired_edited(code_repo):
    module, base, _registry, issued = code_repo
    titles = {**{c: _SAMPLE_TITLES[c] for c in _BASE_ISSUED}, "STA-001": "Changed sample"}
    issued.write_text(_issued_source(_BASE_ISSUED, titles=titles), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and "FROZEN_TITLES" in found[0] and "STA-001" in found[0]
    issued.write_text(_issued_source(_BASE_ISSUED, ever_retired=[]), encoding="utf-8")
    found = module.problems(base)
    assert len(found) == 1 and "EVER_RETIRED" in found[0] and "APP-001" in found[0]


def _two_bases(tmp_path, monkeypatch):
    """main has no registry; the integration branch has one; HEAD deletes a code from it."""
    module = _load_error_code_base()
    repo = _new_repo(tmp_path)
    (repo / "README.md").write_text("Sample.\n", encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "main")
    main = _git_in(repo, "rev-parse", "HEAD")
    registry = repo / "kestrel" / "error_codes.py"
    issued = repo / "tests" / "test_error_codes.py"
    registry.write_text(_registry_source(_BASE_LIVE, _BASE_OLD), encoding="utf-8")
    issued.write_text(_issued_source(_BASE_ISSUED), encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "task A")
    task_a = _git_in(repo, "rev-parse", "HEAD")
    registry.write_text(_registry_source(_BASE_LIVE[:1], _BASE_OLD), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", repo)
    return module, repo, main, task_a


def test_the_base_is_the_integration_branch_when_it_exists(tmp_path, monkeypatch):
    module, repo, main, task_a = _two_bases(tmp_path, monkeypatch)
    _git_in(repo, "update-ref", f"refs/remotes/{module.INTEGRATION_REF}", task_a)
    assert module.resolve_base(main) == task_a
    assert any("STA-002" in p and "is gone" in p for p in module.problems(main))


def test_the_base_falls_back_to_main_without_the_integration_branch(tmp_path, monkeypatch):
    module, _repo, main, _task_a = _two_bases(tmp_path, monkeypatch)
    assert module.resolve_base(main) == main
    assert module.problems(main) == []  # main has no registry yet, so nothing to keep


# ---- KESTREL_DIFF_BASE is honored by --stress alone, ignored everywhere else (D-PROC-15) ---------

def test_base_override_is_read_only_for_the_stress_tier_run_alone(capsys):
    check = _load_check()
    env = {"KESTREL_DIFF_BASE": " cut "}
    assert check.base_override(True, env) == "cut"
    assert check.base_override(False, env) is None
    assert "KESTREL_DIFF_BASE=cut is ignored by this tier" in capsys.readouterr().out
    assert check.base_override(True, {}) is None
    assert check.base_override(False, {}) is None
    assert capsys.readouterr().out == ""


def test_stress_alone_selects_from_the_override_base(monkeypatch, capsys):
    runner = _load_check()
    monkeypatch.setenv("KESTREL_DIFF_BASE", "cut")
    calls = _stress_with_runs(runner, monkeypatch, [True] * 8)
    asked = []
    monkeypatch.setattr(runner.diff_gate, "diff_base",
                        lambda *override: asked.append(override) or ("near", "note: OVERRIDDEN"))
    seen = []
    monkeypatch.setattr(runner.diff_gate, "git_changes",
                        lambda base: seen.append(base) or (["kestrel/a.py"], {}))
    assert runner._stress_gate("py", True) is True
    assert asked == [("cut",)] and seen == ["near"]
    assert "==> stress: note: OVERRIDDEN" in capsys.readouterr().out
    assert calls


def test_stress_inside_the_full_gate_never_takes_the_override(monkeypatch, capsys):
    runner = _load_check()
    monkeypatch.setenv("KESTREL_DIFF_BASE", "cut")
    _stress_with_runs(runner, monkeypatch, [True] * 8)
    asked = []
    monkeypatch.setattr(runner.diff_gate, "diff_base",
                        lambda *override: asked.append(override) or ("main-base", "note"))
    assert runner._stress_gate("py") is True
    assert asked == [()]
    assert "is ignored by this tier" in capsys.readouterr().out


def test_a_refused_override_fails_the_stress_tier(monkeypatch, capsys):
    runner = _load_check()
    monkeypatch.setenv("KESTREL_DIFF_BASE", "cut")
    monkeypatch.setattr(runner.diff_gate, "diff_base",
                        lambda *override: (None, "KESTREL_DIFF_BASE=cut is HEAD itself"))
    assert runner._stress_gate("py", True) is False
    assert "Not ready to push: the stress run needs a git base" in capsys.readouterr().out


def test_the_stress_tier_tells_the_stress_gate_it_runs_alone(runner, monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--stress"])
    got = []
    monkeypatch.setattr(runner, "_stress_gate", lambda *args: got.append(args[1:]) or True)
    assert runner.main() == 0
    assert got == [(True,)]


def test_a_character_the_output_cannot_hold_never_crashes_the_gate(monkeypatch):
    """Windows writes redirected output as cp1252; one U+FFFD in test output crashed the gate."""
    import io

    check = _load_check()
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    check._never_fail_on_a_character()
    print("a test said �")
    out.flush()
    assert raw.getvalue().decode("cp1252").startswith("a test said \\ufffd")
