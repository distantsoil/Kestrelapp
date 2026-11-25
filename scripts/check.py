"""Run what CI runs, locally, before a push (D-CI-2).

GitHub Actions minutes are metered (Windows 2x, macOS 10x), so CI confirms rather than
discovers: lint, the full test suite (which includes the workflow, installer-script and
third-party-notice guards), in that order, stopping at the first failure.

    uv run --extra dev python scripts/check.py          # everything, plus the midnight run
    uv run --extra dev python scripts/check.py --quick  # lint, packaging guards, static checks
    uv run --extra dev python scripts/check.py --fast   # quick plus the whole suite, no coverage,
                                                        # no browser stage (slow PCs, D-TEST-8)

The full gate also (D-TEST-7): runs the browser test files mapped from the files this branch
changed (tests/browser_map.toml; a UI file that maps to nothing fails), and fails when fewer than
80 percent of the lines changed under kestrel/ are executed by a test. --quick only checks the
map is complete for the changed files and prints which browser files would run.

Every tier also runs the D-TEST-8 static checks on what the branch changed (scripts/static_checks.py
and scripts/test_floor.py): new pyright errors, ruff BLE001/S110/ASYNC/RUF006 on changed lines, the
assertion floor, protected paths and the test-quality lint. The full gate adds the red-first proof
for `fix:` commits. Each failure names the rule, the file:line and the fix.
    uv run --extra dev python scripts/check.py --workers 0  # serial comparison

Fail-fast order (D-CI-610 to D-CI-616): the full gate runs the tests this branch's change selects
first (S4), then the failures the last gate recorded (S5), then up to 12 selected browser files
(S6), then the whole suite exactly as before (S7), the stress run (S8), more than 12 browser files
(S9), diff coverage (S10) and the red-first proof (S11). Each test stage stops at its first
failure and names it. S4 to S6 repeat tests S7 runs anyway, so nothing is skipped or moved out.
    uv run --extra dev python scripts/check.py --keep-going  # the old order, every test run to
                                                             # its end (also KESTREL_KEEP_GOING=1)

Windows-only behavior still needs the Windows CI job; everything else should be green here
before anything is pushed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_gate  # noqa: E402  (sits beside this script; the path is added just above)
import error_code_base  # noqa: E402  (same folder)
import static_checks  # noqa: E402  (same folder)
import stress  # noqa: E402  (same folder)
import test_floor  # noqa: E402  (same folder)

QUICK_TESTS = ["tests/test_packaging.py", "tests/test_notices.py"]


# CI's environment (ci.yml `env:`); without it a desktop session takes the tray/browser paths.
CI_ENV = {"KESTREL_HEADLESS": "1"}
# The full gate also runs the clock-boundary run (D-TEST-3, #116): the "today" and "this month"
# modules again with the clock frozen just after midnight. --quick and CI leave it off.
FULL_GATE_ENV = {"KESTREL_BOUNDARY": "1"}

# Fail-fast (D-CI-610 to D-CI-616). The plugin stops a stage at its first failure, under xdist
# too (D-CI-611); it is a pytest option so a child pytest never inherits it.
FAILFAST_PLUGIN = ("-p", "tests.failfast_plugin")
FAILFAST_OPTION = "--kestrel-failfast-file"
# The off switch for a gate started by gate.py or a hook, which cannot pass --keep-going
# (D-CI-614, D-CI-616): "1" gives the old order and the old commands.
KEEP_GOING_ENV = "KESTREL_KEEP_GOING"
# Selected browser files run before the suite when there are this many or fewer (D-CI-613):
# measured at about 27 s a file on four workers, so 12 files is about five minutes.
EARLY_BROWSER_MAX = 12
# S4 repeats tests S7 runs anyway; past this many files it would nearly repeat the suite, so it
# steps aside and S7 (fail-fast too) finds the failure (D-CI-610).
CHANGED_TESTS_MAX = 60
# The last gate's first failure, replayed first next time (D-CI-612). Git-ignored, beside gate.py's
# records but under its own name, so gate.py and push_gated.py never read it.
FAILFAST_RECORD = diff_gate.ROOT / ".gate" / "failfast-last.json"
RECORD_VERSION = 1
RECORD_MAX_IDS = 50
# Fifty IDs of this length stay far below Windows' 32,767-character command line.
RECORD_MAX_ID_CHARS = 400


def _run(label: str, cmd: list[str]) -> bool:
    print(f"==> {label}: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    ok = subprocess.run(
        cmd, check=False, env={**os.environ, **CI_ENV}, cwd=diff_gate.ROOT
    ).returncode == 0
    print(f"    {'ok' if ok else 'FAILED'} ({time.monotonic() - started:.0f}s)", flush=True)
    return ok


SKIP_LINE = re.compile(r"^SKIPPED \[(\d+)\] (.+?): (.*)$")
PASSED_COUNT = re.compile(r"\b(\d+) passed\b")
# How many tests the last _run_pytest saw pass (None: its summary line named none), so a run that
# executed nothing, or only skipped, is never counted as a pass (D-CI-39); and its exit code, which
# S5 reads (D-CI-612).
LAST_RUN: dict[str, int | None] = {"passed": None, "exit": None}
BROWSER_WORDS = ("browser", "playwright", "edge", "chromium")


def worker_count(value: str) -> int:
    if value == "auto":
        return max(1, min(8, (os.cpu_count() or 1) // 2))
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be auto or a nonnegative integer") from exc
    if count < 0:
        raise argparse.ArgumentTypeError("workers must be auto or a nonnegative integer")
    return count


BROWSER_WORKER_CAP = 4


CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")


def _quota_cpus(quota: int, period: int) -> int | None:
    return max(1, -(-quota // period)) if quota > 0 and period > 0 else None


def _v2_limit(folder: Path) -> int | None:
    parts = (folder / "cpu.max").read_text(encoding="utf-8").split()
    return _quota_cpus(int(parts[0]), int(parts[1])) if len(parts) == 2 and parts[0] != "max" \
        else None


def _v1_limit(folder: Path) -> int | None:
    return _quota_cpus(int((folder / "cpu.cfs_quota_us").read_text(encoding="utf-8")),
                       int((folder / "cpu.cfs_period_us").read_text(encoding="utf-8")))


def _cgroup_folders(root: Path) -> list[tuple[str, Path]]:
    """This process's own cgroup folders from /proc/self/cgroup, each followed by its ancestors
    up to the mount (a quota set on a parent limits the child too; D-CI-594). Without a cgroup
    namespace the own folder is a path below the mount, not the mount itself. When the file
    cannot be read, the mounts alone, as before."""
    try:
        lines = PROC_CGROUP.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out: list[tuple[str, Path]] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hier, controllers, rel = fields
        if hier == "0" and controllers == "":
            kind, mount = "v2", root
        elif "cpu" in controllers.split(","):
            kind, mount = "v1", root / "cpu"
        else:
            continue
        folder = mount / rel.lstrip("/")
        while True:
            out.append((kind, folder))
            if folder == mount or mount not in folder.parents:
                break
            folder = folder.parent
    return out or [("v2", root), ("v1", root / "cpu")]


def _cgroup_cpu_limit(root: Path | None = None) -> int | None:
    """The whole CPUs a cgroup quota allows this process, rounded up, or None with no quota
    (D-CI-590): the tightest quota on its own cgroup or any ancestor (D-CI-594). Formats from
    the kernel's cgroup documentation: v2 `cpu.max` is "$MAX $PERIOD" with MAX "max" for no
    limit; v1 `cpu.cfs_quota_us` is -1 for no limit, beside `cpu.cfs_period_us`. A file that
    is missing or unreadable is no quota, never a guess."""
    root = CGROUP_ROOT if root is None else root
    limits: list[int] = []
    for kind, folder in _cgroup_folders(root):
        try:
            limit = _v2_limit(folder) if kind == "v2" else _v1_limit(folder)
        except (OSError, ValueError, IndexError):
            continue
        if limit:
            limits.append(limit)
    return min(limits) if limits else None


def _usable_cpus() -> int:
    """The CPUs this process may use: its affinity, capped by a cgroup quota (D-CI-590); where
    the platform cannot say (no affinity call, or the call fails), the machine's count; never
    less than 1 (D-CI-38)."""
    try:
        usable = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else 0
    except OSError:
        usable = 0
    usable = usable or os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    return min(usable, limit) if limit else usable


def browser_worker_count(value: str) -> int:
    """`auto` is one worker per CPU up to BROWSER_WORKER_CAP (D-CI-35): a browser test is CPU
    bound (about 2 CPU-seconds each, half of it Chromium), so more workers than CPUs only slow
    every page toward the 30 s waits; an explicit number is taken as given."""
    if value == "auto":
        return max(1, min(BROWSER_WORKER_CAP, _usable_cpus()))
    return worker_count(value)


def _coverage_run_env(cov_env: dict[str, str]) -> dict[str, str]:
    """The environment of every stage that adds to the diff-coverage data (S6, S7, S9): one data
    file, and line coverage through sys.monitoring on Python 3.12+. The default tracer loses the
    lines that run after SQLAlchemy's async greenlet bridge, so tested lines read as missed
    (D-GATE-940); it also slowed the browser stage by about a fifth (D-CI-35)."""
    tracer = {"COVERAGE_CORE": "sysmon"} if sys.version_info >= (3, 12) else {}
    return {**cov_env, **tracer}


def _run_pytest(
    cmd: list[str], *, extra_env: dict[str, str] | None = None
) -> tuple[bool, list[tuple[int, str, str]]]:
    """Run pytest with -rs, echo its output, and return (ok, [(count, where, reason)])."""
    print(f"==> pytest: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, env={**os.environ, **CI_ENV, **(extra_env or {})}, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=diff_gate.ROOT,
    )
    skips: list[tuple[int, str, str]] = []
    LAST_RUN["passed"] = None
    LAST_RUN["exit"] = None
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        if counted := PASSED_COUNT.search(line):
            LAST_RUN["passed"] = int(counted.group(1))
        if m := SKIP_LINE.match(line.rstrip()):
            skips.append((int(m.group(1)), m.group(2), m.group(3)))
    code = proc.wait()
    LAST_RUN["exit"] = code
    ok = code == 0
    print(f"    {'ok' if ok else 'FAILED'} ({time.monotonic() - started:.0f}s)", flush=True)
    return ok, skips


def _report_skips(skips: list[tuple[int, str, str]]) -> None:
    """A skip is never silent: total, reasons, and a WARNING when browser tests did not run
    (the stale-assertion CI failure that Edge-less local runs used to hide)."""
    total = sum(n for n, _, _ in skips)
    print(f"Skipped tests: {total}")
    reasons: dict[str, int] = {}
    for n, _where, reason in skips:
        reasons[reason] = reasons.get(reason, 0) + n
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {reason[:140]}")
    browser = [(n, w, r) for n, w, r in skips if any(k in r.lower() for k in BROWSER_WORDS)
               and "KESTREL_LIVE_BROWSER" not in r]
    if browser:
        count = sum(n for n, _, _ in browser)
        print(f"WARNING: {count} browser test(s) were SKIPPED, so the UI/PDF gates did not run "
              "here. Install Edge, run `uv run playwright install chromium`, or set "
              "KESTREL_BROWSER_PATH, then re-run before pushing:")
        for _n, where, reason in browser:
            print(f"  - {where}: {reason[:120]}")


def _plan_diff_guards() -> tuple[list[str], dict[str, set[int]], list[str]] | None:
    """Work out the branch's changes once. Returns (browser test files to run, changed lines,
    unmapped UI files), or None when git has no base to diff against."""
    base, note = diff_gate.diff_base()
    if base is None:
        print(f"==> diff guards: {note}")
        return None
    print(f"==> diff guards: {note}")
    names, lines = diff_gate.git_changes(base)
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
        cwd=diff_gate.ROOT,
    ).stdout
    if problem := diff_gate.empty_diff_problem(names, lines, dirty):
        print(f"==> diff guards: {problem}")
        return None
    if not names and not lines:
        print("==> diff guards: NOTHING TO COMPARE: no file differs from the base, so no browser "
              "file was selected and no line was measured")
    browser, unmapped = diff_gate.select_browser_tests(names, diff_gate.load_map())
    print(f"==> diff base {base[:10]}: {len(names)} changed file(s), "
          f"{len(browser)} browser test file(s) selected")
    return browser, lines, unmapped


def _report_unmapped(unmapped: list[str]) -> None:
    print("Not ready to push: these changed UI files map to no browser test file. Add them to "
          "tests/browser_map.toml:")
    for path in unmapped:
        print(f"  - {path}")


def _diff_coverage_gate(cov_json: Path, lines: dict[str, set[int]]) -> bool:
    cov = json.loads(cov_json.read_text(encoding="utf-8"))
    notes: list[str] = []
    pct, total, covered, uncovered = diff_gate.diff_coverage(lines, cov, notes=notes)
    for note in notes:
        print(f"==> diff coverage note: {note}")
    if pct is None:
        print("Diff coverage: no Python lines changed under kestrel/ (nothing to measure)")
        return True
    ok = pct >= diff_gate.DIFF_COVERAGE_MIN
    print(f"==> diff coverage: {covered}/{total} changed lines executed ({pct:.1f}%), "
          f"minimum {diff_gate.DIFF_COVERAGE_MIN:.0f}% {'ok' if ok else 'FAILED'}")
    if not ok:
        print("Changed lines no test executed:")
        for path, nums in uncovered.items():
            print(f"  {path}: {', '.join(map(str, nums))}")
    return ok


def _browser_evidence_gate(junit: Path, files: list[str]) -> bool:
    """Every selected browser file must have at least one PASSED test (D-TEST-7). Reads the junit
    XML of the live browser run; an unreadable report is a failure, never a pass."""
    try:
        problems = diff_gate.browser_evidence(junit.read_text(encoding="utf-8"), files)
    except OSError as exc:
        print(f"Not ready to push: the browser test report could not be read ({exc}).")
        return False
    except SyntaxError as exc:
        print(f"Not ready to push: the browser test report is not valid XML ({exc}).")
        return False
    for path, problem in sorted(problems.items()):
        print(f"Not ready to push: {path}: {problem}. Install Edge or Chromium, or set "
              "KESTREL_BROWSER_PATH, and re-run.")
    return not problems


def _coverage_step(
    py: str, cov_env: dict[str, str], scratch: Path, lines: dict[str, set[int]]
) -> bool:
    """Write the JSON report from the combined coverage data and apply the diff threshold."""
    cov_json = scratch / "coverage.json"
    report = subprocess.run(
        [py, "-m", "coverage", "json", "-q", "-o", str(cov_json)],
        env={**os.environ, **cov_env}, check=False, cwd=diff_gate.ROOT,
    )
    if report.returncode != 0 or not cov_json.is_file():
        print("Not ready to push: no coverage data was written.")
        return False
    if not _diff_coverage_gate(cov_json, lines):
        print("Not ready to push: add tests that run the changed lines above.")
        return False
    return True


def _static_gates() -> bool:
    """The D-TEST-8 checks on this branch's changes (all tiers). Prints every problem, not only
    the first, so one run shows the whole list."""
    base, note = diff_gate.diff_base()
    if base is None:
        print(f"Not ready to push: the static checks cannot run, {note}. Run "
              "`git fetch origin main`.")
        return False
    print("==> static checks (pyright on changed files, changed-line lint, test floor, "
          "protected paths, test quality)", flush=True)
    names, lines = diff_gate.git_changes(base)
    problems: list[str] = []
    warnings: list[str] = []
    try:
        problems += static_checks.type_check_changed(names)
        problems += static_checks.lint_changed(lines)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        problems.append(f"RULE static checks: could not run ({exc}). Fix: "
                        "`uv sync --extra dev`, then re-run.")
    more_problems, more_warnings = test_floor.run_static(base, lines, names)
    problems += more_problems
    problems += error_code_base.problems(base)  # codes stay permanent across branches (D-ERR-6)
    warnings += more_warnings
    for line in [*warnings, *problems]:
        print(line)
    if problems:
        print(f"Not ready to push: {len(problems)} static check problem(s) above. Each names the "
              "rule and the fix.")
    return not problems


def _red_first_gate() -> bool:
    base, note = diff_gate.diff_base()
    if base is None:
        print(f"Not ready to push: the red-first proof cannot run, {note}. Run "
              "`git fetch origin main`.")
        return False
    print("==> red-first proof for fix: commits (D-TEST-6)", flush=True)
    problems, warnings = test_floor.red_first_problems(base)
    for line in [*warnings, *problems]:
        print(line)
    if problems:
        print(f"Not ready to push: {len(problems)} fix commit(s) without a failing test.")
    return not problems


def _export_lint(py: str) -> bool:
    """Export the working tree the way the public sync does and lint the result (D-CI-27), so a
    pattern that only breaks after the private-to-public rewrites (ledger row 9) fails here, not
    in the public gate. Public-only files come from the public repo verbatim and are skipped.
    The public tree has no publicize.py: it is the export, which the ruff step already linted."""
    if not (diff_gate.ROOT / "scripts" / "publicize.py").is_file():
        print("==> export lint: this tree is the public export; the ruff step above linted it")
        return True
    print("==> export lint (publicize the working tree, then ruff on the export)", flush=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kestrel-export-") as scratch:
        out = Path(scratch) / "export"
        proc = subprocess.run(
            [py, str(diff_gate.ROOT / "scripts" / "publicize.py"), str(diff_gate.ROOT), str(out),
             "--private-repo", str(diff_gate.ROOT), "--lint-only"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            cwd=diff_gate.ROOT,
        )
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr).strip())
            print("Not ready to push: the public export failed; add a sanitizer rule in "
                  "scripts/publicize.py, never edit the public tree.")
            return False
        lint = subprocess.run(
            [py, "-m", "ruff", "check", "--no-cache", str(out)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, cwd=diff_gate.ROOT,
        )
        if lint.returncode != 0:
            print(lint.stdout.replace(str(out) + os.sep, "").strip())
            print("Not ready to push: lint fails on the public export (the rewrites above change "
                  "these lines); fix the private source or the rewrite rule.")
            return False
    print(f"    ok ({time.monotonic() - started:.0f}s)", flush=True)
    return True


def _stress_gate(py: str, alone: bool = False) -> bool:
    """The stress run over the tests this branch affects (D-CI-26, scripts/stress.py). Run alone
    (`--stress`), KESTREL_DIFF_BASE may name a nearer base, so a child or a merge stresses what it
    changed and not everything since main (D-PROC-15). Inside the full gate the base is main."""
    override = base_override(alone)
    base, note = diff_gate.diff_base(override) if override else diff_gate.diff_base()
    if base is None:
        print(f"Not ready to push: the stress run needs a git base ({note}).")
        return False
    if override:
        print(f"==> stress: {note}", flush=True)
    names, _lines = diff_gate.git_changes(base)
    texts = stress.read_test_texts(diff_gate.ROOT)
    files = stress.affected_test_files(names, texts, stress.read_texts("kestrel", diff_gate.ROOT))
    runs = stress.plan(files, stress.clock_seeding(files, texts),
                       now=datetime.now(UTC))
    if not runs:
        print("==> stress run: no affected test files")
        return True
    print(f"==> stress run: {len(files)} affected test file(s); budget "
          f"{stress.BUDGET_S // 60} min for repeats", flush=True)
    started = time.monotonic()
    longest_s = 0.0  # the longest run over every affected file so far (D-CI-591)
    orders = 0
    failures: list[tuple[str, str]] = []
    for run in runs:
        if run.repeat and not stress.budget_allows(time.monotonic() - started, longest_s):
            print(f"==> stress: {orders} of {stress.REPEATS} random orders ran; the longest full "
                  f"run took {longest_s:.0f}s, so another would pass the "
                  f"{stress.BUDGET_S // 60} min budget (D-CI-29)", flush=True)
            break
        print(f"==> stress: {run.label}", flush=True)
        run_started = time.monotonic()
        if run.hold_port:
            with stress.PortHolder() as held:
                if held.already_taken:
                    print(f"    port {held.port} is already in use (held by another process)")
                ok, _skips = _run_pytest(stress.pytest_args(py, run), extra_env=run.env)
        else:
            ok, _skips = _run_pytest(stress.pytest_args(py, run), extra_env=run.env)
        replay = None
        if not ok:
            # Keep going: every condition runs, so one gate finds every failure (D-CI-34).
            env = " ".join(f"{k}={v}" for k, v in run.env.items())
            replay = f"{env} {' '.join(stress.pytest_args('python', run))}".strip()
            failures.append((run.label, replay))
        elif not LAST_RUN["passed"]:
            # Exit code 0 with no test passed is a condition that tested nothing (D-CI-39).
            failures.append((f"{run.label} (ran no test that passed)",
                             " ".join(stress.pytest_args("python", run))))
        if "random order" in run.label:
            orders += 1
        if run.files == files:
            # Not the first order alone: one that failed in seconds (a collection error) would
            # make a full repeat look cheap and run every repeat past the budget (D-CI-591).
            longest_s = max(longest_s, time.monotonic() - run_started)
    if failures:
        print(f"==> stress: {len(failures)} of the conditions that ran failed; every one is "
              "listed here with its own Replay line", flush=True)
        for label, replay in failures:
            print(f"  FAILED: {label}\n  Replay: {replay}")
        names = "; ".join(label for label, _ in failures)
        print(f"Not ready to push: the stress run failed ({len(failures)} condition(s): {names}). "
              "A test that passes alone but fails here is order-, port- or clock-dependent; "
              "fix its cause.")
        return False
    print(f"    stress run ok ({time.monotonic() - started:.0f}s, {orders} random order(s))",
          flush=True)
    return True


def base_override(alone: bool, environ: dict[str, str] | None = None) -> str | None:
    """KESTREL_DIFF_BASE for the stress tier run alone; None, with a printed note when the
    variable is set, for every other tier (they always compare with main)."""
    value = (os.environ if environ is None else environ).get(diff_gate.BASE_ENV, "").strip()
    if not value:
        return None
    if not alone:
        print(f"==> {diff_gate.BASE_ENV}={value} is ignored by this tier; it compares with main "
              "(only --stress honors it)", flush=True)
        return None
    return value


# ---- fail-fast (D-CI-610 to D-CI-616) ----------------------------------------------------------


def keep_going(args: argparse.Namespace, environ: dict[str, str] | None = None) -> bool:
    """--keep-going or KESTREL_KEEP_GOING=1: the old order and commands, every test stage run to
    its end (D-CI-614, D-CI-616)."""
    value = (os.environ if environ is None else environ).get(KEEP_GOING_ENV, "").strip()
    return bool(args.keep_going) or value == "1"


def _with_failfast(cmd: list[str], first: Path) -> list[str]:
    """The command with the fail-fast plugin and its file added after `-p no:cacheprovider`;
    nothing else changes (the invariant test strips exactly these three items, D-CI-610)."""
    at = cmd.index("no:cacheprovider") + 1
    return [*cmd[:at], *FAILFAST_PLUGIN, FAILFAST_OPTION, str(first), *cmd[at:]]


def _read_first_failure(first: Path) -> str | None:
    """The node ID in a stage's first-failure file; "" when the file exists but cannot be read
    (it still fails the stage); None when there is no file."""
    if not first.exists():
        return None
    try:
        return first.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _failfast_stage(
    stage: str, cmd: list[str], *, extra_env: dict[str, str] | None, fail_fast: bool,
) -> tuple[bool, list[tuple[int, str, str]], str | None]:
    """Run one pytest stage; return (passed, skips, first failure).

    With fail-fast the stage gets a fresh empty folder and a file path inside it that does not
    exist yet, so no earlier stage or gate can leave a file behind (D-CI-611). A stage passes only
    when pytest exits 0 AND that file does not exist (G5): a test held back by the plugin can never
    turn into a pass."""
    if not fail_fast:
        ok, skips = _run_pytest(cmd, extra_env=extra_env)
        return ok, skips, None
    folder = Path(tempfile.mkdtemp(prefix="kestrel-failfast-"))
    first = folder / "first-failure"
    try:
        ok, skips = _run_pytest(_with_failfast(cmd, first), extra_env=extra_env)
        failed = _read_first_failure(first)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if failed is not None:
        ok = False
        print(f"FIRST FAILURE in stage {stage}: {failed or '(the failure file was unreadable)'}",
              flush=True)
        if failed:
            write_record(FAILFAST_RECORD, [failed], stage)
    return ok, skips, failed


def _head_sha() -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, cwd=diff_gate.ROOT)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def write_record(path: Path, failed: list[str], stage: str) -> None:
    """Write the record atomically: a uniquely named temporary file beside it, then os.replace,
    so a crash leaves the old record or the new one, and two gates never share a temporary name.
    Any OSError is one printed note: the record never changes a gate's result (D-CI-612)."""
    data = {"version": RECORD_VERSION, "sha": _head_sha(), "failed": failed, "stage": stage}
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="failfast-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"==> fail-fast record not written ({exc}); the gate's result is unchanged",
              flush=True)
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass  # a scanner holding it; the next gate writes a new name


