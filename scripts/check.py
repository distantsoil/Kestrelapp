"""Run what CI runs, locally, before a push (D-CI-2).

GitHub Actions minutes are metered (Windows 2x, macOS 10x), so CI confirms rather than
discovers: lint, the full test suite (which includes the workflow, installer-script and
third-party-notice guards), in that order, stopping at the first failure.

    uv run --extra dev python scripts/check.py          # everything with capped parallelism
    uv run --extra dev python scripts/check.py --quick  # lint + packaging/workflow guards
    uv run --extra dev python scripts/check.py --workers 0  # serial comparison

Windows-only behavior still needs the Windows CI job; everything else should be green here
before anything is pushed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

QUICK_TESTS = ["tests/test_packaging.py", "tests/test_notices.py"]


# CI's environment (ci.yml `env:`); without it a desktop session takes the tray/browser paths.
CI_ENV = {"KESTREL_HEADLESS": "1"}


def _run(label: str, cmd: list[str]) -> bool:
    print(f"==> {label}: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    ok = subprocess.run(cmd, check=False, env={**os.environ, **CI_ENV}).returncode == 0
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


def _run_pytest(cmd: list[str]) -> tuple[bool, list[tuple[int, str, str]]]:
    """Run pytest with -rs, echo its output, and return (ok, [(count, where, reason)])."""
    print(f"==> pytest: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, env={**os.environ, **CI_ENV}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="lint and the packaging guards only")
    parser.add_argument("--workers", type=worker_count,
                        default=os.environ.get("KESTREL_TEST_WORKERS", "auto"), metavar="N",
                        help="workers (default: KESTREL_TEST_WORKERS or half the CPUs, "
                             "capped at 8, minimum 1); 0 runs serially")
    parser.add_argument("--junitxml", metavar="PATH", help="write test outcomes for comparison")
    args = parser.parse_args()
    py = sys.executable
    steps = [("ruff", [py, "-m", "ruff", "check", "."])]
    tests = QUICK_TESTS if args.quick else []
    for label, cmd in steps:
        if not _run(label, cmd):
            print("Not ready to push: fix the failure above first.")
            return 1
    pytest_args = [py, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                   "-n", str(args.workers), "--dist", "worksteal", "--max-worker-restart", "0"]
    if args.junitxml:
        pytest_args.extend(["--junitxml", args.junitxml])
    ok, skips = _run_pytest([*pytest_args, *tests])
    _report_skips(skips)
    if not ok:
        print("Not ready to push: fix the failure above first.")
        return 1
    if args.quick:
        print("Quick checks passed; run the full check before pushing.")
        return 0
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if dirty:
        print("Checked the working tree, which has uncommitted changes; they are NOT in the push:")
        print(dirty)
    print("Ready to push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
