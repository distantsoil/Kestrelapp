"""Mutation testing on the lines this branch changed (INSTRUCTIONS.md 6.2). Pure Python: works on
Windows, macOS and Linux with no extra tools.

Run:  uv run --extra dev python scripts/soak_mutation.py [--max 200] [--out DIR] [--base REF]

How it works: it finds the lines changed under kestrel/ against the base (scripts/diff_gate.py),
copies the repository to a temp folder (the real tree is never edited), and for each changed line
makes ONE small change (flip a comparison, negate a condition, return None, delete a statement
in an if body, blank an error message), then runs only the tests that import that module. If
those tests still pass, no test noticed the change: that is a finding.

Interrupt it any time. Rerun with the same --out folder to continue where it stopped.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from soak_common import Finding, out_dir, verdict, write_findings  # noqa: E402

TOOL = "mutation"
COPY_IGNORE = (
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".claude",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    "data",
    "logs",
)

_FLIP = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


# ---------------------------------------------------------------------------- the mutator
@dataclass
class Site:
    """One place a single change can be made. `apply` mutates a node of a COPY of the tree."""

    lineno: int
    kind: str
    before: str
    after: str
    apply: Callable[[], None]

    @property
    def key(self) -> str:
        return hashlib.sha1(
            f"{self.lineno}|{self.kind}|{self.before}|{self.after}".encode()
        ).hexdigest()[:12]


def _src(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:80]


def sites(tree: ast.Module) -> list[Site]:
    """Every mutation site, in a fixed order (source order of the walk). The same call on a deep
    copy of the tree gives sites in the same order, so index i means the same change."""
    out: list[Site] = []
    def add(node, kind, before, after, fn):
        out.append(Site(node.lineno, kind, before, after, fn))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _FLIP:
            new = _FLIP[type(node.ops[0])]()

            def flip(n=node, new=new):
                n.ops[0] = new

            after = copy.deepcopy(node)
            after.ops[0] = new
            add(node, "compare", _src(node), _src(after), flip)
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):

            def negate(n=node):
                n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)

            add(node, "negate", _src(node.test), f"not ({_src(node.test)})", negate)
            if isinstance(node, ast.If):
                for i, stmt in enumerate(node.body):
                    if isinstance(stmt, ast.Pass):
                        continue

                    def drop(n=node, i=i):
                        n.body[i] = ast.copy_location(ast.Pass(), n.body[i])

                    add(stmt, "remove", _src(stmt), "pass", drop)
        elif isinstance(node, ast.Return) and node.value is not None:
            v = node.value
            if isinstance(v, ast.Constant) and v.value is None:
                continue
            repl: list[object] = [None]
            if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                repl = [not v.value]
            elif isinstance(v, ast.Constant) and isinstance(v.value, int):
                repl = [0 if v.value != 0 else 1]
            for r in repl:

                def ret(n=node, r=r):
                    n.value = ast.copy_location(ast.Constant(value=r), n.value)

                add(node, "return", _src(v), repr(r), ret)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for arg in node.exc.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:

                    def blank(a=arg):
                        a.value = ""

                    add(arg, "message", repr(arg.value[:60]), "''", blank)
                    break
    return out


def mutated_source(source: str, index: int) -> str:
    """The module's source with mutation site `index` applied (via a copy of the tree)."""
    tree = copy.deepcopy(ast.parse(source))
    sites(tree)[index].apply()
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def sites_on_lines(source: str, lines: set[int]) -> list[tuple[int, Site]]:
    """(index, site) for the sites whose line is in `lines`."""
    return [(i, s) for i, s in enumerate(sites(ast.parse(source))) if s.lineno in lines]


