"""Print one version's section from a Keep a Changelog file.

Usage: python scripts/changelog_section.py <version> [--file CHANGELOG.md] [--allow-unreleased]

The release workflow uses this for the GitHub Release body; build_latest_json.py reuses
`list_sections` for the update feed. Exit 1 with a message on stderr when the section is missing
or empty.
`--allow-unreleased` lets a prerelease version (one containing "-") fall back to the `[Unreleased]`
section when it has no section of its own; a stable version never does.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\S+))?[ \t]*$")


def _split(text: str) -> list[tuple[str, str, str]]:
    """Every `## [x]` section as (version, date, body), in file order, Unreleased included."""
    found: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            found.append((match["version"].strip(), match["date"] or "", []))
        elif found:
            found[-1][2].append(line)
    return [(v.removeprefix("v"), d, "\n".join(body).strip()) for v, d, body in found]


def list_sections(text: str) -> list[tuple[str, str, str]]:
    """Released sections as (version, date, body), file order, `[Unreleased]` skipped."""
    return [s for s in _split(text) if s[0].lower() != "unreleased"]


def extract_section(text: str, version: str, *, allow_unreleased: bool = False) -> str | None:
    """The body of `## [version]`, stripped and without the heading; None when absent. With
    `allow_unreleased`, a prerelease version with no section of its own gets `[Unreleased]`."""
    wanted = version.removeprefix("v")
    sections = _split(text)
    for found, _date, body in sections:
        if found == wanted:
            return body
    if allow_unreleased and "-" in wanted:
        for found, _date, body in sections:
            if found.lower() == "unreleased":
                return body
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("--file", default="CHANGELOG.md")
    parser.add_argument(
        "--allow-unreleased",
        action="store_true",
        help="prereleases only: fall back to [Unreleased] when there is no exact section",
    )
    args = parser.parse_args(argv)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    body = extract_section(text, args.version, allow_unreleased=args.allow_unreleased)
    if body is None:
        print(f"{args.file} has no section '## [{args.version}]'", file=sys.stderr)
        return 1
    if not body:
        print(f"{args.file}: section '## [{args.version}]' is empty", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
