"""Write the error code help table from the registry (D-ERR-1, D-ERR-2).

    uv run python scripts/error_codes_table.py            # writes docs/guide/error-codes.md
    uv run python scripts/error_codes_table.py OUT.md     # writes OUT.md instead

`tests/test_error_codes.py` fails when the committed table differs from the registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "docs" / "guide" / "error-codes.md"


def main(argv: list[str] | None = None) -> int:
    from kestrel.error_codes import render_table

    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else TABLE
    target.write_text(render_table(), encoding="utf-8", newline="\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
