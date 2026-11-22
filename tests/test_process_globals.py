"""Process-global state never crosses from one test to the next (D-TEST-25). The first test
dirties all three the way app code does (no monkeypatch); the second, which runs after it in
file order, must start clean. Under the stress run's random order the second can run first,
where it passes trivially; file order is the proof."""

from __future__ import annotations

import os

from kestrel import startup_health
from kestrel.services import count_cache, tracks

_STARTED_IN_SAFE_MODE = startup_health.SAFE_MODE_ENV in os.environ


def test_app_code_dirties_the_process_globals():
    os.environ[startup_health.SAFE_MODE_ENV] = "1"  # as the safe-start button does
    tracks._cache["value"] = ("consulting",)
    count_cache._entries[("invented", 1)] = (3, 0.0, None)
    assert tracks._cache["value"] == ("consulting",) and count_cache._entries


def test_the_next_test_starts_with_clean_process_globals():
    assert tracks._cache["value"] is None
    assert count_cache._entries == {}
    assert (startup_health.SAFE_MODE_ENV in os.environ) is _STARTED_IN_SAFE_MODE
