"""A failed setup action's log line is shown on the page outside Show details, so it never
names an exception class (the route crawl's rule)."""

from __future__ import annotations

import re

import pytest

from kestrel.errors import UserFacingError
from kestrel.services import setup_state as ss


@pytest.fixture(autouse=True)
def _clean_registry():
    ss.reset_actions()
    yield
    ss.reset_actions()


def _run(exc: Exception) -> ss.Action:
    def work(_a: ss.Action) -> None:
        raise exc

    act = ss.start_action("error-text-test", work)
    act.thread.join(5)
    return act


def test_an_unexpected_failure_reads_in_plain_words():
    act = _run(RuntimeError("ollama exploded at C:/Users/someone/models"))
    assert act.status == "failed"
    assert act.error == "an unexpected problem; the details are in the log"
    shown = "\n".join(act.lines)
    assert "RuntimeError" not in shown and "someone" not in shown
    assert not re.search(r"\b[A-Z]\w*(?:Error|Exception)\b", shown)


def test_kestrels_own_message_is_shown_as_written_without_its_class():
    act = _run(UserFacingError("Pick a model first."))
    assert act.error == "Pick a model first."
    assert "UserFacingError" not in "\n".join(act.lines)


def test_an_empty_message_of_kestrels_own_falls_back_to_the_plain_sentence():
    act = _run(UserFacingError("   "))
    assert act.error == "an unexpected problem; the details are in the log"
