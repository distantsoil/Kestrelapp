"""Ruff's security rules (S) run on the whole tree and nothing is accepted silently (D-CI-30).

Proof that the rules are live (a shell=True call is refused in app code) and that every
accepted finding in app code says why, on its own line.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_NOQA_S = re.compile(r"#\s*noqa:\s*(S\d{3}(?:\s*,\s*S\d{3})*)(.*)$")


def _ruff_stdin(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "concise",
         "--stdin-filename", filename, "-"],
        input=source, capture_output=True, text=True, encoding="utf-8", check=False, cwd=ROOT,
    )


def test_security_rules_are_selected() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "S" in config["tool"]["ruff"]["lint"]["select"]


def test_shell_true_in_app_code_is_refused() -> None:
    source = (
        "from __future__ import annotations\n\nimport subprocess\n\n\n"
        "def run(cmd: str) -> None:\n    subprocess.run(cmd, shell=True)\n"
    )
    proc = _ruff_stdin(source, "kestrel/example_module.py")
    assert proc.returncode != 0
    assert "S602" in proc.stdout


def test_a_reasoned_noqa_is_the_only_way_through() -> None:
    source = (
        "from __future__ import annotations\n\nimport subprocess\n\n\n"
        "def run(cmd: str) -> None:\n"
        "    subprocess.run(cmd, shell=True)  # noqa: S602  invented test reason\n"
    )
    assert _ruff_stdin(source, "kestrel/example_module.py").returncode == 0


_PRAGMA = re.compile(r"^(?:pyright|type|noqa|fmt)\s*:")


def _has_reason(before: str, after: str) -> bool:
    """A reason in words: text after the codes, or the comment just before the noqa. Another
    tool's pragma (`# pyright: ignore`, `# type: ignore`) is not a reason."""
    after_words = [seg.strip() for seg in after.split("#")]
    if any(seg and not _PRAGMA.match(seg) for seg in after_words):
        return True
    if "#" not in before:
        return False
    comment = before.rsplit("#", 1)[1].strip()
    return bool(comment) and not _PRAGMA.match(comment)


@pytest.mark.parametrize("line,ok", [
    ("x()  # noqa: S603  fixed argv", True),
    ("x()  # fixed argv  # noqa: S603", True),
    ("x()  # pyright: ignore  # noqa: S603", False),
    ("x()  # noqa: S603  # pyright: ignore[reportX]", False),
    ("x()  # noqa: S603", False),
    ("x()  # noqa: S606  opens a folder  # pyright: ignore[reportX]", True),
])
def test_a_pragma_is_not_a_reason(line, ok) -> None:
    match = _NOQA_S.search(line)
    assert _has_reason(line[: match.start()], match.group(2)) is ok


def test_every_security_noqa_in_app_code_carries_a_reason() -> None:
    silent = []
    for path in sorted((ROOT / "kestrel").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _NOQA_S.search(line)
            if not match:
                continue
            if not _has_reason(line[: match.start()], match.group(2)):
                silent.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert silent == [], "security noqa without a reason: " + ", ".join(silent)
