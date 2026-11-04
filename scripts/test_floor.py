"""Anti-gaming checks for the test suite (D-TEST-8), run by scripts/check.py.

Four checks, each failing with a plain message that names the rule, the file (and line) and the fix:

  floor      a test file may not lose asserts or tests, or gain skips, versus the merge base
  protected  the files that define the gates cannot change without a decision ID in a commit
  quality    lint for tests written to pass rather than to prove (changed lines only)
  red_first  every `fix:` commit's new tests must FAIL on the commit's parent (D-TEST-6)

A "decision ID" is D-AREA-N (for example D-TEST-8). It justifies a change when a commit message on
the branch has a line that contains both the ID and the changed path. Everything here is pure or
takes its inputs as arguments, so tests/test_test_floor.py can feed it broken input. Only the git
helpers run git, locally.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DECISION_ID = re.compile(r"\bD-[A-Z]+-[0-9]+\b")
REMOVALS_FILE = "tests/.removals.toml"
RED_FIRST_BUDGET_SECONDS = 600
FIX_SUBJECT = re.compile(r"^fix(\([^)]*\))?!?:")

# Paths whose change needs a decision ID (D-TEST-8). A trailing slash means "anything under it".
PROTECTED_PATHS = (
    "scripts/check.py",
    "scripts/diff_gate.py",
    "scripts/test_floor.py",
    "scripts/static_checks.py",
    "scripts/pyright_baseline.json",
    "tests/conftest.py",
    "tests/browser_plugin.py",
    "tests/browser_map.toml",
    REMOVALS_FILE,
    ".gitignore",
    "CLAUDE.md",
    "AGENTS.md",
    ".github/",
)
# Module-level names in tests/test_contracts.py whose value is an allowlist.
ALLOWLIST_NAME = re.compile(r"ALLOWED|^NOT_A_|^NOT_CONSENT|^DYNAMIC_URLS|EXEMPT")


def _norm(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix().removeprefix("./")


# ---- the public export ------------------------------------------------------------------------


def public_export_note(root: Path | None = None) -> str | None:
    """A printable reason when this tree is the public export, else None.

    The public repository receives one sync commit per change and no decision IDs, so the history
    checks (assertion floor, protected paths, red-first) cannot be satisfied there; they are
    enforced on the private branch. INSTRUCTIONS.md marks that tree: it exists only there, and
    the export drops it. Every other check still runs on the public tree."""
    root = root or ROOT
    if (root / "INSTRUCTIONS.md").exists():
        return None
    return (
        "NOTE public export detected (no INSTRUCTIONS.md): the assertion "
        "floor, protected-path and red-first checks are skipped here because they read branch "
        "history and decision IDs, which the public repository does not carry; they ran on the "
        "private branch. All other checks still run."
    )


# ---- decision IDs ------------------------------------------------------------------------------


def justified_paths(messages: str, paths: list[str]) -> set[str]:
    """The paths from `paths` named, with a decision ID, on one line of the commit messages."""
    lines = [ln for ln in messages.splitlines() if DECISION_ID.search(ln)]
    return {p for p in paths if any(_norm(p) in ln.replace("\\", "/") for ln in lines)}


def read_removals(text: str) -> dict[str, str]:
    """{file: decision ID} from tests/.removals.toml entries that carry both an ID and a reason."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    out: dict[str, str] = {}
    for entry in data.get("removal", []):
        decision = str(entry.get("decision", ""))
        if DECISION_ID.fullmatch(decision) and str(entry.get("reason", "")).strip():
            out[_norm(str(entry.get("file", "")))] = decision
    return out


# ---- check 1: the assertion floor --------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    asserts: int  # assert statements plus pytest.raises / pytest.warns uses
    tests: int
    skips: int  # skip / skipif / xfail markers and pytest.skip / pytest.xfail calls

    def __str__(self) -> str:
        return f"{self.asserts} asserts, {self.tests} tests, {self.skips} skips"


def _is_pytest_attr(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in names
        and isinstance(node.value, ast.Name)
        and node.value.id == "pytest"
    )


