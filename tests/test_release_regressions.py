"""Small pins for the 0.7.2 release fixes: authored robots refusal, stored vs shown task errors,
OSError kinds, and the short model name on the Models page."""

from __future__ import annotations

from kestrel.errors import (
    RobotsDisallowed,
    failure_record,
    plain_os_reason,
    shown_failure,
    task_failure_text,
)
from kestrel.web.deps import short_model_name


def test_a_robots_refusal_is_an_authored_sentence_with_no_url():
    exc = RobotsDisallowed()
    assert isinstance(exc, PermissionError)
    assert "robots.txt disallows" in task_failure_text(exc)
    assert "http" not in str(exc)


def test_a_stored_failure_keeps_the_detail_and_the_page_text_drops_it():
    stored = failure_record(OSError("network down"))
    assert "OSError: network down" in stored
    assert "network down" not in shown_failure(stored)
    assert shown_failure("an authored sentence") == "an authored sentence"
    assert shown_failure(None) == ""


def test_connection_and_timeout_errors_read_as_the_network_not_a_file():
    for exc in (ConnectionResetError("x"), TimeoutError("x"), ConnectionRefusedError("x")):
        assert "network" in plain_os_reason(exc)
        assert "file" not in task_failure_text(exc)
    assert plain_os_reason(PermissionError("x")) == "permission was denied"


def test_short_model_name_drops_a_quantization_suffix_only():
    assert short_model_name("qwen3:4b-instruct-2507-q4_K_M") == "qwen3:4b-instruct-2507"
    assert short_model_name("llama3:8b-q8_0") == "llama3:8b"
    assert short_model_name("x:7b-fp16") == "x:7b"
    assert short_model_name("qwen3:14b") == "qwen3:14b"
    assert short_model_name("kestrel-tier2") == "kestrel-tier2"
