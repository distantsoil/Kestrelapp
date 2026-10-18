"""The desktop app writes the app's own events to kestrel.log.

Every module logs through structlog (`log = structlog.get_logger()`). The desktop entry point
configured only stdlib logging, so structlog kept its default of printing to stdout, which a
windowed build does not have. A 0.4.0 diagnostics bundle held migrations and outbound HTTP lines
and not one Kestrel event: not even `kestrel_start`, logged straight after logging is configured.
"""

from __future__ import annotations

import logging

import pytest
import structlog

from kestrel import logging_setup, paths

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture
def _clean_logging():
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    logging_setup._configured = False
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)
    logging_setup._configured = False
    structlog.reset_defaults()


@pytest.mark.usefixtures("_clean_logging")
def test_desktop_logging_puts_structlog_events_in_kestrel_log():
    from kestrel.desktop import shell

    paths.ensure_dirs()
    shell._configure_logging()
    structlog.get_logger().warning("probe_event_for_the_log_file", detail="x")
    # A logger no other test configures (uvicorn reconfigures its own once any server has run).
    logging.getLogger("kestrel.test.stdlib_probe").info("stdlib line still arrives")
    for h in logging.getLogger().handlers:
        h.flush()

    text = (paths.logs_dir() / "kestrel.log").read_text(encoding="utf-8")
    assert "probe_event_for_the_log_file" in text, (
        "a structlog event never reached kestrel.log, so the packaged app's warnings are lost"
    )
    assert "stdlib line still arrives" in text


@pytest.mark.usefixtures("_clean_logging")
def test_non_ascii_survives_into_the_log_so_the_scrubber_can_match_it():
    """The diagnostics scrubber masks by exact substring. JSON's default `ensure_ascii` writes
    "Nestlé" as `Nestl\u00e9`, which the scrubber never matches, so a confidential employer name
    would ship in a diagnostics bundle."""
    from kestrel.desktop import shell

    paths.ensure_dirs()
    shell._configure_logging()
    structlog.get_logger().info("probe_employer", org="Nestlé Café")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (paths.logs_dir() / "kestrel.log").read_text(encoding="utf-8")
    assert "Nestlé Café" in text


def test_the_scrubber_masks_a_key_in_its_json_escaped_form():
    import json

    from kestrel.services.diagnostics import REDACTED, make_scrubber

    key = r'sk-or-v1-ab"cd\ef0123456789'
    line = "2026-10-17 INFO MainThread " + json.dumps({"event": "x", "header": key})
    scrubbed = make_scrubber(None, [key])(line)
    assert REDACTED in scrubbed and "ab\\\"cd" not in scrubbed, scrubbed
