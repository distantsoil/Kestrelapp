"""The stress run (#188, D-CI-26, D-CI-29): the tests a branch affects, under the conditions that
broke them in CI and on the owner's PC but never in the serial Linux gate.

    uv run --extra dev python scripts/check.py --stress   # only this (also part of the full gate)

Every test file affected by the branch's changes runs, with `-n 4`:
1. once in a random order (tests/stress_order.py), timed;
2. once with port 8765 (the first rung of the app's port ladder) held by a listener and a fast
   clock: each module's clock alias runs CLOCK_RATE times real speed, so the time between seeding a
   row and requesting the page is what a slow machine would see;
3. for files that seed "minutes ago" rows (they import tests/clock_seed.py), once with the clock
   started at 00:00:39 UTC and once at 23:59:30 UTC (the Windows CI root cause in 0.7.6);
4. again in new random orders, up to five orders in all, while the time budget allows. A narrow
   branch gets all five; a branch that touches half the app gets fewer, and the gate says so.

These are the 0.7.6 failures that passed alone and failed in parallel, on Windows or on a slow
machine. Affected files: changed test files; test files that name a changed kestrel module (its
dotted name anywhere, including monkeypatch strings, or `from parent import leaf`); test files
that use a changed script, test helper or test module (`from tests import x`, `import x`,
`tests.x`, `x.py`); for a changed package `__init__.py`, the files that import names defined in it
rather than its submodules; for a changed non-Python file under kestrel/ (a template, a prompt),
the test files of the modules that name it. tests/conftest.py selects every file. Browser files
are left to the browser stage.

Everything here except `PortHolder` and `read_texts` is pure, so tests/test_stress_run.py can test
it without git or a subprocess.
"""

from __future__ import annotations

import ast
import errno
import random
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPEATS = 5
WORKERS = 4
HELD_PORT = 8765
CLOCK_RATE = 10
# The stress run starts no new random-order repeat once it has used this much (D-CI-29). The
# first order, the port-and-clock run and the midnight runs always run.
BUDGET_S = 20 * 60
# Test helpers every test file loads (pytest plugins and fixtures): a change to one stresses the
# whole non-browser suite, since any file may depend on it.
SHARED_PLUMBING = frozenset({"tests/conftest.py"})
# Files that run their own child pytest (opt-in, skipped here) or need a browser.
NOT_STRESSED = ("_browser.py",)
NOT_STRESSED_FILES = frozenset({"tests/test_clock_boundaries.py"})
CLOCK_SEED_USE = re.compile(r"\bclock_seed\b")

Matcher = Callable[[str], bool]


@dataclass
class StressRun:
    label: str
    files: list[str]
    env: dict[str, str] = field(default_factory=dict)
    hold_port: bool = False
    repeat: bool = False  # a further random order, run only while the budget allows


def _is_test_file(name: str) -> bool:
    path = Path(name)
    return (path.parts[:1] == ("tests",) and path.name.startswith("test_")
            and path.suffix == ".py" and not path.name.endswith(NOT_STRESSED)
            and name not in NOT_STRESSED_FILES)


def _from_import(parent: str, leaf: str) -> re.Pattern[str]:
    """`from parent import leaf`, on one line or inside parentheses."""
    p, leaf_re = re.escape(parent), re.escape(leaf)
    return re.compile(rf"from\s+{p}\s+import\s+(?:\([^)]*?\b{leaf_re}\b|[^\n(]*?\b{leaf_re}\b)")


def _searcher(pattern: re.Pattern[str]) -> Matcher:
    return lambda text: pattern.search(text) is not None


_FROM_IMPORT_NAMES = re.compile(r"from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n]*)")


def _imports_names_from(pkg: str, names: frozenset[str]) -> Matcher:
    """A test file that imports, from package `pkg`, a name its __init__.py defines (not one of
    its submodules), or reads `pkg.__version__`-style attributes, or does a bare `import pkg`."""
    bare = re.compile(rf"^\s*import\s+{re.escape(pkg)}\s*(?:as\s+\w+\s*)?$", re.M)
    dunder = re.compile(rf"\b{re.escape(pkg)}\.__\w+__\b")

    def match(text: str) -> bool:
        if bare.search(text) or dunder.search(text):
            return True
        for m in _FROM_IMPORT_NAMES.finditer(text):
            if m.group(1) != pkg:
                continue
            imported = {w.split(" as ")[0].strip() for w in m.group(2).strip("()").split(",")}
            if imported & names:
                return True
        return False

    return match


