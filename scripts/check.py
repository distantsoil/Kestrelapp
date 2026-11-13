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

Windows-only behavior still needs the Windows CI job; everything else should be green here
before anything is pushed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diff_gate  # noqa: E402  (sits beside this script; the path is added just above)
import static_checks  # noqa: E402  (same folder)
import stress  # noqa: E402  (same folder)
import test_floor  # noqa: E402  (same folder)

QUICK_TESTS = ["tests/test_packaging.py", "tests/test_notices.py"]


# CI's environment (ci.yml `env:`); without it a desktop session takes the tray/browser paths.
CI_ENV = {"KESTREL_HEADLESS": "1"}
# The full gate also runs the clock-boundary run (D-TEST-3, #116): the "today" and "this month"
# modules again with the clock frozen just after midnight. --quick and CI leave it off.
FULL_GATE_ENV = {"KESTREL_BOUNDARY": "1"}


def _run(label: str, cmd: list[str]) -> bool:
    print(f"==> {label}: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    ok = subprocess.run(
        cmd, check=False, env={**os.environ, **CI_ENV}, cwd=diff_gate.ROOT
    ).returncode == 0
    print(f"    {'ok' if ok else 'FAILED'} ({time.monotonic() - started:.0f}s)", flush=True)
    return ok


SKIP_LINE = re.compile(r"^SKIPPED \[(\d+)\] (.+?): (.*)$")
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
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        if m := SKIP_LINE.match(line.rstrip()):
            skips.append((int(m.group(1)), m.group(2), m.group(3)))
    ok = proc.wait() == 0
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
        print(f"==> static checks: skipped, {note}")
        return True
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
    warnings += more_warnings
    for line in [*warnings, *problems]:
        print(line)
    if problems:
        print(f"Not ready to push: {len(problems)} static check problem(s) above. Each names the "
              "rule and the fix.")
    return not problems


def _red_first_gate() -> bool:
    base, _note = diff_gate.diff_base()
    if base is None:
        return True
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


def _stress_gate(py: str) -> bool:
    """The stress run over the tests this branch affects (D-CI-26, scripts/stress.py)."""
    base, note = diff_gate.diff_base()
    if base is None:
        print(f"Not ready to push: the stress run needs a git base ({note}).")
        return False
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
    first_s = 0.0
    orders = 0
    for run in runs:
        if run.repeat and not stress.budget_allows(time.monotonic() - started, first_s):
            print(f"==> stress: {orders} of {stress.REPEATS} random orders ran; one took "
                  f"{first_s:.0f}s, so another would pass the {stress.BUDGET_S // 60} min budget "
                  "(D-CI-29)", flush=True)
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
        if not ok:
            env = " ".join(f"{k}={v}" for k, v in run.env.items())
            print(f"Replay: {env} {' '.join(stress.pytest_args('python', run))}".strip())
            print(f"Not ready to push: the stress run failed ({run.label}). A test that passes "
                  "alone but fails here is order-, port- or clock-dependent; fix its cause.")
            return False
        if "random order" in run.label:
            orders += 1
            first_s = first_s or time.monotonic() - run_started
    print(f"    stress run ok ({time.monotonic() - started:.0f}s, {orders} random order(s))",
          flush=True)
    return True


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
    parser.add_argument("--browser-workers", type=worker_count,
                        default=os.environ.get("KESTREL_BROWSER_WORKERS", "1"), metavar="N",
                        help="workers for the live browser stage (default 1: each test drives a "
                             "real browser against a server under coverage, and two at once on "
                             "a small machine push slow pages past the 30 s waits)")
    parser.add_argument("--junitxml", metavar="PATH", help="write test outcomes for comparison")
    return parser


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


def _gate(args: argparse.Namespace) -> int:
    py = sys.executable
    if args.stress:
        if not _stress_gate(py):
            return 1
        print(PASS_LINES["stress"])
        return 0
    plan = _plan_diff_guards()
    if plan is not None and plan[2]:
        _report_unmapped(plan[2])
        return 1
    if plan is None and not (args.quick or args.fast):
        print("Not ready to push: the diff guards (browser files for touched pages, diff "
              "coverage) need a git base; fetch origin/main or create main.")
        return 1
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
    pytest_args = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                   "-n", str(args.workers), "--dist", "worksteal", "--max-worker-restart", "0"]
    if args.junitxml:
        pytest_args.extend(["--junitxml", args.junitxml])
    scratch = tempfile.TemporaryDirectory(prefix="kestrel-cov-")
    cov_env = {"COVERAGE_FILE": str(Path(scratch.name) / ".coverage")}
    light = args.quick or args.fast  # no coverage, no browser stage
    cov_args = [] if light or plan is None else ["--cov=kestrel", "--cov-report="]
    ok, skips = _run_pytest(
        [*pytest_args, *cov_args, *tests],
        extra_env=None if light else {**FULL_GATE_ENV, **cov_env},
    )
    _report_skips(skips)
    if not ok:
        print("Not ready to push: fix the failure above first.")
        return 1
    if light:
        if plan is not None and plan[0]:
            print("Browser files the full gate would run for this branch: " + " ".join(plan[0]))
        if args.fast:
            print("Fast checks passed; run the full check before pushing.")
        else:
            print("Quick checks passed; run the full check before pushing.")
        return 0
    assert plan is not None  # checked before the suite ran
    if not _stress_gate(py):
        return 1
    browser_files, changed_lines, _unmapped = plan
    if browser_files:
        # Browser tests are gated off in the main run; run the selected files for real, and add
        # their execution to the coverage data.
        junit = Path(scratch.name) / "browser-junit.xml"
        browser_args = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                        "-n", str(args.browser_workers), "--dist", "worksteal",
                        "--max-worker-restart", "0"]
        ok, bskips = _run_pytest(
            [*browser_args, "--cov=kestrel", "--cov-append", "--cov-report=",
             "--junitxml", str(junit), *browser_files],
            extra_env={**cov_env, "KESTREL_LIVE_BROWSER": "1"},
        )
        _report_skips(bskips)
        if not ok:
            print("Not ready to push: a browser test for a touched page failed.")
            return 1
        if not _browser_evidence_gate(junit, browser_files):
            return 1
    if not _coverage_step(py, cov_env, Path(scratch.name), changed_lines):
        return 1
    if not _red_first_gate():
        return 1
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
        cwd=diff_gate.ROOT,
    ).stdout.strip()
    if dirty:
        print("Checked the working tree, which has uncommitted changes; they are NOT in the push:")
        print(dirty)
    print("Ready to push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
