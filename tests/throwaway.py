"""The mark that says a data directory is a throwaway one. The `_isolate_data_home` fixture
(tests/conftest.py) writes it into every per-test data home; anything that deletes data
(tests/retry_seed.py) refuses a directory without it, so it cannot wipe a real database even
when run outside pytest with KESTREL_DATA_HOME pointing at one."""

from __future__ import annotations

import os
from pathlib import Path

from kestrel import paths

MARKER = ".kestrel-throwaway"


def mark(home: Path) -> None:
    (home / MARKER).write_text("A test data directory. Safe to delete.\n", encoding="utf-8")


def is_throwaway(home: Path) -> bool:
    return (home / MARKER).is_file()


def real_data_home() -> Path:
    """The operator's real data home (resolved), ignoring KESTREL_DATA_HOME. A seam for tests."""
    return paths.default_data_home().resolve()


def require_throwaway_home(who: str) -> None:
    """Raise unless KESTREL_DATA_HOME is set, is a marked throwaway directory, is not the real data
    home or inside it, and holds paths.db_path(). Call it FIRST in anything that deletes or
    rebuilds data: the autouse pytest fixture protects only code running under pytest, and a seed
    helper run from a plain shell once wiped a real database."""
    raw = os.environ.get("KESTREL_DATA_HOME")
    if not raw:
        raise RuntimeError(
            f"{who} refuses to run: KESTREL_DATA_HOME is not set, so it would act on the real "
            "data directory. Run it under pytest, or point KESTREL_DATA_HOME at a throwaway "
            "directory marked with tests.throwaway.mark()."
        )
    home = Path(raw).resolve()
    real = real_data_home()
    if home == real or real in home.parents:
        raise RuntimeError(
            f"{who} refuses to run: KESTREL_DATA_HOME ({home}) is the real data directory or "
            "inside it."
        )
    if home not in paths.db_path().resolve().parents or not is_throwaway(home):
        raise RuntimeError(
            f"{who} refuses to run: KESTREL_DATA_HOME ({home}) is not a marked throwaway "
            "directory holding the database (tests.throwaway.mark)."
        )
