"""Shared plumbing for the soak tools (INSTRUCTIONS.md 6.2): the findings record, the output
folder, and the final verdict line. Works the same on Windows, macOS and Linux."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """One thing a person or a weaker model can act on. `where` names the file or prompt,
    `rule` the rule that was broken, `action` the next step in one plain sentence."""

    where: str
    rule: str
    action: str
    detail: str = ""


def out_dir(requested: str | None, tool: str) -> Path:
    """The folder the person named, created if needed; else a new folder under the system temp
    directory. The path is printed so nobody has to hunt for it."""
    if requested:
        path = Path(requested)
        path.mkdir(parents=True, exist_ok=True)
    else:
        path = Path(tempfile.mkdtemp(prefix=f"kestrel-soak-{tool}-"))
    print(f"Findings folder: {path}")
    return path


def write_findings(
    path: Path, tool: str, findings: list[Finding], summary_lines: list[str]
) -> None:
    """findings.json (machine readable) and findings.md (short summary) in `path`."""
    rows = [asdict(f) for f in findings]
    (path / "findings.json").write_text(
        json.dumps({"tool": tool, "count": len(rows), "findings": rows}, indent=2) + "\n",
        encoding="utf-8",
    )
    md = [f"# Soak findings: {tool}", "", *summary_lines, ""]
    if findings:
        md.append(f"## {len(findings)} finding(s)")
        md.append("")
        for i, f in enumerate(findings, 1):
            md.append(f"{i}. **{f.where}**: {f.rule}. Next: {f.action}")
            if f.detail:
                md.append(f"   - {f.detail}")
    else:
        md.append("No findings.")
    (path / "findings.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def verdict(tool: str, findings: list[Finding]) -> int:
    """Print the final line and return the process exit status (0 on pass)."""
    if findings:
        print(f"SOAK FAIL: {tool}: {len(findings)} findings")
        return 1
    print(f"SOAK PASS: {tool}")
    return 0