def safe_test_id(value: object, root: Path) -> bool:
    """A node ID the record may replay (G6): a string of at most RECORD_MAX_ID_CHARS characters
    (fifty fit on a Windows command line) with no control character, written as
    `tests/...` (so it can never be read as a pytest option), naming a `test_*.py` file that,
    once resolved, lies inside root/tests (so `tests/../x` cannot leave it) and exists."""
    if not isinstance(value, str) or "::" not in value or len(value) > RECORD_MAX_ID_CHARS:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    file_part = value.split("::", 1)[0]
    parts = PurePosixPath(file_part).parts
    if parts[:1] != ("tests",):
        return False
    if not (parts[-1].startswith("test_") and parts[-1].endswith(".py")):
        return False
    target = (root / file_part).resolve()
    if not target.is_relative_to((root / "tests").resolve()):
        return False
    return target.is_file()


def read_record(path: Path, root: Path) -> tuple[list[str], str]:
    """(IDs to replay, a note). A missing, empty, corrupt or wrong-shaped record gives no IDs and
    never an error: S5 steps aside and S7 runs everything (fail closed, D-CI-612)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], "no record of an earlier failure"
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"the record could not be read ({exc.__class__.__name__})"
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return [], "the record is not valid JSON"
    if not isinstance(data, dict) or data.get("version") != RECORD_VERSION \
            or not isinstance(data.get("failed"), list):
        return [], "the record has an unknown shape"
    ids: list[str] = []
    for item in data["failed"]:
        if safe_test_id(item, root) and item not in ids:
            ids.append(item)
        if len(ids) == RECORD_MAX_IDS:
            break
    sha = data.get("sha") if isinstance(data.get("sha"), str) else ""
    head = _head_sha()
    note = f"from commit {sha[:7]}, a hint only" if sha and sha != head else "from this commit"
    return ids, note


def is_browser_test(name: str) -> bool:
    path = PurePosixPath(name)
    return path.parts[:1] == ("tests",) and path.name.startswith("test_") \
        and path.name.endswith("_browser.py")


def _changed_selection() -> tuple[list[str], list[str]]:
    """(S4's test files, changed browser test files). S4 takes what the stress run takes
    (stress.affected_test_files: changed test files, the files that name a changed module, helper
    users); a changed `*_browser.py` file joins the browser files run live (D-CI-615)."""
    base, _note = diff_gate.diff_base()
    if base is None:
        return [], []
    names, _lines = diff_gate.git_changes(base)
    texts = stress.read_test_texts(diff_gate.ROOT)
    tests = stress.affected_test_files(names, texts, stress.read_texts("kestrel", diff_gate.ROOT))
    tests = [t for t in tests if (diff_gate.ROOT / t).is_file()]
    live = sorted(n for n in names if is_browser_test(n) and (diff_gate.ROOT / n).is_file())
    return tests, live


def _junit_path(args: argparse.Namespace, key: str, fallback: Path | None) -> Path | None:
    """Where a stage writes its junit report: --junit-dir/<key>.xml when given (the acceptance
    comparison reads one per stage), else the fallback."""
    if getattr(args, "junit_dir", None):
        folder = Path(args.junit_dir)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{key}.xml"
    return fallback


def _required_stages(browser: list[str], fail_fast: bool) -> list[str]:
    """The stages a full gate must have run before its pass line, worked out apart from the list
    that runs them (G4): the whole suite, stress, the browser files when any are selected, diff
    coverage and red-first. A stage dropped from the run list cannot reach "Ready to push."."""
    required = ["S7 suite", "S8 stress"]
    if browser:
        early = fail_fast and len(browser) <= EARLY_BROWSER_MAX
        required.append("S6 browser" if early else "S9 browser")
    return [*required, "S10 coverage", "S11 red-first"]


PASS_LINES = {
    "full": "Ready to push.",
    "quick": "Quick checks passed; run the full check before pushing.",
    "fast": "Fast checks passed; run the full check before pushing.",
    "stress": "Stress checks passed; run the full check before pushing.",
}


class _LastLine:
    """Pass stdout through unchanged while remembering the last non-empty line written."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.last = ""
        self.not_ready = ""
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            if line.strip():
                self.last = line.strip()
                if line.startswith("Not ready to push") and not self.not_ready:
                    self.not_ready = line.strip()
        return self.inner.write(text)

    def flush(self) -> None:
        self.inner.flush()

    def final_line(self) -> str:
        return self._partial.strip() or self.last

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--quick", action="store_true",
                        help="lint, the packaging guards and the static checks")
    parser.add_argument("--fast", action="store_true",
                        help="quick plus the whole suite, without coverage or the browser stage")
    parser.add_argument("--stress", action="store_true",
                        help="only the stress run over the tests this branch affects (the full "
                             "gate runs it too)")
    parser.add_argument("--workers", type=worker_count,
                        default=os.environ.get("KESTREL_TEST_WORKERS", "auto"), metavar="N",
                        help="workers (default: KESTREL_TEST_WORKERS or half the CPUs, "
                             "capped at 8, minimum 1); 0 runs serially")
    parser.add_argument("--browser-workers", type=browser_worker_count,
                        default=os.environ.get("KESTREL_BROWSER_WORKERS", "auto"), metavar="N",
                        help="workers for the live browser stage (default: one per CPU, at most "
                             f"{BROWSER_WORKER_CAP}; each test is CPU bound, and more workers "
                             "than CPUs push slow pages past the 30 s waits)")
    parser.add_argument("--junitxml", metavar="PATH", help="write test outcomes for comparison")
    parser.add_argument("--junit-dir", metavar="DIR",
                        help="write one junit report per test stage into DIR (the fail-fast "
                             "acceptance comparison, D-CI-610)")
    parser.add_argument("--keep-going", action="store_true",
                        help="the old order: every test stage runs to its end and lists every "
                             f"failure (also {KEEP_GOING_ENV}=1; D-CI-614)")
    return parser


