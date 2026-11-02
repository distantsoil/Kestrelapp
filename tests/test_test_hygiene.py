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


# ---- D-TEST-3: no "now -/+ a few minutes" seeds outside clock_seed ----------------------------
# A row seeded at `datetime.now(UTC) - timedelta(minutes=5)` and then counted as "today" or "this
# month" fails for the first minutes of every day or month. Use `clock_seed.ago_within`.

_SMALL_UNITS = {"minutes", "hours", "seconds", "milliseconds", "microseconds"}
_CLOCK_SEED_NAMES = {"ago_within", "frozen_datetime", "clock_seed"}

# (file, substring of the offending source line, reason). Only rows deliberately in the past or
# future that no day or month count reads. Keep this short.
SMALL_DELTA_ALLOWLIST: list[tuple[str, str, str]] = [
    ("test_run_summary.py", "lease_until=iso(datetime.now(UTC) + timedelta(minutes=5))",
     "live task lease five minutes ahead (future); no count reads it"),
    ("test_run_summary.py", "return iso(datetime.now(UTC) + timedelta(minutes=5))",
     "_live() lease five minutes ahead (future); no count reads it"),
    ("test_runs_browser.py", "task.lease_until = iso(datetime.now(UTC) + timedelta(minutes=5))",
     "task lease five minutes ahead (future); no count reads it"),
    ("test_runs_browser.py",
     "run.started_at = iso(datetime.now(UTC) - timedelta(minutes=minutes_ago))",
     "elapsed minutes on a running run's card, set exactly; clamping would change the minutes"),
    ("rescoring_helpers.py", "until = iso(", "lease expiry five minutes ahead; no count reads it"),
    ("runs_seed.py", "now - timedelta(hours=hours)", "history rows are 26 hours or older"),
    ("test_intelligence_liveness.py", "_hours = (now - timedelta(hours=",
     "liveness recheck cadence in hours; no day count"),
    ("test_needs_try.py", "created_at=iso(datetime.now(UTC) + timedelta(minutes=",
     "evaluation or failure made newer than a quarantine row (future); no day count reads it"),
    ("test_run_model_choice.py", "timedelta(minutes=5)", "run drawer span; no day count reads it"),
    ("test_runs_browser.py", "timedelta(seconds=30)", "scrape duration of 30 s; no day count"),
    ("test_scan_activity.py", "timedelta(", "lease and hold windows around now; no day count"),
    ("test_sources_browser.py", "timedelta(hours=", "relative 'hours ago' labels on source rows"),
    ("test_surfaces.py", "timedelta(**k)", "relative_time check against a fixed 2026-10-24 now"),
    ("test_surfaces.py", "timedelta(days=1)", "next action dated yesterday: overdue at any hour"),
    ("test_updater.py", "timedelta(hours=", "update reminder in the future; no day count"),
]


def _small_timedelta(node: ast.AST) -> bool:
    """A `timedelta(...)` call that spans under two days or is hours/minutes/seconds based."""
    if not (isinstance(node, ast.Call) and (
            getattr(node.func, "id", None) == "timedelta"
            or getattr(node.func, "attr", None) == "timedelta")):
        return False
    if len(node.args) >= 2:  # timedelta(days, seconds, ...)
        return True
    for kw in node.keywords:
        if kw.arg is None:  # timedelta(**kw): a helper that takes any unit, so assume small
            return True
        if kw.arg in _SMALL_UNITS:
            return True
        if kw.arg == "days":
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)):
                if v.value < 2:
                    return True
            elif isinstance(v, ast.UnaryOp) or not isinstance(v, ast.Constant):
                return False  # a variable or expression: cannot tell, not flagged
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)) and arg.value < 2:
            return True
    return False


def _is_now_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"now", "utcnow"}
            and getattr(node.func.value, "id", getattr(node.func.value, "attr", None))
            in {"datetime", "dt"})


def _now_names(tree: ast.Module) -> set[str]:
    """Names assigned straight from `datetime.now(...)` anywhere in the file."""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _is_now_call(n.value):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and n.value is not None and _is_now_call(n.value):
            if isinstance(n.target, ast.Name):
                names.add(n.target.id)
    return names


def small_delta_clock_seeds(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    """(line, source line) of each `now +/- small timedelta` not routed through clock_seed."""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    now_names = _now_names(tree)
    parents: dict[int, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p

    def is_now(e: ast.AST) -> bool:
        return _is_now_call(e) or (isinstance(e, ast.Name) and e.id in now_names)

    def via_clock_seed(n: ast.AST) -> bool:
        cur: ast.AST | None = n
        while cur is not None and not isinstance(cur, ast.stmt):
            if isinstance(cur, ast.Call):
                f = cur.func
                if (getattr(f, "id", None) in _CLOCK_SEED_NAMES
                        or getattr(f, "attr", None) in _CLOCK_SEED_NAMES):
                    return True
            cur = parents.get(id(cur))
        return False

    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)):
            if ((is_now(n.left) and _small_timedelta(n.right))
                    or (is_now(n.right) and _small_timedelta(n.left))):
                if not via_clock_seed(n):
                    hits.append((n.lineno, lines[n.lineno - 1].strip()))
    return hits


def test_no_test_seeds_now_minus_a_small_delta_outside_clock_seed():
    offenders = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == "clock_seed.py":
            continue
        rel = path.relative_to(TESTS).as_posix()
        for line, text in small_delta_clock_seeds(path.read_text(encoding="utf-8"), rel):
            if any(f == rel and sub in text and why for f, sub, why in SMALL_DELTA_ALLOWLIST):
                continue
            offenders.append(f"{rel}:{line}: {text}")
    assert not offenders, (
        "now -/+ a few minutes crosses the day or month a test counts it in (D-TEST-2/3). Seed "
        "with clock_seed.ago_within(...), or allowlist a row no day/month count reads, with a "
        "reason, in SMALL_DELTA_ALLOWLIST:\n" + "\n".join(offenders)
    )


def test_the_small_delta_guard_flags_and_clears_the_right_shapes():
    flag = small_delta_clock_seeds
    assert flag("x = datetime.now(UTC) - timedelta(minutes=5)")
    assert flag("now = datetime.now(UTC)\ny = now - timedelta(hours=3)")
    assert flag("now = datetime.now(UTC)\ny = timedelta(seconds=9) + now")
    assert flag("x = datetime.now(UTC) - timedelta(days=1)")
    assert flag("x = datetime.now(UTC) - timedelta(days=0.1)")
    assert flag("def _ago(**kw):\n    return datetime.now(UTC) - timedelta(**kw)")
    assert not flag("x = datetime.now(UTC) - timedelta(days=9)")
    assert not flag("x = other - timedelta(minutes=5)")
    assert not flag("x = ago_within(timedelta(minutes=5), s, horizon=h)")
    assert not flag("x = ago_within(datetime.now(UTC) - timedelta(minutes=5), s)")
