"""Collect every CI result for a commit before fixing anything.

    python scripts/ci_status.py <owner/repo> <sha> [--wait] [--timeout 5400]

Reads the commit's check runs with `gh api`. With --wait it polls every 60 seconds until no run
is queued or in progress. It then prints one line per check (name, conclusion); for each failed
GitHub Actions job it prints the `FAILED ...` lines and the `N failed, N passed` line from the job
log; for a failed code-scanning check it lists the alert titles the check output carries.

The last line is `CI GREEN: <n> checks` or `CI RED: <n> failed: <names>`, and the exit code
matches (0 only for green). Waiting for all results before fixing anything lets one pass fix every
finding instead of restarting a long gate for each one.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

POLL_SECONDS = 60
GH_CALL_TIMEOUT = 60
GH_ATTEMPTS = 3
GH_BACKOFF_SECONDS = 5
DEFAULT_REQUIRED = ["CodeQL"]
DEFAULT_TIMEOUT = 5400
PASSING = {"success", "neutral", "skipped"}
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FAILED_LINE = re.compile(r"^FAILED ")
SUMMARY_LINE = re.compile(r"\b\d+ failed\b.*|\b\d+ passed\b.*")
BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")
MAX_ALERT_LINES = 20
FALLBACK_TAIL = 15

# Module-level aliases so tests can patch the clock and the sleep without touching the globals.
_sleep = time.sleep
_monotonic = time.monotonic


class GhError(RuntimeError):
    """`gh` could not be run, kept failing, or ran out of time."""


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - _monotonic()


def gh_api(path: str, *, paginate: bool = False, deadline: float | None = None) -> str:
    """Run `gh api`, retrying transient failures with backoff. Every call has a timeout, and no
    attempt starts after `deadline` (a `_monotonic` reading), so a hung call cannot outlast it."""
    cmd = ["gh", "api", *(["--paginate"] if paginate else []), path]
    last = "no attempt was made"
    for attempt in range(GH_ATTEMPTS):
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise GhError(f"gave up on gh api {path}: out of time ({last})")
        limit = GH_CALL_TIMEOUT if left is None else max(1.0, min(GH_CALL_TIMEOUT, left))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, encoding="utf-8",
                errors="replace", timeout=limit,
            )
        except subprocess.TimeoutExpired:
            last = f"timed out after {limit:.0f}s"
        except OSError as exc:
            raise GhError(f"could not run gh: {exc}") from exc
        else:
            if proc.returncode == 0:
                return proc.stdout
            last = proc.stderr.strip()[:200] or f"exit {proc.returncode}"
        if attempt + 1 < GH_ATTEMPTS:
            _sleep(GH_BACKOFF_SECONDS * (attempt + 1))
    raise GhError(f"gh api {path} failed after {GH_ATTEMPTS} attempts: {last}")


def _json_documents(raw: str) -> list[dict]:
    """`gh api --paginate` prints one JSON document per page, back to back."""
    decoder = json.JSONDecoder()
    docs: list[dict] = []
    pos = 0
    while pos < len(raw):
        if raw[pos].isspace():
            pos += 1
            continue
        try:
            doc, pos = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError as exc:
            raise GhError(f"check-runs response is not JSON: {exc}") from exc
        docs.append(doc)
    return docs


def fetch_runs(repo: str, sha: str, deadline: float | None = None) -> list[dict]:
    raw = gh_api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", paginate=True,
                 deadline=deadline)
    runs: list[dict] = []
    for doc in _json_documents(raw):
        runs.extend(doc.get("check_runs", []))
    return runs


def is_code_scanning(run: dict) -> bool:
    name = str(run.get("name", "")).lower()
    return (run.get("app") or {}).get("slug") == "github-code-scanning" or name == "codeql"


def missing_required(runs: list[dict], required: list[str]) -> list[str]:
    """Required check names (matched case-insensitively as a substring) with no completed run."""
    done = [str(r.get("name", "")).lower() for r in runs if r.get("status") == "completed"]
    return [req for req in required if not any(req.lower() in name for name in done)]


def pending(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r.get("status") != "completed"]


def is_failed(run: dict) -> bool:
    return run.get("status") != "completed" or run.get("conclusion") not in PASSING


def describe(run: dict) -> str:
    return str(run.get("conclusion") or run.get("status") or "unknown")


def failure_lines(log: str) -> list[str]:
    """FAILED lines and the pass/fail count line from a job log, timestamps stripped."""
    keep: list[str] = []
    for raw in log.splitlines():
        line = ANSI.sub("", TIMESTAMP.sub("", raw)).strip()
        if FAILED_LINE.match(line) or (SUMMARY_LINE.search(line) and "failed" in line):
            keep.append(line)
    return keep


def log_tail(log: str) -> list[str]:
    lines = [ANSI.sub("", TIMESTAMP.sub("", raw)).rstrip() for raw in log.splitlines()]
    return [line for line in lines if line.strip()][-FALLBACK_TAIL:]


def alert_titles(run: dict) -> list[str]:
    out = run.get("output") or {}
    texts = [str(out.get(k) or "") for k in ("summary", "text")]
    lines = [ln.strip() for text in texts for ln in text.splitlines() if BULLET.match(ln)]
    return lines[:MAX_ALERT_LINES]


def is_actions_job(run: dict) -> bool:
    return (run.get("app") or {}).get("slug") == "github-actions"


def report_failure(repo: str, run: dict) -> None:
    name = run.get("name", "?")
    if is_actions_job(run):
        print(f"--- {name} (job {run.get('id')}) ---")
        try:
            log = gh_api(f"repos/{repo}/actions/jobs/{run.get('id')}/logs")
        except GhError as exc:
            print(f"  (log unavailable: {exc})")
            return
        for line in failure_lines(log) or log_tail(log):
            print(f"  {line}")
        return
    out = run.get("output") or {}
    print(f"--- {name} ---")
    if out.get("title"):
        print(f"  {out['title']}")
    for line in alert_titles(run):
        print(f"  {line}")


def wait_for_runs(
    repo: str, sha: str, timeout: float, required: list[str] | None = None
) -> list[dict]:
    """Poll until every run is finished and every required check exists. `timeout` bounds the
    whole wait, including calls that hang."""
    started = _monotonic()
    deadline = started + timeout
    required = required or []
    runs: list[dict] = []
    while True:
        try:
            runs = fetch_runs(repo, sha, deadline)
        except GhError:
            if _monotonic() >= deadline and runs:
                return runs
            raise
        if runs and not pending(runs) and not missing_required(runs, required):
            return runs
        if _monotonic() >= deadline:
            return runs
        waiting = [r.get("name", "?") for r in pending(runs)] + [
            f"{req} (not reported yet)" for req in missing_required(runs, required)]
        print(f"waiting {POLL_SECONDS}s: {', '.join(waiting) or 'no checks yet'}", flush=True)
        _sleep(POLL_SECONDS)


def summarize(repo: str, runs: list[dict], required: list[str] | None = None) -> int:
    for run in runs:
        print(f"{run.get('name', '?')}: {describe(run)}")
    failed = [r for r in runs if is_failed(r)]
    for run in failed:
        if run.get("status") == "completed":
            report_failure(repo, run)
    for run in runs:
        # A neutral code-scanning check passes, but its alerts are still findings to read.
        if run not in failed and is_code_scanning(run) and alert_titles(run):
            print(f"--- {run.get('name', '?')} (passed, with alerts to review) ---")
            for line in alert_titles(run):
                print(f"  {line}")
    absent = missing_required(runs, required or [])
    if not runs and not absent:
        print("CI RED: 0 checks found for this commit")
        return 1
    names = [
        r.get("name", "?") + ("" if r.get("status") == "completed" else f" ({r.get('status')})")
        for r in failed
    ] + [f"{req} (missing)" for req in absent if not any(
        req.lower() in str(r.get("name", "")).lower() for r in failed)]
    if names:
        print(f"CI RED: {len(names)} failed: {', '.join(names)}")
        return 1
    print(f"CI GREEN: {len(runs)} checks")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("sha")
    parser.add_argument("--wait", action="store_true", help="poll until no run is unfinished")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to wait with --wait, hung calls included (default 5400)")
    parser.add_argument("--require", action="append", metavar="NAME",
                        help="a check that must exist and be finished before the result counts "
                             "(repeatable; default CodeQL, which appears only after its workflow "
                             "uploads; `--require none` turns this off)")
    args = parser.parse_args(argv)
    required = DEFAULT_REQUIRED if args.require is None else [
        r for r in args.require if r.lower() != "none"]
    try:
        if args.wait:
            runs = wait_for_runs(args.repo, args.sha, args.timeout, required)
        else:
            runs = fetch_runs(args.repo, args.sha, _monotonic() + args.timeout)
        return summarize(args.repo, runs, required)
    except GhError as exc:
        print(f"CI RED: 1 failed: ci_status (could not read CI results: {exc})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
