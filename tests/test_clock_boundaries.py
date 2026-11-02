"""Opt-in boundary run (D-TEST-3): re-run the "today" / "this month" modules with the clock set
one to three minutes past midnight. Enabled by `KESTREL_BOUNDARY=1`; skipped otherwise, so the
normal suite and CI pay nothing.

    KESTREL_BOUNDARY=1 uv run --extra dev pytest -n 0 tests/test_clock_boundaries.py

Each instant runs the modules in a child pytest with `tests/boundary_clock.py` freezing
`datetime.now()` (see that file for what it can and cannot freeze). The two instants are 00:03 on
this machine's own clock (where "today" is decided, D-UI-9) and 00:01 UTC on the 1st (hosted
spend's "this month"; also what CI runs on)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_BOUNDARY") != "1", reason="opt-in: set KESTREL_BOUNDARY=1"
)

ROOT = Path(__file__).resolve().parent.parent
MODULES = [
    "tests/test_today.py",
    "tests/test_today_screen.py",
    "tests/test_review_today_round.py",
    "tests/test_rescoring_state.py",
    "tests/test_hosted_balance.py",
    "tests/test_run_summary.py",
    "tests/test_runs_page.py",
    "tests/test_analytics.py",
    "tests/test_analytics_page.py",
]
def _instants(now: datetime | None = None) -> dict[str, datetime]:
    """The most recent past occurrences (a future instant skews code that also reads the real
    clock): 00:03 on this machine's own midnight, and 00:01 UTC on the 1st of this month."""
    now = now or datetime.now(UTC)
    midnight = now.astimezone().replace(hour=0, minute=3, second=0, microsecond=0)
    first = now.replace(day=1, hour=0, minute=1, second=0, microsecond=0)
    return {"00:03 local": midnight.astimezone(UTC), "00:01 UTC on the 1st": first}


INSTANTS = _instants()
# The month-start instant is a day behind the real date, which skews anything that also reads the
# real "today", so it runs only the modules that count a month.
MONTH_MODULES = ["tests/test_hosted_balance.py", "tests/test_analytics.py"]


@pytest.mark.parametrize("label", list(INSTANTS))
def test_time_sensitive_modules_pass_just_after_midnight(label):
    env = {**os.environ, "KESTREL_FROZEN_AT": INSTANTS[label].isoformat()}
    env.pop("KESTREL_BOUNDARY", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "-q", "-p", "tests.boundary_clock",
         "-p", "no:cacheprovider", *(MONTH_MODULES if "UTC" in label else MODULES)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"frozen at {label}:\n{proc.stdout[-3000:]}{proc.stderr[-800:]}"
