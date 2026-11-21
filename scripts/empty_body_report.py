"""Count new roles with no description, per source and week, from a COPY of the data file
(issue #182, D-SRC-650). Read only: the file is opened with SQLite's read-only mode, nothing is
written, and no row text is printed (only counts).

    python scripts/empty_body_report.py path/to/copy-of-kestrel.db

Use it to check whether a board started returning empty descriptions recently, and to measure
the thresholds in kestrel/ingest/empty_bodies.py against real runs. A role counts as empty when
its stored description has no visible text (the same structural test the runner uses)."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from kestrel.ingest.empty_bodies import MIN_SAMPLE, visible_text


def report(db_file: Path) -> list[tuple[str, str, int, int]]:
    """Rows of (week, source, roles, empty roles), oldest week first."""
    con = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT o.first_seen_at, s.name, s.kind, o.raw, o.description_md "
            "FROM opportunity o LEFT JOIN source s ON s.id = o.source_id").fetchall()
    finally:
        con.close()
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for seen, name, kind, raw, body in rows:
        try:
            week = datetime.fromisoformat(str(seen)).strftime("%G-W%V")
        except ValueError:
            continue
        site = ""
        if kind == "jobspy" and raw:
            try:
                site = str((json.loads(raw) or {}).get("site") or "")
            except (ValueError, AttributeError):
                site = ""
        key = (week, f"{name or 'unknown'}{' / ' + site if site else ''}")
        tally[key][0] += 1
        tally[key][1] += 0 if visible_text(body) else 1
    return [(w, s, n, e) for (w, s), (n, e) in sorted(tally.items())]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/empty_body_report.py <copy of the data file>")
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"not a file: {path}")
        return 2
    print("week     source                                   roles  empty  share")
    for week, source, n, e in report(path):
        share = "too few to tell" if n < MIN_SAMPLE else f"{e / n:.0%}"
        print(f"{week}  {source[:40]:40} {n:6} {e:6}  {share}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
