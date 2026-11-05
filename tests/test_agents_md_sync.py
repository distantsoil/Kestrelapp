"""CLAUDE.md says AGENTS.md is a verbatim copy of it for Warp, so a change to one is a change to
both. The two files may differ only in their first (title) line: the private repository titles
each file with its own name, and the public export gives AGENTS.md the CLAUDE.md title (a
sanitizer rule), so the same test holds in both repositories."""

from __future__ import annotations

import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agents_md_is_claude_md_apart_from_the_title_line():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert claude and agents
    assert claude[0].startswith("# CLAUDE.md")
    assert agents[0].startswith(("# AGENTS.md", "# CLAUDE.md"))
    assert agents[0].removeprefix("# AGENTS.md") in (
        claude[0].removeprefix("# CLAUDE.md"), claude[0]
    )
    diff = list(difflib.unified_diff(claude[1:], agents[1:], "CLAUDE.md", "AGENTS.md", lineterm="",
                                     n=0))
    assert not diff, "AGENTS.md has drifted from CLAUDE.md:\n" + "\n".join(diff[:40])