def init_names(source: str) -> frozenset[str]:
    """Names a package __init__.py defines or imports at top level (what `from pkg import x`
    can mean other than a submodule)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset({"*"})
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return frozenset(names)


def _matchers(name: str, app_texts: dict[str, str]) -> list[Matcher]:
    """How a test file refers to the changed file `name`."""
    path = Path(name)
    if path.suffix != ".py":
        return []
    parts = list(path.with_suffix("").parts)
    if parts[0] == "kestrel":
        if parts[-1] == "__init__":
            pkg = ".".join(parts[:-1])
            return [_imports_names_from(pkg, init_names(app_texts.get(name, "")))]
        dotted = ".".join(parts)
        matchers = [_searcher(re.compile(rf"\b{re.escape(dotted)}\b"))]
        if len(parts) > 1:
            matchers.append(_searcher(_from_import(".".join(parts[:-1]), parts[-1])))
        return matchers
    if parts[0] in {"scripts", "tests"} and len(parts) == 2:
        # A path or file name ("scripts/check.py", "check.py"), a dotted name ("tests.x"), a
        # plain or package import; never the bare word, which for check.py matches everything.
        stem, top = re.escape(path.stem), parts[0]
        return [_searcher(re.compile(rf"\b{stem}\.py\b|\b{top}\.{stem}\b"
                                     rf"|^\s*(?:from|import)\s+{stem}\b", re.M)),
                _searcher(_from_import(top, path.stem))]
    return []


def _non_python_users(name: str, app_texts: dict[str, str]) -> list[str]:
    """kestrel/ modules that name a changed non-Python file (a template by its name, a prompt or
    data file by its file name)."""
    base = re.compile(rf"\b{re.escape(Path(name).name)}\b")
    return sorted(m for m, text in app_texts.items()
                  if m.endswith(".py") and m != name and base.search(text))


def affected_test_files(changed: list[str], test_texts: dict[str, str],
                        app_texts: dict[str, str] | None = None) -> list[str]:
    """The test files to stress, sorted. `test_texts` maps each test file (repo-relative, forward
    slashes) to its source; `app_texts` does the same for kestrel/."""
    app_texts = app_texts or {}
    candidates = sorted(n for n in test_texts if _is_test_file(n))
    if any(n in SHARED_PLUMBING for n in changed):
        return candidates
    sources = set(changed)
    for n in changed:
        if n.startswith("kestrel/") and not n.endswith(".py"):
            sources.update(_non_python_users(n, app_texts))
    chosen = {n for n in sources if n in test_texts and _is_test_file(n)}
    matchers = [m for n in sorted(sources) for m in _matchers(n, app_texts)]
    for name in candidates:
        if name not in chosen and any(m(test_texts[name]) for m in matchers):
            chosen.add(name)
    return sorted(chosen)


def clock_seeding(files: list[str], test_texts: dict[str, str]) -> list[str]:
    """The files that seed "minutes ago" rows through tests/clock_seed.py."""
    return [n for n in files if CLOCK_SEED_USE.search(test_texts.get(n, ""))]


def recent_instants(now: datetime) -> dict[str, datetime]:
    """The most recent past 00:00:39 and 23:59:30 UTC (a future instant skews code that also reads
    the real clock)."""
    now = now.astimezone(UTC)
    out: dict[str, datetime] = {}
    for label, (h, m, s) in (("00:00:39 UTC", (0, 0, 39)), ("23:59:30 UTC", (23, 59, 30))):
        at = now.replace(hour=h, minute=m, second=s, microsecond=0)
        if at > now:
            at -= timedelta(days=1)
        out[label] = at
    return out


def plan(files: list[str], seeding: list[str], *, now: datetime,
         seeds: list[int] | None = None) -> list[StressRun]:
    """The runs, in order. `seeds` fixes the random orders (tests); by default each order draws
    one, printed in the run's label so a failure can be replayed with KESTREL_STRESS_SEED."""
    if not files:
        return []
    seeds = seeds or [random.SystemRandom().randrange(1, 2**31) for _ in range(REPEATS)]
    order = [StressRun(f"random order {i + 1} of {REPEATS}, seed {seed}", files,
                       {"KESTREL_STRESS_SEED": str(seed)}, repeat=i > 0)
             for i, seed in enumerate(seeds[:REPEATS])]
    runs = [order[0],
            StressRun(f"port {HELD_PORT} held, clock at {CLOCK_RATE}x real speed", files,
                      {"KESTREL_STRESS_SEED": str(seeds[0]),
                       "KESTREL_CLOCK_RATE": str(CLOCK_RATE)}, hold_port=True)]
    if seeding:
        for label, at in recent_instants(now).items():
            runs.append(StressRun(f"clock from {label}", seeding,
                                  {"KESTREL_FROZEN_AT": at.isoformat()}))
    return runs + order[1:]


def budget_allows(elapsed_s: float, first_run_s: float, budget_s: float | None = None) -> bool:
    """Whether another random-order repeat (about as long as the first run) fits the budget
    (BUDGET_S, read at call time)."""
    return elapsed_s + first_run_s <= (BUDGET_S if budget_s is None else budget_s)


def pytest_args(python: str, run: StressRun) -> list[str]:
    plugins = ["-p", "tests.stress_order"]
    if "KESTREL_CLOCK_RATE" in run.env or "KESTREL_FROZEN_AT" in run.env:
        plugins += ["-p", "tests.boundary_clock"]
    return [python, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *plugins,
            "-n", str(WORKERS), "--dist", "load", "--max-worker-restart", "0", *run.files]


class PortHolder:
    """Holds HELD_PORT on loopback while a run executes, the way the running app would. If the
    port is already taken (the app itself is running), it is held already, which is the point."""

    def __init__(self, port: int = HELD_PORT) -> None:
        self.port = port
        self.sock: socket.socket | None = None
        self.already_taken = False

    def __enter__(self) -> PortHolder:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
            sock.listen(1)
        except OSError as exc:
            sock.close()
            if exc.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                raise
            self.already_taken = True
            return self
        self.sock = sock
        return self

    def __exit__(self, *exc: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def read_texts(folder: str, root: Path = ROOT) -> dict[str, str]:
    """{repo-relative path: text} for the .py files under `folder`."""
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted((root / folder).rglob("*.py")) if "__pycache__" not in p.parts}


def read_test_texts(root: Path = ROOT) -> dict[str, str]:
    return read_texts("tests", root)