def file_stats(source: str) -> Stats:
    """Count what a test file proves (AST, never regex). A syntax error counts as an empty file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Stats(0, 0, 0)
    asserts = tests = skips = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) or (
            isinstance(node, ast.Call) and _is_pytest_attr(node.func, {"raises", "warns"})
        ):
            asserts += 1
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            tests += 1
        elif isinstance(node, ast.Attribute):
            marker = (
                node.attr in {"skip", "skipif", "xfail"}
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            )
            if marker or _is_pytest_attr(node, {"skip", "xfail"}):
                skips += 1
    return Stats(asserts, tests, skips)


def floor_problems(
    before: dict[str, str | None],
    after: dict[str, str | None],
    justified: set[str],
) -> list[str]:
    """before/after map a test file to its source (None: file absent). Files in `justified` (a
    decision ID names them) are allowed to drop."""
    problems: list[str] = []
    for path in sorted(before):
        old_src, new_src = before[path], after.get(path)
        if old_src is None or path in justified:
            continue
        old = file_stats(old_src)
        new = file_stats(new_src) if new_src is not None else Stats(0, 0, 0)
        if new.asserts < old.asserts or new.tests < old.tests or new.skips > old.skips:
            what = "was deleted or emptied" if new_src is None else "lost proof or gained skips"
            problems.append(
                f"RULE assertion floor: {path} {what}: before {old}, after {new}. "
                f"Fix: restore the lost asserts/tests and remove the new skips. If the loss is "
                f"deliberate, put a line in a branch commit message naming {path} and a decision "
                f"ID (for example 'D-TEST-9: {path} removed, covered by ...'), or list the file "
                f"with decision and reason in {REMOVALS_FILE}."
            )
    return problems


# ---- check 2: protected paths ------------------------------------------------------------------


def _toml_tables(text: str | None, prefixes: tuple[str, ...]) -> dict:
    if not text:
        return {}
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        return {"unparseable": text}
    return {k: v for k, v in tool.items() if k.startswith(prefixes)}


def allowlist_dump(source: str | None) -> dict[str, str]:
    """{name: ast dump} of the allowlist assignments at the top of a module."""
    if not source:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"syntax error": source}
    out: dict[str, str] = {}
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        value = getattr(node, "value", None)
        for target in targets:
            if isinstance(target, ast.Name) and ALLOWLIST_NAME.search(target.id) and value:
                out[target.id] = ast.dump(value)
    return out


def protected_problems(
    changed: list[str],
    base_text: dict[str, str | None],
    new_text: dict[str, str | None],
    messages: str,
) -> list[str]:
    """`changed` is the branch's changed paths. base_text/new_text hold the sources of
    pyproject.toml and tests/test_contracts.py, which are protected only in part."""
    touched: list[str] = []
    for raw in changed:
        path = _norm(raw)
        if any(path == p or (p.endswith("/") and path.startswith(p)) for p in PROTECTED_PATHS):
            touched.append(path)
    pyproject = "pyproject.toml"
    if pyproject in map(_norm, changed):
        prefixes = ("pytest", "ruff", "pyright")
        if _toml_tables(base_text.get(pyproject), prefixes) != _toml_tables(
            new_text.get(pyproject), prefixes
        ):
            touched.append(pyproject)
    contracts = "tests/test_contracts.py"
    if contracts in map(_norm, changed) and allowlist_dump(
        base_text.get(contracts)
    ) != allowlist_dump(new_text.get(contracts)):
        touched.append(contracts)
    okay = justified_paths(messages, touched)
    bad = sorted(set(touched) - okay)
    return [
        f"RULE protected path: {p} defines or weakens a gate and changed without a decision ID. "
        f"Fix: if the change is intended, put a line in a branch commit message that names {p} "
        f"and a decision ID (for example 'D-TEST-9: {p} now ...') after recording the decision "
        f"in the private decision log; otherwise revert the change."
        for p in bad
    ]


# ---- check 3: test-quality lint ----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        tag = "WARNING" if self.warning else "RULE"
        return f"{tag} {self.rule}: {self.path}:{self.line}: {self.message}"


_ASSERTING_CALL = re.compile(r"assert|expect|raises|warns|fail|verify", re.IGNORECASE)
_GETSOURCE = {"getsource", "getsourcelines", "getsourcefile"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _has_getsource(node: ast.AST, tainted: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _call_name(sub) in _GETSOURCE:
            return True
        if isinstance(sub, ast.Name) and sub.id in tainted:
            return True
    return False


def _asserts_with_parents(func: ast.AST):
    """Yield (assert node, whether it sits inside an `if`) for one test function."""

    def visit(node: ast.AST, in_if: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Assert):
                yield child, in_if
            yield from visit(child, in_if or isinstance(child, ast.If))

    yield from visit(func, False)


def _function_tests(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            yield node


def quality_findings(path: str, source: str, changed: set[int] | None) -> list[Finding]:
    """Lint one test file. `changed` is the set of changed line numbers: a finding counts only when
    its lines overlap them (None: the whole file, used for a new file)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, "test syntax", f"cannot parse: {exc.msg}")]

    def hit(first: int, last: int) -> bool:
        return changed is None or any(first <= n <= last for n in changed)

    out: list[Finding] = []
    for func in _function_tests(tree):
        last = func.end_lineno or func.lineno
        tainted: set[str] = set()
        for sub in ast.walk(func):
            if isinstance(sub, ast.Assign) and _has_getsource(sub.value, set()):
                tainted.update(t.id for t in sub.targets if isinstance(t, ast.Name))
        proves = False
        for sub in ast.walk(func):
            if isinstance(sub, ast.Assert):
                proves = True
            elif isinstance(sub, ast.Call) and _ASSERTING_CALL.search(_call_name(sub)):
                proves = True
        for node, in_if in _asserts_with_parents(func):
            if not hit(node.lineno, node.end_lineno or node.lineno):
                continue
            if in_if:
                out.append(
                    Finding(
                        path,
                        node.lineno,
                        "conditional assert",
                        "an assert inside `if` passes silently when the condition is false. "
                        "Fix: assert the condition itself, or split into one test per branch",
                    )
                )
            if _has_getsource(node.test, tainted):
                out.append(
                    Finding(
                        path,
                        node.lineno,
                        "source-text assert",
                        "asserting on inspect.getsource output checks how the code is written, not "
                        "what it does (D-TEST-6). Fix: call the code and assert on its result",
                    )
                )
            test = node.test
            if isinstance(test, ast.Constant) and test.value:
                out.append(
                    Finding(
                        path,
                        node.lineno,
                        "assert True",
                        "this assert can never fail. Fix: assert the real expectation",
                    )
                )
            if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
                out.append(
                    Finding(
                        path,
                        node.lineno,
                        "assert with or",
                        "`assert a or b` passes when either holds, which often hides a weak check. "
                        "Fix: assert the exact expected value, or confirm both alternatives are "
                        "intended",
                        warning=True,
                    )
                )
        if not proves and hit(func.lineno, last):
            out.append(
                Finding(
                    path,
                    func.lineno,
                    "test without assertion",
                    f"{func.name} has no assert, pytest.raises/warns or expect call, so it cannot "
                    "fail on a wrong result. Fix: assert what the person would see",
                )
            )
    return out


