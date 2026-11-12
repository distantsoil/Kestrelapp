"""The logic behind the two diff-based guards in scripts/check.py (D-TEST-7 A and C).

A. Browser tests for touched pages: changed UI files are mapped, through tests/browser_map.toml,
   to the browser test files that cover them. A UI file that maps to nothing fails the gate.
C. Diff coverage: the share of lines changed under kestrel/ (added or modified against the
   merge base) that some test executed, computed from coverage.py's JSON report.

Everything here is pure or takes its inputs as arguments, so tests/test_diff_gate.py can use
small synthetic inputs. Only the git helpers run git, and only locally (no network).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "tests" / "browser_map.toml"
DIFF_COVERAGE_MIN = 80.0

UI_GLOBS = (
    "kestrel/web/templates/*",
    "kestrel/web/static/*",
    "kestrel/web/pages/*.py",
    "kestrel/web/routes*.py",
    "kestrel/web/*_view.py",
    "kestrel/web/error_page.py",
    # Every module directly under kestrel/web, by glob and not by name (D-TEST-7): one that builds
    # no page context is mapped to the shell set explicitly in tests/browser_map.toml.
    "kestrel/web/*.py",
    # The documents engine builds the preview and PDF HTML: its templates, the modules that
    # render a template (render.py, letters.py) and the ones that feed or drive them.
    "kestrel/docs/templates/*",
    "kestrel/docs/render.py",
    "kestrel/docs/letters.py",
    "kestrel/docs/letter_task.py",
    "kestrel/docs/styles.py",
    "kestrel/docs/sections.py",
    "kestrel/docs/service.py",
    "kestrel/desktop/*",
)
SHELL_REF = "@shell"


def _norm(path: str) -> str:
    """Repo-relative POSIX form: backslashes become slashes, a leading ./ is dropped, so a
    Windows run (git there prints forward slashes, os.path does not) matches the same rules."""
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")


def is_ui_file(path: str) -> bool:
    """A file whose change can alter what a person sees (pycache excluded)."""
    path = _norm(path)
    if "__pycache__" in path or path.endswith((".pyc", ".pyo")):
        return False
    return any(fnmatch.fnmatchcase(path, g) for g in UI_GLOBS)


@dataclass(frozen=True)
class BrowserMap:
    shell: tuple[str, ...]
    rules: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]  # (patterns, browser files)
    # Browser files that run only in the soak tier (KESTREL_LAYOUT_CRAWL=1). Never selected by the
    # gate, so they are not in all_browser_files(); the completeness test accepts them.
    soak: tuple[str, ...] = ()

    def all_browser_files(self) -> set[str]:
        out = set(self.shell)
        for _patterns, files in self.rules:
            out.update(f for f in files if f != SHELL_REF)
        return out


def load_map(path: Path = MAP_PATH) -> BrowserMap:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = tuple((tuple(r["patterns"]), tuple(r["files"])) for r in data.get("rule", []))
    soak = tuple(data.get("soak", {}).get("files", []))
    return BrowserMap(shell=tuple(data["shell"]["files"]), rules=rules, soak=soak)


def browser_files_for(path: str, bmap: BrowserMap) -> set[str]:
    """Every browser test file (name only) covering `path`; empty when no rule matches."""
    path = _norm(path)
    found: set[str] = set()
    for patterns, files in bmap.rules:
        if any(fnmatch.fnmatchcase(path, p) for p in patterns):
            for f in files:
                found.update(bmap.shell if f == SHELL_REF else (f,))
    return found


def select_browser_tests(changed: list[str], bmap: BrowserMap) -> tuple[list[str], list[str]]:
    """(sorted tests/ paths to run, sorted changed UI files that map to nothing)."""
    selected: set[str] = set()
    unmapped: list[str] = []
    for raw in changed:
        path = _norm(raw)
        if not is_ui_file(path):
            continue
        hits = browser_files_for(path, bmap)
        if hits:
            selected.update(hits)
        else:
            unmapped.append(path)
    return [f"tests/{name}" for name in sorted(selected)], sorted(unmapped)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff_lines(diff: str) -> dict[str, set[int]]:
    """Added or modified line numbers per file from `git diff -U0` output (new-file numbering).
    Pure deletions add nothing; deleted files (`+++ /dev/null`) are skipped."""
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = _norm(target[2:] if target.startswith("b/") else target)
                changed.setdefault(current, set())
        elif current is not None and (m := _HUNK.match(line)):
            start = int(m.group(1))
            count = 1 if m.group(2) is None else int(m.group(2))
            changed[current].update(range(start, start + count))
    return {f: lines for f, lines in changed.items() if lines}


def _norm_key(path: str, root: Path) -> str:
    """A coverage.py file key as repo-relative POSIX: absolute keys (either separator, a Windows
    drive in any case) lose the repo root prefix."""
    key = path.replace("\\", "/")
    prefix = root.as_posix().rstrip("/") + "/"
    if key.casefold().startswith(prefix.casefold()) and (
        key.startswith(prefix) or ":" in prefix[:3]
    ):
        key = key[len(prefix):]
    return _norm(key)


def diff_coverage(
    changed: dict[str, set[int]],
    cov: dict,
    *,
    root: Path = ROOT,
    notes: list[str] | None = None,
) -> tuple[float | None, int, int, dict[str, list[int]]]:
    """(percent, executable changed lines, executed ones, {file: uncovered changed lines}).

    Only kestrel/*.py files count. A changed line counts when coverage.py treats it as
    executable (executed or missing), so comments, blanks and lines under `# pragma: no cover`
    (reported as excluded) never count. A file no test imported has no entry; its non-blank,
    non-comment changed lines all count as uncovered. A changed file that no longer exists is
    ignored, with a line appended to `notes`. percent is None with nothing to measure."""
    files = {_norm_key(k, root): v for k, v in cov.get("files", {}).items()}
    total = covered = 0
    uncovered: dict[str, list[int]] = {}
    for path, lines in sorted(changed.items()):
        key = _norm(path)
        if not (key.startswith("kestrel/") and key.endswith(".py")):
            continue
        entry = files.get(key)
        if entry is not None:
            hit = lines & set(entry.get("executed_lines", []))
            miss = sorted(lines & set(entry.get("missing_lines", [])))
        else:
            file = root / key
            if not file.is_file():
                if notes is not None:
                    notes.append(f"{key} no longer exists; ignored")
                continue
            src = file.read_text(encoding="utf-8", errors="replace").splitlines()
            hit = set()
            miss = sorted(
                n
                for n in lines
                if n <= len(src) and src[n - 1].strip() and not src[n - 1].lstrip().startswith("#")
            )
        covered += len(hit)
        total += len(hit) + len(miss)
        if miss:
            uncovered[key] = miss
    if total == 0:
        return None, 0, 0, {}
    return 100.0 * covered / total, total, covered, uncovered


# ---- git (local only) --------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _commit_date(ref: str) -> str:
    return _git("show", "-s", "--format=%cs", ref).stdout.strip() or "unknown date"


def describe_base(ref: str, sha: str, date: str, *, stale_against: str | None, head: str) -> str:
    """The loud note printed for the diff base: which ref, its commit and date, and why it may
    not be the base the branch will be reviewed against."""
    parts = [f"diff base is {ref} ({sha[:10]}, commit date {date})"]
    if stale_against:
        parts.append(
            f"WARNING: {ref} is older than local {stale_against}; changes already on "
            f"{stale_against} but not on {ref} are counted as this branch's. Fetch {ref} to fix"
        )
    if head == sha:
        parts.append(
            "WARNING: HEAD equals the base, so there is no committed difference to compare; only "
            "uncommitted and untracked changes are measured"
        )
    return "; ".join(parts)


def diff_base() -> tuple[str | None, str]:
    """(commit to diff against, note). origin/main's merge base, else main's, else None. The
    note always says which ref was used and its commit date."""
    head = _git("rev-parse", "HEAD").stdout.strip()
    missing = ""
    for ref in ("origin/main", "main"):
        proc = _git("merge-base", "HEAD", ref)
        if proc.returncode != 0 or not proc.stdout.strip():
            missing = "origin/main is missing; " if ref == "origin/main" else missing
            continue
        sha = proc.stdout.strip()
        stale = None
        if ref == "origin/main":
            # origin/main is stale when local main has commits it lacks.
            ahead = _git("rev-list", "--count", "origin/main..main")
            if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
                stale = "main"
        note = describe_base(ref, sha, _commit_date(ref), stale_against=stale, head=head)
        return sha, missing + note
    return None, "neither origin/main nor main exists, so the diff guards cannot run"


def empty_diff_problem(names: list[str], lines: dict, dirty: str) -> str | None:
    """A message when the diff came out empty although the working tree has changes (the gate
    would otherwise pass on zero evidence); None when the diff is consistent."""
    if names or lines or not dirty.strip():
        return None
    return (
        "the diff against the base is empty, but the working tree has changes "
        f"({len(dirty.strip().splitlines())} path(s) in git status); nothing would be checked"
    )


def git_changes(base: str) -> tuple[list[str], dict[str, set[int]]]:
    """(existing changed file names, changed lines) between `base` and the working tree, with
    uncommitted and untracked files included (an untracked .py file counts whole)."""
    names = {n for n in _git("diff", "--name-only", base).stdout.splitlines() if n}
    lines = parse_diff_lines(_git("diff", "-U0", "--no-color", base).stdout)
    for n in _git("ls-files", "--others", "--exclude-standard").stdout.splitlines():
        if not n:
            continue
        names.add(n)
        path = ROOT / n
        if n.endswith(".py") and path.is_file():
            count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if count:
                lines[n] = set(range(1, count + 1))
    return sorted(_norm(n) for n in names if (ROOT / n).exists()), lines


# ---- browser evidence ----------------------------------------------------------------------------


def browser_evidence(junit_xml: str, files: list[str]) -> dict[str, str]:
    """{selected file: problem} from a pytest junit XML. A selected browser file needs at least
    one test that passed; collected-nothing and all-skipped files are problems, and the message
    carries the skip reasons. Failures are reported by pytest's own exit status, not here."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415  (only this function parses XML)

    outcomes: dict[str, dict[str, list[str]]] = {}
    for case in ET.fromstring(junit_xml).iter("testcase"):
        parts = reversed(case.get("classname", "").split("."))
        module = next((seg for seg in parts if seg.startswith("test_")), None)
        if module is None:
            continue
        bucket = outcomes.setdefault(module + ".py", {"passed": [], "skipped": [], "bad": []})
        skipped = case.find("skipped")
        if skipped is not None:
            bucket["skipped"].append((skipped.get("message") or "no reason given").strip())
        elif case.find("failure") is not None or case.find("error") is not None:
            bucket["bad"].append("failed")
        else:
            bucket["passed"].append(case.get("name", ""))
    problems: dict[str, str] = {}
    for path in files:
        name = PurePosixPath(_norm(path)).name
        got = outcomes.get(name)
        if got is None or not (got["passed"] or got["skipped"] or got["bad"]):
            problems[path] = "collected no tests"
        elif not got["passed"] and not got["bad"]:
            reasons = sorted(set(got["skipped"]))
            problems[path] = f"all {len(got['skipped'])} test(s) were skipped: " + "; ".join(
                r[:160] for r in reasons
            )
    return problems
