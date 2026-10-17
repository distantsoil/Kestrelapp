"""Guard: no test module reads the clock at import time.

A module-level `NOW = datetime.now()` freezes at collection, so a test that runs after midnight
(or a day-boundary in CI's timezone) compares against a stale date. Compute the value inside a
function called at use time instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

CLOCK_CALLS = {
    ("datetime", "now"), ("datetime", "utcnow"), ("date", "today"), ("time", "time"),
    ("datetime", "today"),
}
TESTS = Path(__file__).parent


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        # datetime.now(), datetime.datetime.now(), date.today(), time.time()
        owner = f.value
        name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
        return (name, f.attr) in CLOCK_CALLS
    return False


def _import_time_clock_reads(tree: ast.Module) -> list[int]:
    """Line numbers of top-level assignments whose value contains a clock call. Function and
    lambda bodies are skipped (they run at use time); class bodies and default args run at
    import and are checked."""
    hits: list[int] = []

    def visit(node: ast.AST) -> bool:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        if _is_clock_call(node):
            return True
        return any(visit(child) for child in ast.iter_child_nodes(node))

    def scan(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                if stmt.value is not None and visit(stmt.value):
                    hits.append(stmt.lineno)
            elif isinstance(stmt, ast.ClassDef):
                scan(stmt.body)
            elif isinstance(stmt, (ast.If, ast.Try, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    scan(getattr(stmt, field, []))

    scan(tree.body)
    return hits


def test_no_test_module_reads_the_clock_at_import_time():
    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{path.name}:{line}" for line in _import_time_clock_reads(tree)]
    assert not offenders, (
        "module-level clock reads freeze at collection; make them functions called at use "
        f"time: {offenders}"
    )


def test_the_guard_catches_a_clock_read_and_ignores_function_bodies():
    bad = ast.parse("import datetime\nNOW = datetime.datetime.now()\nT: float = time.time()\n")
    assert _import_time_clock_reads(bad) == [2, 3]
    ok = ast.parse("def now():\n    return datetime.now()\nX = lambda: date.today()\n")
    assert _import_time_clock_reads(ok) == []
