"""The whole soak tier in one command (INSTRUCTIONS.md 6.2). Works on Windows, macOS and Linux.

Run:  uv run --extra dev python scripts/soak.py [--only STEP] [--out DIR] [--max N]

Steps, in order:
  browser    the full browser suite (KESTREL_LIVE_BROWSER=1, one test at a time)
  crawl      the layout crawl (KESTREL_LAYOUT_CRAWL=1), if its test file exists
  random     the test files changed on this branch, three times, in a random order
  prompts    real-model prompt evals (skipped with a line if Ollama is not reachable)
  mutation   mutation testing on changed lines

Ends with a table and `SOAK PASS: soak` or `SOAK FAIL: soak: N findings` (N = failed steps).
Each step's own findings are in its folder under --out (default: a new temp folder).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from soak_common import out_dir  # noqa: E402

STEPS = ("browser", "crawl", "random", "prompts", "mutation")
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
CRAWL = ROOT / "tests" / "test_layout_crawl_browser.py"


@dataclass
class Result:
    step: str
    status: str  # PASS, FAIL or SKIP
    note: str
    seconds: float


def stream(cmd: list[str], env_extra: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """Run a command, show its output as it arrives, return (exit code, all output lines)."""
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line.rstrip("\n"))
    return proc.wait(), lines


def pytest_verdict(code: int, lines: list[str]) -> tuple[str, str]:
    """Pass only on the words: exit 0 AND a final line that says passed, with nothing failed,
    errored or (for a whole step) skipped without a pass."""
    last = next((x for x in reversed(lines) if x.strip()), "")
    if code == 0 and re.search(r"\d+ passed", last) and not re.search(r"failed|error", last):
        return "PASS", last.strip(" =")
    if code == 0 and "skipped" in last:
        return "FAIL", f"nothing ran, everything was skipped: {last.strip(' =')}"
    return "FAIL", last.strip(" =") or f"pytest exited {code}"


def tool_verdict(code: int, lines: list[str], tool: str) -> tuple[str, str]:
    """Read a soak tool's own final line (SOAK PASS / SOAK FAIL / SOAK SKIP)."""
    last = next((x for x in reversed(lines) if x.startswith("SOAK ")), "")
    if last.startswith("SOAK PASS"):
        return "PASS", ""
    if last.startswith("SOAK SKIP"):
        return "SKIP", last.split(":", 2)[-1].strip()
    if last.startswith("SOAK FAIL"):
        return "FAIL", last.split(":", 2)[-1].strip()
    return "FAIL", f"{tool} ended without a SOAK line (exit {code})"


def changed_test_files() -> list[str]:
    import diff_gate

    base, _ = diff_gate.diff_base()
    if base is None:
        return []
    names, _ = diff_gate.git_changes(base)
    return sorted(
        n
        for n in names
        if re.fullmatch(r"tests/test_[\w]+\.py", n) and not n.endswith("_browser.py")
    )


def run_step(step: str, args, root_out: Path) -> Result:
    start = time.monotonic()
    live = {"KESTREL_LIVE_BROWSER": "1"}
    if step == "browser":
        files = sorted(
            str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("*_browser.py") if p != CRAWL
        )
        code, lines = stream([*PYTEST, *files], live)
        status, note = pytest_verdict(code, lines)
    elif step == "crawl":
        if not CRAWL.exists():
            return Result(step, "SKIP", "tests/test_layout_crawl_browser.py does not exist", 0)
        code, lines = stream(
            [*PYTEST, str(CRAWL.relative_to(ROOT))], {**live, "KESTREL_LAYOUT_CRAWL": "1"}
        )
        status, note = pytest_verdict(code, lines)
    elif step == "random":
        files = changed_test_files()
        if not files:
            return Result(step, "SKIP", "no test files changed on this branch", 0)
        have_randomly = importlib.util.find_spec("pytest_randomly") is not None
        status, note = "PASS", f"{len(files)} file(s) x 3 orders"
        for seed in (1, 2, 3):
            if have_randomly:
                cmd, env = [*PYTEST, f"--randomly-seed={seed}", *files], {}
            else:
                cmd = [*PYTEST, "-p", "soak_shuffle_plugin", *files]
                env = {
                    "SOAK_SHUFFLE_SEED": str(seed),
                    "PYTHONPATH": os.pathsep.join(
                        [str(ROOT / "scripts"), os.environ.get("PYTHONPATH", "")]
                    ).rstrip(os.pathsep),
                }
            code, lines = stream(cmd, env)
            status, note = pytest_verdict(code, lines)
            if status != "PASS":
                note = f"order seed {seed} failed: {note}. Rerun with the same seed to reproduce"
                break
    else:
        script = "soak_prompts.py" if step == "prompts" else "soak_mutation.py"
        cmd = [sys.executable, str(ROOT / "scripts" / script), "--out", str(root_out / step)]
        if step == "mutation":
            cmd += ["--max", str(args.max)]
        code, lines = stream(cmd)
        status, note = tool_verdict(code, lines, step)
    return Result(step, status, note, time.monotonic() - start)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the soak tier.")
    p.add_argument("--only", choices=STEPS, help="run one step")
    p.add_argument("--out", help="folder for each step's findings (default: new temp)")
    p.add_argument("--max", type=int, default=200, help="most mutants (mutation step, 200)")
    args = p.parse_args(argv)
    root_out = out_dir(args.out, "all")
    results: list[Result] = []
    for step in [args.only] if args.only else STEPS:
        print(f"\n===== soak step: {step} =====", flush=True)
        results.append(run_step(step, args, root_out))
        print(f"----- {step}: {results[-1].status} {results[-1].note}", flush=True)
    print("\nstep       result  time     note")
    for r in results:
        print(f"{r.step:<10} {r.status:<7} {r.seconds:>5.0f}s   {r.note}")
    failed = [r for r in results if r.status == "FAIL"]
    print(f"Findings folders are under {root_out}")
    if failed:
        print(f"SOAK FAIL: soak: {len(failed)} findings")
        return 1
    print("SOAK PASS: soak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
