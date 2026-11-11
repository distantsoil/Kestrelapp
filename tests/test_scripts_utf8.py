"""Every script that reads a command's output as text names UTF-8, so a Windows machine never
decodes git's output with its default code page (cp1252) and crashes on a byte it cannot map.
Found on the owner's Windows PC, 2026-11-11: scripts/diff_gate.py got None back from `git diff`
and the quick gate stopped with AttributeError."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text_calls_without_utf8() -> list[str]:
    bad: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            text = kw.get("text")
            if not (isinstance(text, ast.Constant) and text.value is True):
                continue
            enc = kw.get("encoding")
            if not (isinstance(enc, ast.Constant) and str(enc.value).lower() in ("utf-8", "utf8")):
                bad.append(f"{path.name}:{node.lineno}")
    return bad


def test_every_text_subprocess_call_in_scripts_decodes_utf8():
    assert _text_calls_without_utf8() == [], (
        "pass encoding='utf-8' (and errors='replace' when the output is only read) to every "
        "subprocess call with text=True in scripts/")
