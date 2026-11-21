"""Refuse a disk image whose hidden layout files carry a build path or account name (#198).

    python packaging/macos/dmg_privacy.py <file>... --forbid <text> [--forbid <text>]

Each file is read as bytes and searched for every forbidden text in UTF-8 and in UTF-16 (both
byte orders), the encodings .DS_Store and alias records use. Prints which file holds which kind
of value, never the value itself, and exits 1 on any hit, 0 when clean, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MIN_LENGTH = 3


def encodings(text: str) -> list[bytes]:
    return [text.encode("utf-8"), text.encode("utf-16-be"), text.encode("utf-16-le")]


def hits(data: bytes, forbidden: list[str]) -> list[int]:
    """Indexes into `forbidden` of every text found in `data`."""
    return [i for i, text in enumerate(forbidden) if any(e in data for e in encodings(text))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--forbid", action="append", default=[])
    args = parser.parse_args(argv)
    forbidden = [t for t in args.forbid if len(t.strip()) >= MIN_LENGTH]
    if not forbidden:
        print("dmg_privacy: nothing to check for", file=sys.stderr)
        return 2
    bad = 0
    for path in args.files:
        if not path.is_file():
            continue
        for index in hits(path.read_bytes(), forbidden):
            print(f"dmg_privacy: {path.name} contains forbidden value #{index + 1}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
