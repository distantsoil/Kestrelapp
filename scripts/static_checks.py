"""Static checks that look only at what a branch changed (D-TEST-8), run by scripts/check.py.

pyright     type errors, compared with a committed baseline so only NEW errors in files changed on
            the branch fail (the existing ones are recorded, not hidden, in
            scripts/pyright_baseline.json)
ruff lines  BLE001 (blind except), S110 (try/except/pass), ASYNC (async pitfalls) and RUF006
            (dangling task), reported only for lines changed on the branch

Both exist because of two bugs a human review found in the ompepic batch: an `await` bound to the
wrong call, and a coroutine used without `await`. A type checker sees both; no test did.

    python scripts/static_checks.py --write-baseline   # regenerate (needs a decision ID, D-TEST-8)

Every function takes its inputs as arguments so tests/test_static_checks.py can feed it broken
code. Failure messages name the rule, file:line, and the fix.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "scripts" / "pyright_baseline.json"
LINT_SELECT = "BLE001,S110,ASYNC,RUF006"
PYRIGHT_TIMEOUT_SECONDS = 900


# ---- pyright -----------------------------------------------------------------------------------


# The three systems Kestrel ships on. The type check runs once for the machine it is on and once
# for each of the others, so code that is wrong only on Windows fails on a Linux builder too
# (D-CI-595: six such errors reached the release check on the owner's PC, because every builder
# was a Linux session and pyright checks for the system it runs on unless told otherwise).
PLATFORMS = ("Windows", "Darwin", "Linux")
# Not reported for another system: a package installed only there cannot be found here.
OTHER_PLATFORM_IGNORED = frozenset({"reportMissingImports", "reportMissingModuleSource"})


def host_platform(name: str | None = None) -> str:
    name = sys.platform if name is None else name
    return "Windows" if name == "win32" else "Darwin" if name == "darwin" else "Linux"


def run_pyright(paths: list[str], *, cwd: Path = ROOT, platform: str | None = None) -> list[dict]:
    """Error diagnostics for `paths` (repo-relative or absolute), from `pyright --outputjson`.
    With `platform` ("Windows", "Darwin", "Linux") the code is checked as that system sees it."""
    cmd = [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, "--outputjson"]
    if platform:
        cmd += ["--pythonplatform", platform]
    cmd += paths
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=PYRIGHT_TIMEOUT_SECONDS,
        encoding="utf-8",
        errors="replace",
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "RULE type check: pyright produced no JSON report. Fix: run "
            "`uv run --extra dev python -m pyright --version` and install it with "
            f"`uv sync --extra dev`. Output was: {(proc.stdout + proc.stderr)[:300]}"
        ) from exc
    return [d for d in data.get("generalDiagnostics", []) if d.get("severity") == "error"]


def _line_text(path: str, line: int, root: Path) -> str:
    file = Path(path) if Path(path).is_absolute() else root / path
    try:
        return file.read_text(encoding="utf-8", errors="replace").splitlines()[line - 1].strip()
    except (OSError, IndexError):
        return ""


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    # Resolved on both sides: pyright on Windows names a file "c:\\..." while Python says "C:\\...",
    # and only resolve() gives one spelling, for a file outside the repo too.
    resolved = p.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def fingerprint(diag: dict, root: Path = ROOT) -> tuple[str, str]:
    """(repo-relative file, key). The key survives edits elsewhere in the file: it hashes the rule,
    the first line of the message and the text of the offending line, never its number."""
    file = _rel(diag["file"], root)
    line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
    text = _line_text(diag["file"], line, root)
    msg = diag.get("message", "").splitlines()[0][:100]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return file, f"{diag.get('rule', 'error')}|{digest}|{msg}"


def make_baseline(diags: list[dict], root: Path = ROOT) -> dict:
    files: dict[str, Counter] = {}
    for d in diags:
        file, key = fingerprint(d, root)
        files.setdefault(file, Counter())[key] += 1
    return {
        "note": "Recorded pyright errors (D-TEST-8). Only NEW errors in changed files fail. "
        "Do not regenerate to silence a failure; fix the code.",
        "total": sum(sum(c.values()) for c in files.values()),
        "files": {f: dict(sorted(c.items())) for f, c in sorted(files.items())},
    }


def new_type_errors(
    diags: list[dict], baseline: dict, changed: set[str], root: Path = ROOT
) -> list[str]:
    """Messages for errors in `changed` files that the baseline does not already allow."""
    seen: dict[str, Counter] = {}
    by_key: dict[tuple[str, str], list[dict]] = {}
    for d in diags:
        file, key = fingerprint(d, root)
        if file not in changed:
            continue
        seen.setdefault(file, Counter())[key] += 1
        by_key.setdefault((file, key), []).append(d)
    problems: list[str] = []
    for file, counts in sorted(seen.items()):
        allowed = baseline.get("files", {}).get(file, {})
        for key, n in sorted(counts.items()):
            extra = n - allowed.get(key, 0)
            if extra <= 0:
                continue
            for d in by_key[(file, key)][-extra:]:
                line = d["range"]["start"]["line"] + 1
                msg = d["message"].splitlines()[0]
                problems.append(
                    f"RULE type check: {file}:{line}: {d.get('rule', 'error')}: {msg}. "
                    "Fix: correct the type error (a missing `await` or a call on the wrong "
                    "object is a real bug). Do not add the error to the baseline."
                )
    return problems


def other_platform_errors(files: list[str], baseline: dict, host_diags: list[dict], *,
                          root: Path = ROOT, host: str | None = None,
                          run=run_pyright) -> list[str]:
    """New errors that appear only when `files` are checked as another system sees them."""
    host = host or host_platform()
    known = Counter(fingerprint(d, root) for d in host_diags)
    problems: list[str] = []
    for platform in PLATFORMS:
        if platform == host:
            continue
        only_there: list[dict] = []
        left = Counter(known)
        for d in run(files, platform=platform):
            if d.get("rule") in OTHER_PLATFORM_IGNORED:
                continue
            key = fingerprint(d, root)
            if left[key] > 0:  # the same error the host run already judged
                left[key] -= 1
                continue
            only_there.append(d)
        for line in new_type_errors(only_there, baseline, set(files), root):
            problems.append(line.replace(
                "RULE type check: ", f"RULE type check (as {platform} sees it): ", 1).replace(
                "Fix: correct the type error", f"Fix: this line is wrong on {platform} only. "
                "Put the call behind `if sys.platform ...` so the checker can see it never "
                f"runs there, or use a call {platform} has. Correct the type error", 1))
    return problems


def type_check_changed(
    changed_names: list[str], *, baseline_path: Path = BASELINE_PATH, log=print
) -> list[str]:
    files = sorted(
        n
        for n in changed_names
        if n.startswith("kestrel/") and n.endswith(".py") and (ROOT / n).is_file()
    )
    if not files:
        log("    pyright: no Python file under kestrel/ changed, nothing to check")
        return []
    if not baseline_path.is_file():
        return [
            f"RULE type check: {baseline_path.name} is missing. Fix: restore it from git "
            "(`git checkout origin/main -- scripts/pyright_baseline.json`)."
        ]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    host_diags = run_pyright(files)
    problems = new_type_errors(host_diags, baseline, set(files))
    elsewhere = other_platform_errors(files, baseline, host_diags)
    log(f"    pyright: {len(files)} changed file(s), {len(problems)} new error(s) here, "
        f"{len(elsewhere)} more as {' and '.join(p for p in PLATFORMS if p != host_platform())} "
        "see them")
    return problems + elsewhere


# ---- ruff on changed lines ---------------------------------------------------------------------


def run_ruff_select(paths: list[str], *, cwd: Path = ROOT) -> list[dict]:
    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        LINT_SELECT,
        "--no-cache",
        "--output-format",
        "json",
        *paths,
    ]
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"RULE changed-line lint: ruff gave no JSON: {proc.stderr[:300]}"
        ) from exc


def lint_on_changed_lines(
    findings: list[dict], changed: dict[str, set[int]], root: Path = ROOT
) -> list[str]:
    """Keep ruff findings whose line range touches a changed line of the same file."""
    out: list[str] = []
    for f in findings:
        file = _rel(f["filename"], root)
        lines = changed.get(file)
        if not lines:
            continue
        first, last = f["location"]["row"], f["end_location"]["row"]
        if not any(first <= n <= last for n in lines):
            continue
        out.append(
            f"RULE changed-line lint ({f['code']}): {file}:{first}: {f['message']}. Fix: "
            + _FIXES.get(
                f["code"].rstrip("0123456789") if f["code"].startswith("ASYNC") else f["code"],
                "fix the lint finding (see `ruff rule " + f["code"] + "`).",
            )
        )
    return out


_FIXES = {
    "BLE001": "catch the specific exception, or log it with context and re-raise",
    "S110": "handle the error or log it; never swallow it with `pass`",
    "ASYNC": "do not block the event loop; use the async API or asyncio.to_thread",
    "RUF006": "keep a reference to the task (a set or attribute) so it cannot be collected",
}


def lint_changed(changed: dict[str, set[int]], *, log=print) -> list[str]:
    files = sorted(f for f in changed if f.endswith(".py") and (ROOT / f).is_file())
    if not files:
        return []
    problems = lint_on_changed_lines(run_ruff_select(files), changed)
    log(f"    ruff {LINT_SELECT} on changed lines: {len(problems)} finding(s)")
    return problems


def main() -> int:
    if "--write-baseline" in sys.argv:
        diags = run_pyright(["kestrel"])
        baseline = make_baseline(diags)
        BASELINE_PATH.write_text(json.dumps(baseline, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE_PATH} with {baseline['total']} errors")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
