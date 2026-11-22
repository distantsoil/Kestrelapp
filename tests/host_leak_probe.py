"""Run only by tests/test_host_isolation.py, in a fixed order in its own pytest process (not a
test_* file, so the suite never collects it). The first test leaves state behind the way a
careless test would; the next checks that the shared fixtures cleared it (D-TEST-576, 578)."""

from __future__ import annotations

from kestrel.llm.profiles import Hardware
from kestrel.services import setup_state

FAKE = Hardware(os="linux", gpu_vendor="nvidia", vram_gb=99.0, ram_gb=512.0)


def test_one_fills_the_hardware_cache_with_a_fake_machine():
    setup_state._hw_cache["value"] = FAKE
    assert setup_state.detected_hardware() is FAKE


def test_two_never_sees_the_previous_tests_machine():
    assert setup_state._hw_cache["value"] is not FAKE


def test_three_starts_a_setup_action_and_returns_while_it_runs():
    """D-TEST-578, D-TEST-581: a slow Setup action (a doctor run, a pull) still running when its
    test ends. This test is reported for it (an error at teardown); the next one must not see
    it."""
    import time

    def work(act):
        time.sleep(1.5)
        setup_state._hw_cache["value"] = FAKE  # what a doctor run's hardware check would cache

    setup_state.reset_actions()
    setup_state.start_action("probe", work)
    assert setup_state.running_action() is not None


def test_four_never_shares_the_process_with_the_previous_tests_action():
    import threading

    assert [t.name for t in threading.enumerate() if t.name.startswith("setup-")] == []
    # Joined before the caches were reset, so what the thread cached went with them.
    assert setup_state._hw_cache["value"] is not FAKE