# ---- check 4: red-first proof for fix commits --------------------------------------------------


def is_fix_subject(subject: str) -> bool:
    return bool(FIX_SUBJECT.match(subject.strip()))


def failed_test_ids(pytest_output: str) -> list[str]:
    """Node ids from `-rfE` summary lines of a pytest run."""
    return [
        m.group(1)
        for m in re.finditer(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", pytest_output, re.MULTILINE)
    ]


def red_first_verdict(parent_exit: int, fix_exit: int | None) -> str | None:
    """None when the tests prove the fix; otherwise the reason they do not.
    parent_exit: pytest exit code on the commit's parent (1 failed, 2 collection error, 0 passed,
    5 nothing collected); fix_exit: exit code of the failing tests re-run on the fix commit."""
    if parent_exit == 0:
        return "every new test PASSES on the parent commit, so none of them proves the fix"
    if parent_exit == 5:
        return "pytest collected no tests from the commit's test files"
    if parent_exit not in (1, 2):
        return f"pytest on the parent commit ended with status {parent_exit}, not a test failure"
    if fix_exit not in (None, 0):
        return (
            "the tests that fail on the parent also fail on the fix commit, so the failure "
            "is not evidence the fix works (environment, order, or a broken test)"
        )
    return None


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def _pytest(worktree: Path, files: list[str], *, ids: list[str] | None = None) -> tuple[int, str]:
    env = {**os.environ, "KESTREL_HEADLESS": "1", "PYTHONPATH": str(worktree)}
    env.pop("KESTREL_LIVE_BROWSER", None)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-rfE",
        "--tb=no",
        "-n",
        "0",
        "--timeout",
        "120",
        *(ids or files),
    ]
    proc = subprocess.run(
        cmd,
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def _make_worktree(sha: str, parent: Path, repo: Path) -> Path:
    path = parent / sha.replace("^", "-parent")[:14]
    _git("worktree", "add", "--detach", "--force", str(path), sha, cwd=repo)
    return path


def red_first_commit(
    sha: str, test_files: list[str], scratch: Path, repo: Path | None = None
) -> str | None:
    """Run one fix commit's tests on its parent. Returns None (proved) or the reason."""
    repo = repo or ROOT
    wt = _make_worktree(f"{sha}^", scratch, repo)
    try:
        _git("checkout", sha, "--", *test_files, cwd=wt)
        # Every changed file under tests/ is checked out (fixtures, maps, conftest), but only the
        # test modules are pytest targets: a .toml or .json target is a usage error (status 4).
        targets = [
            f
            for f in test_files
            if PurePosixPath(f).name.startswith("test_") and f.endswith(".py")
        ]
        code, out = _pytest(wt, targets)
        failing = failed_test_ids(out)
        fix_exit = None
        if code == 1 and failing:
            fixed = _make_worktree(sha, scratch / "fix", repo)
            try:
                fix_exit, _ = _pytest(fixed, test_files, ids=failing)
            finally:
                _git("worktree", "remove", "--force", str(fixed), cwd=repo)
        return red_first_verdict(code, fix_exit)
    finally:
        _git("worktree", "remove", "--force", str(wt), cwd=repo)


def red_first_problems(
    base: str, *, budget: float = RED_FIRST_BUDGET_SECONDS, log=print, repo: Path | None = None
) -> tuple[list[str], list[str]]:
    """(problems, warnings) for every `fix:` commit on base..HEAD that adds or changes tests."""
    repo = repo or ROOT
    note = public_export_note(repo)
    if note:
        return [], [note]
    revs = _git("rev-list", "--no-merges", "--reverse", f"{base}..HEAD", cwd=repo).stdout.split()
    started = time.monotonic()
    problems: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix="kestrel-redfirst-"))
    try:
        for sha in revs:
            subject = _git("show", "-s", "--format=%s", sha, cwd=repo).stdout.strip()
            if not is_fix_subject(subject):
                continue
            files = [
                f
                for f in _git(
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "--diff-filter=AM",
                    sha,
                    cwd=repo,
                ).stdout.splitlines()
                if f.startswith("tests/")
            ]
            tests = [
                f for f in files if PurePosixPath(f).name.startswith("test_") and f.endswith(".py")
            ]
            if not tests:
                log(f"    red-first {sha[:10]} {subject[:60]}: no test file changed, skipped")
                continue
            if time.monotonic() - started > budget:
                skipped.append(f"{sha[:10]} {subject[:60]}")
                continue
            reason = red_first_commit(sha, files, scratch, repo)
            if reason is None:
                log(f"    red-first {sha[:10]} {subject[:60]}: ok (a new test fails on the parent)")
            else:
                log(f"    red-first {sha[:10]} {subject[:60]}: FAILED")
                problems.append(
                    f"RULE red-first: commit {sha[:10]} ({subject[:70]}): {reason}. Fix: write a "
                    f"test that drives the real route or entry point and fails on the old code "
                    f"(run `git stash` or check out {sha[:10]}^ and run the test to see it fail), "
                    "then amend the commit; see D-TEST-6."
                )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        _git("worktree", "prune", cwd=repo)
    if skipped:
        warnings.append(
            f"WARNING red-first: stopped after the {budget:.0f}s budget; NOT checked: "
            + "; ".join(skipped)
            + ". Run them on a machine with time, or raise the budget."
        )
    return problems, warnings


