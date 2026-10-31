"""The local gate runs the same tests in parallel and serial modes."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.delenv("KESTREL_TEST_WORKERS", raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "check.py"
    spec = importlib.util.spec_from_file_location("check_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd: (commands.append(cmd) or True, []))
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
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd: (False, []))
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
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd: (commands.append(cmd) or False, []))
    assert runner.main() == 1
    assert commands[0][commands[0].index("-n") + 1] == expected


def test_explicit_worker_count_overrides_environment(runner, monkeypatch):
    monkeypatch.setenv("KESTREL_TEST_WORKERS", "3")
    monkeypatch.setattr(runner.sys, "argv", ["check.py", "--workers", "2"])
    monkeypatch.setattr(runner, "_run", lambda *args: True)
    commands = []
    monkeypatch.setattr(runner, "_run_pytest", lambda cmd: (commands.append(cmd) or False, []))
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