def _never_fail_on_a_character() -> None:
    """A character the output encoding cannot hold is escaped, never fatal. On Windows stdout
    redirected to a file is cp1252, and one U+FFFD in a test's output crashed the gate."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def main() -> int:
    """Run the gate. The exit code always matches the words: 0 only when no "Not ready" line was
    printed and the last line is this tier's pass line (INSTRUCTIONS.md section 6), so
    `check.py && git push` is safe."""
    parser = _build_parser()
    args = parser.parse_args()
    if sum((args.quick, args.fast, args.stress)) > 1:
        parser.error("choose one of --quick, --fast or --stress")
    tier = ("quick" if args.quick else "fast" if args.fast
            else "stress" if args.stress else "full")
    _never_fail_on_a_character()
    real = sys.stdout
    tee = _LastLine(real)
    sys.stdout = tee
    try:
        code = _gate(args)
    finally:
        sys.stdout = real
    last = tee.final_line()
    if code != 0:
        return code
    if tee.not_ready:
        print("Not ready to push: a stage printed a failure but the gate went on "
              f"({tee.not_ready!r}).")
        return 1
    if last != PASS_LINES[tier]:
        print(f"Not ready to push: the gate stopped without its pass line (last line: {last!r}).")
        return 1
    return code


def _known_facts_line() -> str | None:
    """How the known-facts register divides (enforced, debt, practice), from the pre-flight
    tool beside this file. None where that tool is absent: it is not part of every copy of this
    tree. A register that cannot be read is said, never skipped silently."""
    tool = Path(__file__).resolve().parent / "preflight.py"
    if not tool.is_file():
        return None
    import preflight  # noqa: PLC0415  (same folder; present only beside the register)

    try:
        return preflight.count_line()
    except (OSError, ValueError) as exc:
        return f"Known facts: the register could not be read ({exc})"


def _gate(args: argparse.Namespace) -> int:
    py = sys.executable
    if args.stress:
        if not _stress_gate(py, True):
            return 1
        print(PASS_LINES["stress"])
        return 0
    base_override(False)  # says so when the variable is set and this tier ignores it
    plan = _plan_diff_guards()
    if plan is not None and plan[2]:
        _report_unmapped(plan[2])
        return 1
    if plan is None:
        print("Not ready to push: the diff guards (static checks, red-first proof, browser files "
              "for touched pages, diff coverage) need a git base to compare with and something "
              "to compare; run `git fetch origin main` (or create main) and run this again. "
              "No tier passes without them (D-CI-37).")
        return 1
    if args.quick:
        facts = _known_facts_line()
        if facts:
            print(f"==> {facts}", flush=True)
    steps = [("ruff", [py, "-m", "ruff", "check", "."])]
    tests = QUICK_TESTS if args.quick else []
    for label, cmd in steps:
        if not _run(label, cmd):
            print("Not ready to push: fix the failure above first.")
            return 1
    if not _static_gates():
        return 1
    if not _export_lint(py):
        return 1
    fail_fast = not keep_going(args)
    if not fail_fast:
        print("==> keep-going: the old order; every test stage runs to its end (D-CI-614)",
              flush=True)
    pytest_args = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                   "-n", str(args.workers), "--dist", "worksteal", "--max-worker-restart", "0"]
    junit_main = args.junitxml or _junit_path(args, "s7-suite", None)
    if junit_main:
        pytest_args.extend(["--junitxml", str(junit_main)])
    scratch = tempfile.TemporaryDirectory(prefix="kestrel-cov-")
    cov_env = {"COVERAGE_FILE": str(Path(scratch.name) / ".coverage")}
    light = args.quick or args.fast  # no coverage, no browser stage
    if light:
        ok, skips, _first = _failfast_stage("S7 suite", [*pytest_args, *tests], extra_env=None,
                                            fail_fast=fail_fast)
        _report_skips(skips)
        if not ok:
            print("Not ready to push: fix the failure above first.")
            return 1
        if plan[0]:
            print("Browser files the full gate would run for this branch: " + " ".join(plan[0]))
        if args.fast:
            print("Fast checks passed; run the full check before pushing.")
        else:
            print("Quick checks passed; run the full check before pushing.")
        return 0
    stages, browser = _full_stages(py, args, plan, pytest_args, cov_env, Path(scratch.name),
                                   fail_fast)
    if not _run_stages(stages, _required_stages(browser, fail_fast)):
        return 1
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
        cwd=diff_gate.ROOT,
    ).stdout.strip()
    if dirty:
        print("Checked the working tree, which has uncommitted changes; they are NOT in the push:")
        print(dirty)
    if FAILFAST_RECORD.exists():
        write_record(FAILFAST_RECORD, [], "ready")  # cleared only by a whole passing gate
    print("Ready to push.")
    return 0


Stage = tuple[str, Callable[[], bool]]


def _run_stages(stages: list[Stage], required: list[str]) -> bool:
    """Run the stages in order and stop at the first that fails (its own "Not ready" line is
    the last line). The gate passes only when every required stage ran (G4)."""
    ran: list[str] = []
    for name, run in stages:
        if not run():
            return False
        ran.append(name)
    missing = [name for name in required if name not in ran]
    if missing:
        print(f"Not ready to push: the gate skipped required stage(s) {', '.join(missing)}; "
              "it cannot pass without them (D-CI-610).")
        return False
    return True


def _full_stages(
    py: str, args: argparse.Namespace, plan: tuple[list[str], dict[str, set[int]], list[str]],
    pytest_args: list[str], cov_env: dict[str, str], scratch: Path, fail_fast: bool,
) -> tuple[list[Stage], list[str]]:
    """The full gate's stages after the static checks, in order, and the browser files it runs.

    Fail-fast (D-CI-610): S4 changed-file tests, S5 the last gate's failures, S6 up to
    EARLY_BROWSER_MAX browser files, S7 the whole suite, S8 stress, S9 more browser files, S10
    diff coverage, S11 red-first. S7 is the old suite command plus the plugin and --cov-append
    (so S6's coverage is kept); S4 to S6 only repeat tests S7 runs. Keep-going (D-CI-614): the
    old order and the old commands exactly: S7, S8, S9 with the mapped files, S10, S11."""
    browser_files, changed_lines, _unmapped = plan
    changed_tests, changed_browser = _changed_selection() if fail_fast else ([], [])
    browser = [*browser_files, *(f for f in changed_browser if f not in browser_files)]
    if changed_browser:
        print("==> changed browser test file(s), run live (D-CI-615): "
              + " ".join(changed_browser), flush=True)
    stages: list[Stage] = []
    if fail_fast:
        if len(changed_tests) > CHANGED_TESTS_MAX:
            print(f"==> S4 changed-file tests: {len(changed_tests)} files would nearly repeat "
                  "the suite; S7 runs them all, fail-fast (D-CI-610)", flush=True)
        elif changed_tests:
            stages.append(("S4 changed", lambda: _changed_tests_stage(py, args, changed_tests)))
        stages.append(("S5 last failures", lambda: _last_failures_stage(py, args, browser)))
        if browser and len(browser) <= EARLY_BROWSER_MAX:
            stages.append(("S6 browser", lambda: _browser_stage(
                py, args, browser, cov_env, scratch, "S6 browser", fail_fast)))
    cov_args = ["--cov=kestrel", "--cov-append", "--cov-report="] if fail_fast \
        else ["--cov=kestrel", "--cov-report="]
    suite_cmd = [*pytest_args, *cov_args]

    def suite() -> bool:
        ok, skips, _first = _failfast_stage("S7 suite", suite_cmd,
                                            extra_env={**FULL_GATE_ENV,
                                                       **_coverage_run_env(cov_env)},
                                            fail_fast=fail_fast)
        _report_skips(skips)
        if not ok:
            print("Not ready to push: fix the failure above first.")
        return ok

    stages.append(("S7 suite", suite))
    stages.append(("S8 stress", lambda: _stress_gate(py)))
    if browser and (not fail_fast or len(browser) > EARLY_BROWSER_MAX):
        stages.append(("S9 browser", lambda: _browser_stage(
            py, args, browser, cov_env, scratch, "S9 browser", fail_fast)))
    stages.append(("S10 coverage", lambda: _coverage_step(py, cov_env, scratch, changed_lines)))
    stages.append(("S11 red-first", _red_first_gate))
    return stages, browser


def _changed_tests_stage(py: str, args: argparse.Namespace, files: list[str]) -> bool:
    """S4: the test files this change selects, first, under the full gate's environment and
    without coverage (S7 measures it). A run that collected nothing (exit 5) is a note."""
    print(f"==> S4 changed-file tests: {len(files)} file(s) this change selects, run first "
          "(D-CI-610)", flush=True)
    cmd = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
           "-n", str(min(args.workers, stress.WORKERS)), "--dist", "load",
           "--max-worker-restart", "0"]
    junit = _junit_path(args, "s4-changed", None)
    if junit:
        cmd.extend(["--junitxml", str(junit)])
    ok, _skips, first = _failfast_stage("S4 changed", [*cmd, *files],
                                        extra_env=dict(FULL_GATE_ENV), fail_fast=True)
    if ok:
        return True
    if first is None and LAST_RUN["exit"] == 5:
        print("==> S4 note: the selected files hold no test to run; S7 runs every test")
        return True
    print("Not ready to push: a test this change selects failed (stage S4); fix it first.")
    return False


def _last_failures_stage(py: str, args: argparse.Namespace, browser: list[str]) -> bool:
    """S5: replay the last gate's recorded first failure, alone, before anything slow. Only IDs
    this gate runs anyway: a non-browser test, or a browser test whose file is in this run's
    browser selection (run live). It fails the gate only on a real failure (exit 1 or a
    first-failure file); any other result is a note that never starts with "Not ready to push"
    (G8), because S6, S7 and S9 run every test anyway (D-CI-612)."""
    ids, note = read_record(FAILFAST_RECORD, diff_gate.ROOT)
    plain = [i for i in ids if not is_browser_test(i.split("::", 1)[0])]
    live = [i for i in ids if i.split("::", 1)[0] in browser and i not in plain]
    if not plain and not live:
        print(f"==> S5 last run's failures: nothing to replay ({note})", flush=True)
        return True
    print(f"==> S5 last run's failures: {len(plain) + len(live)} test(s), {note} (D-CI-612)",
          flush=True)
    groups = (("s5-last", plain, dict(FULL_GATE_ENV)),
              ("s5-last-browser", live, {**FULL_GATE_ENV, "KESTREL_LIVE_BROWSER": "1"}))
    for key, group, env in groups:
        if not group:
            continue
        cmd = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-n", "0"]
        junit = _junit_path(args, key, None)
        if junit:
            cmd.extend(["--junitxml", str(junit)])
        # No OSError handler here: one around the whole stage could also swallow an error raised
        # after a failure was recorded (Codex round 2). RECORD_MAX_ID_CHARS keeps the command
        # line short enough to start; any other error stops the gate, never passes it.
        ok, _skips, first = _failfast_stage("S5 last failures", [*cmd, *group], extra_env=env,
                                            fail_fast=True)
        if first is not None or LAST_RUN["exit"] == 1:
            print("Not ready to push: a test that failed in the last gate still fails (stage "
                  "S5); fix it first.")
            return False
        if not ok:
            print(f"==> S5 note: pytest exit {LAST_RUN['exit']} on the recorded test(s) (renamed, "
                  "or not collected here); a hint only, S7 still runs every test")
        elif not LAST_RUN["passed"]:
            print("==> S5 note: the hint ran no test that passed (skipped here); a hint only, "
                  "S7 still runs every test")
    return True


def _browser_stage(py: str, args: argparse.Namespace, browser: list[str],
                   cov_env: dict[str, str], scratch: Path, stage: str, fail_fast: bool) -> bool:
    """S6 or S9: the selected browser files, live, adding to the coverage data, then the per-file
    evidence check (every file has a passed test, D-TEST-7). Browser tests are gated off in the
    main run."""
    key = "s6-browser" if stage.startswith("S6") else "s9-browser"
    junit = _junit_path(args, key, scratch / "browser-junit.xml")
    assert junit is not None  # the fallback is never None
    browser_args = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                    "-n", str(args.browser_workers), "--dist", "worksteal",
                    "--max-worker-restart", "0"]
    ok, bskips, _first = _failfast_stage(
        stage,
        [*browser_args, "--cov=kestrel", "--cov-append", "--cov-report=",
         "--junitxml", str(junit), *browser],
        extra_env={**_coverage_run_env(cov_env), "KESTREL_LIVE_BROWSER": "1"},
        fail_fast=fail_fast,
    )
    _report_skips(bskips)
    if not ok:
        print("Not ready to push: a browser test for a touched page failed.")
        return False
    return _browser_evidence_gate(junit, browser)


if __name__ == "__main__":
    raise SystemExit(main())