# ---- git plumbing for the other three checks ---------------------------------------------------


def _show(base: str, path: str) -> str | None:
    proc = _git("show", f"{base}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _read(path: str) -> str | None:
    file = ROOT / path
    return file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None


def branch_messages(base: str) -> str:
    return _git("log", "--format=%B", f"{base}..HEAD").stdout


def _quality_lint(
    changed_lines: dict[str, set[int]],
    problems: list[str],
    warnings: list[str],
    *,
    base: str | None = None,
    history: bool = True,
) -> None:
    """Quality lint on the test files that changed. Without history (the public export) every
    changed test file is linted on its changed lines only, as an existing file."""
    for name in sorted(changed_lines):
        key = _norm(name)
        file_name = PurePosixPath(key).name
        if (
            key.startswith("tests/")
            and key.endswith(".py")
            and (file_name.startswith("test_") or file_name.endswith("_browser.py"))
        ):
            src = _read(key)
            if src is None:
                continue
            new_file = history and base is not None and _show(base, key) is None
            for finding in quality_findings(key, src, None if new_file else changed_lines[name]):
                (warnings if finding.warning else problems).append(str(finding))


def run_static(
    base: str, changed_lines: dict[str, set[int]], changed_names: list[str]
) -> tuple[list[str], list[str]]:
    """Floor, protected paths and quality lint against the real repository. On the public export
    only the quality lint runs (see public_export_note)."""
    problems: list[str] = []
    warnings: list[str] = []
    note = public_export_note()
    if note:
        warnings.append(note)
        _quality_lint(changed_lines, problems, warnings, history=False)
        return problems, warnings
    messages = branch_messages(base)

    # floor: every test file at the base, with renames followed
    status = _git("diff", "-M", "--name-status", base).stdout.splitlines()
    renames: dict[str, str] = {}
    for row in status:
        parts = row.split("\t")
        if parts[0].startswith("R") and len(parts) == 3:
            renames[_norm(parts[1])] = _norm(parts[2])
    base_files = [
        f
        for f in _git("ls-tree", "-r", "--name-only", base, "tests").stdout.splitlines()
        if f.endswith(".py")
    ]
    before: dict[str, str | None] = {}
    after: dict[str, str | None] = {}
    changed_set = {_norm(n) for n in changed_names} | set(renames)
    for f in base_files:
        key = _norm(f)
        if key not in changed_set and (ROOT / key).is_file():
            continue  # untouched file: its counts cannot have moved
        before[key] = _show(base, f)
        after[key] = _read(renames.get(key, key))
    justified = justified_paths(messages, list(before))
    removals_text = _read(REMOVALS_FILE)
    if removals_text:
        justified |= set(read_removals(removals_text)) & set(before)
    problems += floor_problems(before, after, justified)

    # protected paths (a deleted protected file also counts as changed)
    deleted = [r.split("\t")[1] for r in status if r.startswith("D\t")]
    texts = ("pyproject.toml", "tests/test_contracts.py")
    problems += protected_problems(
        sorted(set(changed_names) | set(deleted)),
        {t: _show(base, t) for t in texts},
        {t: _read(t) for t in texts},
        messages,
    )

    _quality_lint(changed_lines, problems, warnings, base=base)
    return problems, warnings


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import diff_gate  # noqa: PLC0415

    base, note = diff_gate.diff_base()
    if base is None:
        print(note)
        return 1
    names, lines = diff_gate.git_changes(base)
    problems, warnings = run_static(base, lines, names)
    for line in [*warnings, *problems]:
        print(line)
    print("test floor: " + ("FAILED" if problems else "ok"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
