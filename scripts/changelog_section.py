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
from datetime import UTC, date, datetime
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


def drop_empty_unreleased(text: str) -> str:
    """The changelog without an empty `## [Unreleased]` section (D-UPD-12). Empty means no line
    under the heading other than blanks and `###` sub-headings. A section with entries, and a file
    with no such heading, come back unchanged."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln.rstrip("\r\n"))
        if m and m["version"].strip().lower() == "unreleased":
            start = i
            break
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if any(ln.strip() and not ln.startswith("###") for ln in lines[start + 1 : end]):
        return text
    return "".join(lines[:start] + lines[end:])


def _utc_day(stamp: str) -> date | None:
    """The UTC calendar day of an ISO timestamp or date; a timestamp with an offset is converted
    first, and one without is read as UTC. None when it does not parse."""
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", stamp):
            return date.fromisoformat(stamp)
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC).date()


def date_problem(text: str, version: str, released_at: str) -> str | None:
    """None when `## [version]` is dated the UTC day of `released_at`; otherwise one sentence
    saying why not (D-UPD-12). Works on the heading's own date, so a missing date is a problem."""
    wanted = version.removeprefix("v")
    dated = next((d for v, d, _ in list_sections(text) if v == wanted), None)
    if dated is None:
        return f"CHANGELOG has no section for {wanted}"
    if not dated:
        return f"CHANGELOG heading for {wanted} has no date"
    day = _utc_day(released_at)
    if day is None:
        return f"cannot read the release timestamp {released_at!r}"
    if dated != day.isoformat():
        return (
            f"CHANGELOG dates {wanted} {dated} but the release is dated {day.isoformat()} (UTC);"
            " make them the same day"
        )
    return None


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
    parser.add_argument(
        "--check-date",
        metavar="TIMESTAMP",
        help="instead of printing the section, fail unless its heading date is that UTC day",
    )
    args = parser.parse_args(argv)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    if args.check_date is not None:
        problem = date_problem(text, args.version, args.check_date)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        return 0
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
