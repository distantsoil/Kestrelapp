"""pytest plugin: shuffle the collected tests with a fixed seed (the stress run, D-CI-26).

Loaded as `-p tests.stress_order` with `KESTREL_STRESS_SEED` set; without the variable it does
nothing. Every xdist worker collects the same files and draws the same order from the seed, so
the workers agree on the collection (xdist refuses to run when they differ). Replay a failure with
the seed the stress run printed:

    KESTREL_STRESS_SEED=<seed> uv run --extra dev pytest -p tests.stress_order -n 4 <files>
"""

from __future__ import annotations

import os
import random


def _seed() -> int | None:
    raw = os.environ.get("KESTREL_STRESS_SEED", "").strip()
    return int(raw) if raw.isdigit() else None


def pytest_report_header(config) -> str | None:  # noqa: ANN001
    seed = _seed()
    return None if seed is None else f"stress order seed: {seed}"


def pytest_collection_modifyitems(session, config, items) -> None:  # noqa: ANN001
    seed = _seed()
    if seed is not None:
        random.Random(seed).shuffle(items)  # noqa: S311  test order, not security