# ---------------------------------------------------------------------------- test selection
def dotted(rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def tests_for(rel: str, tests_dir: Path) -> list[Path]:
    """Test files that import the module (by dotted name, `from pkg import mod`, or the file
    path), plus ones named for it. Browser tests are left out: they need a browser and a server."""
    mod = dotted(rel)
    pkg, _, leaf = mod.rpartition(".")
    direct = re.compile(rf"\b{re.escape(mod)}\b")
    from_pkg = (
        re.compile(rf"from\s+{re.escape(pkg)}\s+import\s+(\([^)]*\)|[^\n]*)") if pkg else None
    )
    path_text = Path(rel).as_posix()
    named = {f"test_{leaf}.py", f"test_{mod.replace('.', '_')}.py"}
    found: list[Path] = []
    for f in sorted(tests_dir.glob("test_*.py")):
        if f.name.endswith("_browser.py"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = f.name in named or direct.search(text) or path_text in text
        if not hit and from_pkg:
            hit = any(
                re.search(rf"\b{re.escape(leaf)}\b", m.group(1)) for m in from_pkg.finditer(text)
            )
        if hit:
            found.append(f)
    return found


# ---------------------------------------------------------------------------- running tests
def prepare_checkout(dest: Path) -> None:
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(*COPY_IGNORE), dirs_exist_ok=True)


def run_tests(checkout: Path, tests: list[Path], timeout: float) -> tuple[str, str]:
    """('pass' | 'fail' | 'timeout', last output line). The temp copy is first on the path."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(checkout), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        *[str(Path("tests") / t.name) for t in tests],
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=checkout, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"no answer after {timeout:.0f} seconds"
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    return ("pass" if proc.returncode == 0 else "fail"), last


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": {}}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def finding_for(rel: str, site: Site) -> Finding:
    return Finding(
        f"{rel}:{site.lineno}",
        f"tests do not notice when this line changes from `{site.before}` to `{site.after}` "
        f"({site.kind})",
        "Add a test that asserts the observable result of this line (what a person or caller "
        "would see differently), and check it fails on the mutated line.",
    )


def plan(
    changed: dict[str, set[int]], root: Path
) -> tuple[list[tuple[str, int, Site]], list[Finding]]:
    """(file, index, site) to try in a stable order, and findings for untested modules."""
    todo: list[tuple[str, int, Site]] = []
    findings: list[Finding] = []
    for rel in sorted(changed):
        if not (rel.startswith("kestrel/") and rel.endswith(".py")):
            continue
        path = root / rel
        if not path.is_file():
            continue
        found = sites_on_lines(path.read_text(encoding="utf-8"), changed[rel])
        if not found:
            continue
        if not tests_for(rel, root / "tests"):
            findings.append(
                Finding(
                    rel,
                    "changed code that no test file imports",
                    "Write a test file that imports this module and asserts its behavior.",
                )
            )
            continue
        todo += [(rel, i, s) for i, s in found]
    return todo, findings


def run(args) -> int:
    import diff_gate

    path = out_dir(args.out, TOOL)
    if args.base:
        base = args.base
    else:
        base, note = diff_gate.diff_base()
        print(note)
        if base is None:
            print("SOAK SKIP: mutation: no origin/main or main to diff against; pass --base REF")
            return 0
    names, changed = diff_gate.git_changes(base)
    todo, findings = plan(changed, ROOT)
    state_file = path / "state.json"
    state = load_state(state_file)
    done: dict = state["done"]
    pending = [t for t in todo if f"{t[0]}:{t[2].key}" not in done]
    print(f"{len(todo)} mutants on changed lines, {len(todo) - len(pending)} already done.")
    batch = pending[: args.max]
    checkout = Path(tempfile.mkdtemp(prefix="kestrel-mutation-"))
    try:
        if batch:
            print("Copying the repository to a temp folder (the real tree is not touched)...")
            prepare_checkout(checkout)
        baseline: dict[str, bool] = {}
        for n, (rel, idx, site) in enumerate(batch, 1):
            tests = tests_for(rel, ROOT / "tests")
            if rel not in baseline:
                base_res, last = run_tests(checkout, tests, args.timeout * 4)
                baseline[rel] = base_res == "pass"
                if not baseline[rel]:
                    findings.append(
                        Finding(
                            rel,
                            "the tests that import this module fail before any change",
                            "Run those tests alone and fix them first; mutation results are void.",
                            f"{', '.join(t.name for t in tests)}: {last}",
                        )
                    )
            key = f"{rel}:{site.key}"
            if not baseline[rel]:
                done[key] = "skipped"
                continue
            target = checkout / rel
            original = (ROOT / rel).read_text(encoding="utf-8")
            target.write_text(mutated_source(original, idx), encoding="utf-8")
            try:
                result, _ = run_tests(checkout, tests, args.timeout)
            finally:
                target.write_text(original, encoding="utf-8")
            done[key] = "killed" if result in ("fail", "timeout") else "survived"
            mark = "killed" if done[key] == "killed" else "SURVIVED"
            print(f"[{n}/{len(batch)}] {rel}:{site.lineno} {site.kind}: {mark}", flush=True)
            if done[key] == "survived":
                state.setdefault("survivors", {})[key] = [
                    rel,
                    site.lineno,
                    site.kind,
                    site.before,
                    site.after,
                ]
            save_state(state_file, state)
    except KeyboardInterrupt:
        save_state(state_file, state)
        print(f"INTERRUPTED. Rerun with --out {path} to continue.")
        return 130
    finally:
        shutil.rmtree(checkout, ignore_errors=True)

    for _key, (rel, lineno, kind, before, after) in state.get("survivors", {}).items():
        findings.append(finding_for(rel, Site(lineno, kind, before, after, lambda: None)))
    left = len([t for t in todo if f"{t[0]}:{t[2].key}" not in done])
    counts = {k: sum(1 for v in done.values() if v == k) for k in ("killed", "survived", "skipped")}
    summary = [
        f"Base: {base[:10]}. Mutants: {len(todo)} planned, {counts['killed']} killed, "
        f"{counts['survived']} survived, {left} not run yet."
    ]
    if left:
        summary.append(f"Rerun with `--out {path}` to continue the remaining {left}.")
        print(summary[-1])
    write_findings(path, TOOL, findings, summary)
    return verdict(TOOL, findings)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mutation testing on lines changed against the base.")
    p.add_argument("--max", type=int, default=200, help="most mutants to run this time (200)")
    p.add_argument("--out", help="folder for findings and the resume state (default: new temp)")
    p.add_argument("--base", help="git ref to diff against (default: origin/main, else main)")
    p.add_argument("--timeout", type=float, default=300, help="seconds per mutant (300)")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse()))
