"""The fail-fast order of the full gate (scripts/check.py, D-CI-610 to D-CI-616).

The gate runs with canned pytest runs: each stand-in run records its command and, when a test
tells it to, writes the stage's first-failure file the way tests/failfast_plugin.py would. The
guards (G4 to G10) are named in each test; the plugin's own guards are in
tests/test_failfast_plugin.py and the old-against-new command proof in
tests/test_failfast_invariant.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPTION = "--kestrel-failfast-file"


@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.delenv("KESTREL_TEST_WORKERS", raising=False)
    monkeypatch.delenv("KESTREL_KEEP_GOING", raising=False)
    spec = importlib.util.spec_from_file_location("check_failfast", ROOT / "scripts" / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_plan_diff_guards", lambda: ([], {}, []))
    monkeypatch.setattr(module, "_coverage_step", lambda *args: True)
    monkeypatch.setattr(module, "_static_gates", lambda: True)
    monkeypatch.setattr(module, "_red_first_gate", lambda: True)
    monkeypatch.setattr(module, "_browser_evidence_gate", lambda *args: True)
    monkeypatch.setattr(module, "_export_lint", lambda *args: True)
    monkeypatch.setattr(module, "_stress_gate", lambda *args: True)
    monkeypatch.setattr(module, "_changed_selection", lambda: ([], []))
    monkeypatch.setattr(module, "_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(module, "FAILFAST_RECORD", tmp_path / "gate" / "failfast-last.json")
    monkeypatch.setattr(module, "_run", lambda *args: True)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *args, **kwargs: argparse.Namespace(stdout="", returncode=0))
    monkeypatch.setattr(module.sys, "argv", ["check.py", "--workers", "8"])
    return module


class Runs:
    """Stand-in for check._run_pytest. `results` maps a stage marker (a substring of the
    command) to (exit code, passed count, failing test id or None)."""

    def __init__(self, check, results=None):
        self.check = check
        self.results = results or {}
        self.calls: list[tuple[list[str], dict]] = []
        self.files_seen: list[tuple[Path, bool]] = []

    def __call__(self, cmd, extra_env=None):
        self.calls.append((cmd, extra_env or {}))
        code, passed, failed = 0, 1, None
        for marker, result in self.results.items():
            if marker in " ".join(cmd):
                code, passed, failed = result
        if OPTION in cmd:
            first = Path(cmd[cmd.index(OPTION) + 1])
            self.files_seen.append((first, first.exists()))
            if failed:
                first.write_text(failed, encoding="utf-8")
        self.check.LAST_RUN["exit"] = code
        self.check.LAST_RUN["passed"] = passed
        return code == 0, []

    def suite(self):
        return [cmd for cmd, _ in self.calls if "--cov=kestrel" in cmd and "-rs" in cmd
                and not any(a.endswith("_browser.py") for a in cmd)]


def _lines(capsys):
    return capsys.readouterr().out.strip().splitlines()


# ---- G5: a stage whose first-failure file exists never passes --------------------------------

def test_a_stage_with_a_first_failure_file_fails_even_when_pytest_exits_zero(gate, monkeypatch,
                                                                            capsys):
    runs = Runs(gate, {"worksteal": (0, 5, "tests/test_phone.py::test_a")})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 1
    out = _lines(capsys)
    assert "FIRST FAILURE in stage S7 suite: tests/test_phone.py::test_a" in out
    assert out[-1].startswith("Not ready to push")
    assert "Ready to push." not in out


def test_every_stage_gets_a_fresh_file_that_does_not_exist_yet(gate, monkeypatch):
    plan = (["tests/test_nav_browser.py"], {}, [])
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: plan)
    monkeypatch.setattr(gate, "_changed_selection", lambda: (["tests/test_phone.py"], []))
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "sha": "a" * 40, "failed": ["tests/test_phone.py::test_a"],
         "stage": "S7 suite"}), encoding="utf-8")
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    files = [path for path, _existed in runs.files_seen]
    assert len(files) == 4  # S4, S5, S6, S7
    assert len({p.parent for p in files}) == 4
    assert [existed for _path, existed in runs.files_seen] == [False] * 4
    assert not any(p.parent.exists() for p in files)  # each folder is removed after its stage


# ---- G4: the pass line needs every required stage --------------------------------------------

@pytest.mark.parametrize("dropped,plan", [
    ("S8 stress", ([], {}, [])),
    ("S7 suite", ([], {}, [])),
    ("S10 coverage", ([], {}, [])),
    ("S11 red-first", ([], {}, [])),
    ("S6 browser", (["tests/test_nav_browser.py"], {}, [])),
    ("S9 browser", ([f"tests/test_p{n}_browser.py" for n in range(13)], {}, [])),
])
def test_a_stage_dropped_from_the_run_list_blocks_the_pass_line(gate, monkeypatch, capsys,
                                                                 dropped, plan):
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: plan)
    monkeypatch.setattr(gate, "_run_pytest", Runs(gate))
    real = gate._full_stages

    def without(*args, **kwargs):
        stages, browser = real(*args, **kwargs)
        assert dropped in [name for name, _ in stages]
        return [s for s in stages if s[0] != dropped], browser

    monkeypatch.setattr(gate, "_full_stages", without)
    assert gate.main() == 1
    out = _lines(capsys)
    assert "Ready to push." not in out
    assert dropped in out[-1] and out[-1].startswith("Not ready to push")


# ---- stage order (D-CI-610, D-CI-613) ----------------------------------------------------------

def test_the_order_is_changed_tests_then_record_then_few_browser_files_then_the_suite(
        gate, monkeypatch):
    plan = (["tests/test_nav_browser.py"], {}, [])
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: plan)
    monkeypatch.setattr(gate, "_changed_selection",
                        lambda: (["tests/test_phone.py", "tests/test_urls.py"], []))
    order = []
    monkeypatch.setattr(gate, "_stress_gate", lambda *a: order.append("stress") or True)
    runs = Runs(gate)

    def tagged(cmd, extra_env=None):
        joined = " ".join(cmd)
        order.append("S4" if "--dist load" in joined else "S6" if "_browser.py" in joined
                     else "S7" if "--cov=kestrel" in cmd else "S5")
        return runs(cmd, extra_env=extra_env)

    monkeypatch.setattr(gate, "_run_pytest", tagged)
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "sha": "b" * 40, "failed": ["tests/test_phone.py::test_a"],
         "stage": "S7 suite"}), encoding="utf-8")
    assert gate.main() == 0
    assert order == ["S4", "S5", "S6", "S7", "stress"]
    s4_cmd, s4_env = runs.calls[0]
    assert s4_cmd[-2:] == ["tests/test_phone.py", "tests/test_urls.py"]  # every selected file
    assert not any(a.startswith("--cov") for a in s4_cmd)
    assert s4_env["KESTREL_BOUNDARY"] == "1"  # the full gate's environment (Codex point 7)


def test_more_than_twelve_browser_files_run_after_the_suite_and_stress(gate, monkeypatch):
    files = [f"tests/test_p{n}_browser.py" for n in range(13)]
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: (files, {}, []))
    order = []
    monkeypatch.setattr(gate, "_stress_gate", lambda *a: order.append("stress") or True)
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest",
                        lambda cmd, extra_env=None: order.append(
                            "browser" if cmd[-1] in files else "suite") or runs(cmd, extra_env))
    assert gate.main() == 0
    assert order == ["suite", "stress", "browser"]
    browser_cmd, browser_env = runs.calls[-1]
    assert browser_cmd[-13:] == files and browser_env["KESTREL_LIVE_BROWSER"] == "1"


def test_a_failure_in_the_changed_tests_stops_before_the_suite_and_is_recorded(
        gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_changed_selection", lambda: (["tests/test_phone.py"], []))
    runs = Runs(gate, {"--dist load": (2, 3, "tests/test_phone.py::test_b")})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 1
    assert len(runs.calls) == 1
    out = _lines(capsys)
    assert out[-2] == "FIRST FAILURE in stage S4 changed: tests/test_phone.py::test_b"
    assert out[-1].startswith("Not ready to push") and "S4" in out[-1]
    record = json.loads(gate.FAILFAST_RECORD.read_text(encoding="utf-8"))
    assert record == {"version": 1, "sha": "a" * 40, "failed": ["tests/test_phone.py::test_b"],
                      "stage": "S4 changed"}


def test_too_many_changed_test_files_leave_the_work_to_the_suite(gate, monkeypatch, capsys):
    many = [f"tests/test_m{n}.py" for n in range(gate.CHANGED_TESTS_MAX + 1)]
    monkeypatch.setattr(gate, "_changed_selection", lambda: (many, []))
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    assert not any("--dist" in cmd and "load" in cmd for cmd, _ in runs.calls)
    assert len(runs.suite()) == 1
    assert any("would nearly repeat the suite" in line for line in _lines(capsys))


# ---- D-CI-615: a changed browser test file runs live ------------------------------------------

def test_a_changed_browser_test_file_runs_live_with_its_evidence_check(gate, monkeypatch):
    monkeypatch.setattr(gate, "_changed_selection",
                        lambda: ([], ["tests/test_review_backend_browser.py"]))
    checked = []
    monkeypatch.setattr(gate, "_browser_evidence_gate",
                        lambda junit, files: checked.append(list(files)) or True)
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    browser_cmd, env = runs.calls[0]
    assert browser_cmd[-1] == "tests/test_review_backend_browser.py"
    assert env["KESTREL_LIVE_BROWSER"] == "1"
    assert checked == [["tests/test_review_backend_browser.py"]]


# ---- G9: both browser stages keep the evidence check ------------------------------------------

@pytest.mark.parametrize("count", [1, 13])
def test_a_browser_file_with_no_passed_test_fails_in_either_browser_stage(gate, monkeypatch,
                                                                          capsys, count):
    files = [f"tests/test_p{n}_browser.py" for n in range(count)]
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: (files, {}, []))
    monkeypatch.setattr(gate, "_run_pytest", Runs(gate))
    monkeypatch.setattr(gate, "_browser_evidence_gate", lambda *args: False)
    assert gate.main() == 1
    assert "Ready to push." not in _lines(capsys)


# ---- G10: both coverage-writing stages append ---------------------------------------------------

def test_the_suite_appends_to_the_coverage_the_early_browser_stage_wrote(gate, monkeypatch):
    monkeypatch.setattr(gate, "_plan_diff_guards",
                        lambda: (["tests/test_nav_browser.py"], {"kestrel/a.py": {1}}, []))
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    (browser_cmd, browser_env), (suite_cmd, suite_env) = runs.calls
    assert "--cov-append" in browser_cmd
    assert "--cov=kestrel" in suite_cmd and "--cov-append" in suite_cmd
    assert browser_env["COVERAGE_FILE"] == suite_env["COVERAGE_FILE"]


# ---- G8: the record's replay never fails a gate on a stale hint --------------------------------

@pytest.mark.parametrize("code,passed", [(4, None), (5, None), (0, None), (2, None)])
def test_a_stale_replay_is_a_note_and_the_gate_goes_on(gate, monkeypatch, capsys, code, passed):
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "sha": "c" * 40, "failed": ["tests/test_phone.py::test_gone"],
         "stage": "S7 suite"}), encoding="utf-8")
    runs = Runs(gate, {"-n 0 tests/test_phone.py::test_gone": (code, passed, None)})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    out = _lines(capsys)
    assert out[-1] == "Ready to push."
    assert not any(line.startswith("Not ready to push") for line in out)
    assert any(line.startswith("==> S5 note:") for line in out)
    assert len(runs.suite()) == 1  # the whole suite still ran


def test_a_recorded_failure_that_still_fails_stops_the_gate_first(gate, monkeypatch, capsys):
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "sha": "c" * 40, "failed": ["tests/test_phone.py::test_a"],
         "stage": "S7 suite"}), encoding="utf-8")
    runs = Runs(gate, {"-n 0 tests/test_phone.py::test_a": (1, 0, "tests/test_phone.py::test_a")})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 1
    assert len(runs.calls) == 1
    out = _lines(capsys)
    assert "S5" in out[-1] and out[-1].startswith("Not ready to push")
    assert "from commit ccccccc, a hint only" in "\n".join(out)


@pytest.mark.parametrize("selected,replayed", [(True, True), (False, False)])
def test_a_recorded_browser_test_replays_live_only_when_its_file_is_selected(
        gate, monkeypatch, selected, replayed):
    test_id = "tests/test_review_backend_browser.py::test_a"
    files = ["tests/test_review_backend_browser.py"] if selected else []
    monkeypatch.setattr(gate, "_plan_diff_guards", lambda: (files, {}, []))
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "sha": "a" * 40, "failed": [test_id], "stage": "S6 browser"}),
        encoding="utf-8")
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    replays = [(cmd, env) for cmd, env in runs.calls if cmd[-1] == test_id]
    assert bool(replays) is replayed
    assert all(env.get("KESTREL_LIVE_BROWSER") == "1" for _cmd, env in replays)


# ---- G6: the record is read safely ---------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    for name in ("tests/test_a.py", "tests/sub/test_b.py", "scripts/test_x.py",
                 "tests/helper.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("text", [
    None, "", "{not json", "[]", '"tests/test_a.py::t"',
    json.dumps({"version": 2, "failed": ["tests/test_a.py::t"]}),
    json.dumps({"version": 1, "failed": "tests/test_a.py::t"}),
    json.dumps({"version": 1}),
    "[" * 5000 + "]" * 5000,              # nested past the parser's recursion limit
])
def test_a_missing_or_broken_record_replays_nothing_and_raises_nothing(gate, tree, text):
    path = tree / "rec.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    ids, note = gate.read_record(path, tree)
    assert ids == []
    assert note


@pytest.mark.parametrize("bad", [
    "tests/../scripts/test_x.py::t",      # leaves tests/ once resolved
    "scripts/../tests/test_a.py::t",      # not written as tests/..., could look like an option
    "-k evil",
    "tests/test_a.py::t\x00",
    "tests/test_a.py::t\nx",
    "tests/helper.py::t",                 # not a test_*.py file
    "tests/test_missing.py::t",
    "tests/test_a.py",                    # no test name
    "tests/test_a.py::t" + "x" * 400,     # fifty of these would pass the Windows command line
    7, None, ["tests/test_a.py::t"],
])
def test_a_hostile_or_stale_id_is_dropped_and_a_good_one_kept(gate, tree, bad):
    path = tree / "rec.json"
    good = "tests/sub/test_b.py::test_c[1]"
    path.write_text(json.dumps({"version": 1, "sha": "a" * 40, "failed": [bad, good]}),
                    encoding="utf-8")
    ids, _note = gate.read_record(path, tree)
    assert ids == [good]


def test_the_record_replays_at_most_fifty_ids(gate, tree):
    path = tree / "rec.json"
    path.write_text(json.dumps({"version": 1, "failed": [f"tests/test_a.py::t{n}"
                                                         for n in range(80)]}), encoding="utf-8")
    ids, _note = gate.read_record(path, tree)
    assert ids == [f"tests/test_a.py::t{n}" for n in range(50)]


def _refuse(*_args, **_kwargs):
    raise PermissionError("held open by another process")


def test_a_record_that_cannot_be_written_never_changes_the_result(gate, monkeypatch, tmp_path,
                                                                  capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so no folder can be made here", encoding="utf-8")
    monkeypatch.setattr(gate, "FAILFAST_RECORD", blocker / "failfast-last.json")
    runs = Runs(gate, {"worksteal": (1, 5, "tests/test_phone.py::test_a")})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 1
    out = _lines(capsys)
    assert any(line.startswith("==> fail-fast record not written") for line in out)
    assert out[-1] == "Not ready to push: fix the failure above first."
    # and a green gate still ends on its pass line when the record cannot be cleared
    monkeypatch.setattr(gate, "FAILFAST_RECORD", blocker)  # exists, so the gate tries to clear
    monkeypatch.setattr(gate, "_run_pytest", Runs(gate))
    monkeypatch.setattr(gate.os, "replace", _refuse)
    assert gate.main() == 0
    out = _lines(capsys)
    assert out[-1] == "Ready to push."
    assert any(line.startswith("==> fail-fast record not written") for line in out)


def test_only_a_whole_passing_gate_clears_the_record(gate, monkeypatch):
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    old = {"version": 1, "sha": "a" * 40, "failed": ["tests/test_phone.py::test_a"],
           "stage": "S7 suite"}
    gate.FAILFAST_RECORD.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(gate, "_run_pytest", Runs(gate))
    monkeypatch.setattr(gate, "_red_first_gate", lambda: False)  # every test stage passed
    assert gate.main() == 1
    assert json.loads(gate.FAILFAST_RECORD.read_text(encoding="utf-8")) == old
    monkeypatch.setattr(gate, "_red_first_gate", lambda: True)
    assert gate.main() == 0
    assert json.loads(gate.FAILFAST_RECORD.read_text(encoding="utf-8"))["failed"] == []


# ---- D-CI-614 and D-CI-616: --keep-going and the off switch give the old gate -------------------

@pytest.mark.parametrize("flag,env", [(["--keep-going"], None), ([], "1")])
def test_keep_going_runs_the_old_order_with_the_old_commands(gate, monkeypatch, flag, env):
    if env:
        monkeypatch.setenv("KESTREL_KEEP_GOING", env)
    monkeypatch.setattr(gate.sys, "argv", ["check.py", "--workers", "8", *flag])
    monkeypatch.setattr(gate, "_plan_diff_guards",
                        lambda: (["tests/test_nav_browser.py"], {}, []))
    selected = []
    monkeypatch.setattr(gate, "_changed_selection",
                        lambda: selected.append(1) or (["tests/test_phone.py"],
                                                       ["tests/test_x_browser.py"]))
    gate.FAILFAST_RECORD.parent.mkdir(parents=True)
    gate.FAILFAST_RECORD.write_text(json.dumps(
        {"version": 1, "failed": ["tests/test_phone.py::test_a"]}), encoding="utf-8")
    runs = Runs(gate)
    order = []
    monkeypatch.setattr(gate, "_stress_gate", lambda *a: order.append("stress") or True)
    monkeypatch.setattr(gate, "_run_pytest",
                        lambda cmd, extra_env=None: order.append(
                            "browser" if cmd[-1].endswith("_browser.py") else "suite")
                        or runs(cmd, extra_env))
    assert gate.main() == 0
    assert selected == []
    assert order == ["suite", "stress", "browser"]  # the old order (D-CI-614)
    (suite_cmd, _), (browser_cmd, _) = runs.calls
    assert suite_cmd[2:] == ["pytest", "-q", "-rs", "-p", "no:cacheprovider", "-n", "8",
                             "--dist", "worksteal", "--max-worker-restart", "0",
                             "--cov=kestrel", "--cov-report="]
    assert browser_cmd[2:] == ["pytest", "-q", "-rs", "-p", "no:cacheprovider", "-n",
                               str(gate.browser_worker_count("auto")), "--dist", "worksteal",
                               "--max-worker-restart", "0", "--cov=kestrel", "--cov-append",
                               "--cov-report=", "--junitxml", browser_cmd[-2],
                               "tests/test_nav_browser.py"]


def test_keep_going_lists_every_failure_of_the_suite_without_the_plugin(gate, monkeypatch,
                                                                        capsys):
    monkeypatch.setattr(gate.sys, "argv", ["check.py", "--keep-going"])
    runs = Runs(gate, {"worksteal": (1, 5, None)})
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 1
    assert not any(OPTION in cmd for cmd, _ in runs.calls)
    assert _lines(capsys)[-1] == "Not ready to push: fix the failure above first."


@pytest.mark.parametrize("value,expected", [("1", True), ("", False), ("0", False),
                                            ("yes", False)])
def test_only_the_value_one_turns_fail_fast_off(gate, value, expected):
    args = argparse.Namespace(keep_going=False)
    assert gate.keep_going(args, {"KESTREL_KEEP_GOING": value}) is expected
    assert gate.keep_going(argparse.Namespace(keep_going=True), {}) is True


@pytest.mark.parametrize("tier", ["--quick", "--fast"])
def test_light_tiers_stop_at_the_first_failure_too_unless_told_to_keep_going(gate, monkeypatch,
                                                                             capsys, tier):
    monkeypatch.setattr(gate.sys, "argv", ["check.py", tier])
    runs = Runs(gate)
    monkeypatch.setattr(gate, "_run_pytest", runs)
    assert gate.main() == 0
    assert OPTION in runs.calls[0][0]
    monkeypatch.setattr(gate.sys, "argv", ["check.py", tier, "--keep-going"])
    assert gate.main() == 0
    assert OPTION not in runs.calls[1][0]
    capsys.readouterr()
    # a first-failure file fails a light tier even when pytest exits 0 (G5)
    monkeypatch.setattr(gate.sys, "argv", ["check.py", tier])
    runs.results = {"-rs": (0, 5, "tests/test_phone.py::test_a")}
    assert gate.main() == 1
    out = _lines(capsys)
    assert out[-1] == "Not ready to push: fix the failure above first."
    assert not any("passed; run the full check" in line for line in out)


# ---- the gate's readers still find their exact last line ----------------------------------------

def test_a_passing_fail_fast_gate_still_ends_on_the_exact_pass_line(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_plan_diff_guards",
                        lambda: (["tests/test_nav_browser.py"], {}, []))
    monkeypatch.setattr(gate, "_changed_selection", lambda: (["tests/test_phone.py"], []))
    monkeypatch.setattr(gate, "_run_pytest", Runs(gate))
    assert gate.main() == 0
    assert _lines(capsys)[-1] == gate.PASS_LINES["full"] == "Ready to push."
