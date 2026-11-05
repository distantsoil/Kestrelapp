"""scripts/ci_status.py: every CI result collected before fixing anything. gh is mocked."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_status_under_test", Path(__file__).resolve().parents[1] / "scripts" / "ci_status.py"
)
ci = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci)

REPO = "example/app"
SHA = "abc1234"


def run(name, conclusion="success", status="completed", slug="github-actions", id_=1, output=None):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion,
            "app": {"slug": slug}, "output": output or {}}


class FakeGh:
    """Stands in for subprocess.run: answers `gh api <path>` from a table of responses."""

    def __init__(self, responses):
        self.responses = responses  # path -> str | int (exit code) | Exception | list of those
        self.calls: list[str] = []
        self.cmds: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kw):
        assert cmd[:2] == ["gh", "api"]
        path = cmd[-1]
        self.calls.append(path)
        self.cmds.append(cmd)
        self.kwargs.append(kw)
        value = self.responses[path]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return subprocess.CompletedProcess(cmd, value, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, value, "")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(ci, "_sleep", lambda s: None)


def checks(*runs):
    return json.dumps({"check_runs": list(runs)})


CHECKS_PATH = f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100"


def invoke(monkeypatch, capsys, responses, *args):
    if "--require" not in args:
        args = (*args, "--require", "none")
    return invoke_raw(monkeypatch, capsys, responses, *args)


def invoke_raw(monkeypatch, capsys, responses, *args):
    gh = FakeGh(responses)
    monkeypatch.setattr(ci.subprocess, "run", gh)
    rc = ci.main([REPO, SHA, *args])
    return rc, capsys.readouterr().out.strip().splitlines(), gh


def test_all_green_prints_each_check_and_a_green_last_line(monkeypatch, capsys):
    rc, out, _ = invoke(monkeypatch, capsys, {CHECKS_PATH: checks(
        run("ubuntu", id_=1), run("windows", id_=2), run("CodeQL", "neutral", slug="x", id_=3))})
    assert rc == 0
    assert out == ["ubuntu: success", "windows: success", "CodeQL: neutral", "CI GREEN: 3 checks"]


def test_failed_job_prints_failed_and_count_lines_without_timestamps(monkeypatch, capsys):
    log = "\n".join([
        "2026-11-05T10:00:00.1234567Z collecting ...",
        "2026-11-05T10:00:01.0000000Z \x1b[31mFAILED tests/test_a.py::test_x - assert 1 == 2"
        "\x1b[0m",
        "2026-11-05T10:00:02.0000000Z ===== 1 failed, 40 passed in 3.2s =====",
    ])
    rc, out, gh = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("ubuntu", "failure", id_=77), run("lint", id_=78)),
        f"repos/{REPO}/actions/jobs/77/logs": log,
    })
    assert rc == 1
    assert "  FAILED tests/test_a.py::test_x - assert 1 == 2" in out
    assert "  ===== 1 failed, 40 passed in 3.2s =====" in out
    assert not any("2026-11-05" in line for line in out)
    assert out[-1] == "CI RED: 1 failed: ubuntu"
    assert f"repos/{REPO}/actions/jobs/78/logs" not in gh.calls  # passing jobs are not read


def test_every_failure_is_listed_not_only_the_first(monkeypatch, capsys):
    rc, out, _ = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("ubuntu", "failure", id_=1), run("windows", "timed_out", id_=2)),
        f"repos/{REPO}/actions/jobs/1/logs": "FAILED a\n",
        f"repos/{REPO}/actions/jobs/2/logs": "FAILED b\n",
    })
    assert rc == 1 and "  FAILED a" in out and "  FAILED b" in out
    assert out[-1] == "CI RED: 2 failed: ubuntu, windows"


def test_codeql_failure_lists_alert_titles_from_the_output(monkeypatch, capsys):
    output = {"title": "2 new alerts", "summary": "Found:\n- Unsafe script tag\n- Bad regex\nend",
              "text": "no bullets here"}
    rc, out, gh = invoke(monkeypatch, capsys, {CHECKS_PATH: checks(
        run("CodeQL", "failure", slug="github-code-scanning", id_=9, output=output))})
    assert rc == 1
    assert "  2 new alerts" in out and "  - Unsafe script tag" in out and "  - Bad regex" in out
    assert out[-1] == "CI RED: 1 failed: CodeQL"
    assert all("/logs" not in c for c in gh.calls)


def test_a_log_with_no_failed_lines_falls_back_to_its_tail(monkeypatch, capsys):
    log = "\n".join(f"2026-11-05T10:00:00Z line {i}" for i in range(40))
    rc, out, _ = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("build", "failure", id_=5)),
        f"repos/{REPO}/actions/jobs/5/logs": log})
    assert rc == 1 and "  line 39" in out and "  line 0" not in out


def test_unreadable_job_log_is_reported_and_still_red(monkeypatch, capsys):
    rc, out, _ = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("build", "failure", id_=5)),
        f"repos/{REPO}/actions/jobs/5/logs": 1})
    assert rc == 1 and any("log unavailable" in line for line in out)
    assert out[-1] == "CI RED: 1 failed: build"


def test_wait_polls_until_nothing_is_unfinished(monkeypatch, capsys):
    sleeps: list[int] = []
    monkeypatch.setattr(ci, "_sleep", sleeps.append)
    monkeypatch.setattr(ci, "_monotonic", lambda: 0.0)
    rc, out, gh = invoke(monkeypatch, capsys, {CHECKS_PATH: [
        checks(run("ubuntu", None, "in_progress"), run("lint")),
        checks(run("ubuntu", None, "queued"), run("lint")),
        checks(run("ubuntu"), run("lint"))]}, "--wait")
    assert rc == 0 and sleeps == [60, 60] and gh.calls.count(CHECKS_PATH) == 3
    assert out[-1] == "CI GREEN: 2 checks"


def test_without_wait_an_unfinished_check_is_not_green(monkeypatch, capsys):
    rc, out, gh = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("ubuntu", None, "in_progress"))})
    assert rc == 1 and gh.calls == [CHECKS_PATH]
    assert out[-1] == "CI RED: 1 failed: ubuntu (in_progress)"


def test_wait_gives_up_at_the_timeout(monkeypatch, capsys):
    clock = iter([0.0, 10.0, 100.0])
    monkeypatch.setattr(ci, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(ci, "_sleep", lambda s: None)
    rc, out, _ = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("ubuntu", None, "in_progress"))}, "--wait", "--timeout", "50")
    assert rc == 1 and out[-1] == "CI RED: 1 failed: ubuntu (in_progress)"


def test_no_checks_at_all_is_red(monkeypatch, capsys):
    rc, out, _ = invoke(monkeypatch, capsys, {CHECKS_PATH: checks()})
    assert rc == 1 and out[-1].startswith("CI RED: 0 checks")


def test_gh_failure_is_red_with_the_reason(monkeypatch, capsys):
    rc, out, _ = invoke(monkeypatch, capsys, {CHECKS_PATH: 1})
    assert rc == 1 and out[-1].startswith("CI RED: 1 failed: ci_status")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_every_non_passing_conclusion_is_red(monkeypatch, capsys, conclusion):
    rc, out, _ = invoke(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("job", conclusion, id_=3)),
        f"repos/{REPO}/actions/jobs/3/logs": "FAILED x\n"})
    assert rc == 1 and out[-1] == "CI RED: 1 failed: job"


CODEQL = dict(name="CodeQL", slug="github-code-scanning")
CQ = run(id_=99, **CODEQL)


def test_green_waits_until_the_required_codeql_check_exists(monkeypatch, capsys):
    sleeps: list[int] = []
    monkeypatch.setattr(ci, "_sleep", sleeps.append)
    monkeypatch.setattr(ci, "_monotonic", lambda: 0.0)
    rc, out, gh = invoke(monkeypatch, capsys, {CHECKS_PATH: [
        checks(run("ubuntu"), run("Analyze (python)", id_=2)),
        checks(run("ubuntu"), run("Analyze (python)", id_=2), run(id_=3, **CODEQL))]},
        "--wait", "--require", "CodeQL")
    assert rc == 0 and sleeps == [60]
    assert out[-1] == "CI GREEN: 3 checks"
    assert any("CodeQL (not reported yet)" in line for line in out)


def test_codeql_is_required_by_default(monkeypatch, capsys):
    gh = FakeGh({CHECKS_PATH: checks(run("ubuntu"))})
    monkeypatch.setattr(ci.subprocess, "run", gh)
    rc = ci.main([REPO, SHA])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1 and out[-1] == "CI RED: 1 failed: CodeQL (missing)"


def test_a_required_check_that_never_appears_is_red_at_the_timeout(monkeypatch, capsys):
    clock = iter([0.0, 10.0, 100.0])
    monkeypatch.setattr(ci, "_monotonic", lambda: next(clock))
    rc, out, _ = invoke(monkeypatch, capsys, {CHECKS_PATH: checks(run("ubuntu"))},
                        "--wait", "--timeout", "50", "--require", "CodeQL")
    assert rc == 1 and out[-1] == "CI RED: 1 failed: CodeQL (missing)"


def test_a_neutral_codeql_check_with_alerts_lists_them(monkeypatch, capsys):
    output = {"title": "1 new alert", "summary": "- Reflected XSS in the about page"}
    rc, out, _ = invoke(monkeypatch, capsys, {CHECKS_PATH: checks(
        run("CodeQL", "neutral", slug="github-code-scanning", id_=9, output=output))})
    assert "  - Reflected XSS in the about page" in out
    assert out[-1] == "CI GREEN: 1 checks" and rc == 0


def test_a_transient_gh_failure_is_retried(monkeypatch, capsys):
    sleeps: list[int] = []
    monkeypatch.setattr(ci, "_sleep", sleeps.append)
    rc, out, gh = invoke_raw(monkeypatch, capsys, {CHECKS_PATH: [1, checks(run("ubuntu"), CQ)]})
    assert rc == 0 and out[-1] == "CI GREEN: 2 checks"
    assert gh.calls.count(CHECKS_PATH) == 2 and sleeps == [5]


def test_gh_is_given_up_on_after_a_few_attempts(monkeypatch, capsys):
    rc, out, gh = invoke_raw(monkeypatch, capsys, {CHECKS_PATH: 1})
    assert rc == 1 and gh.calls.count(CHECKS_PATH) == 3
    assert out[-1].startswith("CI RED: 1 failed: ci_status")


def test_a_hung_gh_call_times_out_and_is_retried(monkeypatch, capsys):
    hang = subprocess.TimeoutExpired(["gh"], 60)
    rc, out, gh = invoke_raw(monkeypatch, capsys,
                             {CHECKS_PATH: [hang, checks(run("ubuntu"), CQ)]})
    assert rc == 0 and out[-1] == "CI GREEN: 2 checks"


def test_every_gh_call_has_a_timeout(monkeypatch, capsys):
    _rc, _out, gh = invoke_raw(monkeypatch, capsys, {
        CHECKS_PATH: checks(run("ubuntu", "failure", id_=4), CQ),
        f"repos/{REPO}/actions/jobs/4/logs": "FAILED x\n"})
    assert len(gh.kwargs) == 2 and all(kw.get("timeout") for kw in gh.kwargs)


def test_timeout_bounds_the_whole_wait_even_when_calls_hang(monkeypatch, capsys):
    now = [0.0]
    monkeypatch.setattr(ci, "_monotonic", lambda: now[0])
    monkeypatch.setattr(ci, "_sleep", lambda s: now.__setitem__(0, now[0] + s))

    class Hanging(FakeGh):
        def __call__(self, cmd, **kw):
            now[0] += kw["timeout"]
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    gh = Hanging({})
    monkeypatch.setattr(ci.subprocess, "run", gh)
    rc = ci.main([REPO, SHA, "--wait", "--timeout", "100"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1 and out[-1].startswith("CI RED: 1 failed: ci_status")
    assert now[0] <= 100 + 15


def test_check_runs_are_paginated(monkeypatch, capsys):
    page1 = checks(run("ubuntu", id_=1))
    page2 = checks(run("windows", id_=2), CQ)
    rc, out, gh = invoke_raw(monkeypatch, capsys, {CHECKS_PATH: page1 + "\n" + page2})
    assert "--paginate" in gh.cmds[0]
    assert out[-1] == "CI GREEN: 3 checks" and rc == 0
