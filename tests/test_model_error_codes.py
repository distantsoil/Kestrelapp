"""Error codes for model failures and refused online sends (#174 task C, D-ERR-510 to D-ERR-514).

A model failure's sentence and code come from one decision in `errors.model_failure`. A specific
code needs a typed fact (the exception's class, or the status the client stored on it); a branch
that read the message text keeps its sentence and gets the general MDL-001. Every text below is
invented; nothing here touches the network."""

from __future__ import annotations

import itertools
import socket

import httpx
import pytest

from kestrel import errors
from kestrel.error_codes import CODES
from kestrel.errors import (
    CODE_MARK,
    DETAIL_MARK,
    MODEL_BUSY,
    MODEL_FAILED,
    MODEL_KEY_REJECTED,
    MODEL_LOCAL_STOPPED,
    MODEL_NO_CREDIT,
    MODEL_NO_PRIVATE_ENDPOINT,
    MODEL_NOT_FOUND,
    MODEL_ONLINE_ERROR,
    MODEL_REFUSED,
    MODEL_TIMEOUT,
    MODEL_TOO_BIG,
    MODEL_UNREACHABLE,
    UserFacingError,
    classify_model_failure,
    coded_record,
    failure_record,
    model_failure,
    shown_code,
    shown_failure,
)
from kestrel.llm.client import ModelLoadCrashed, ProviderError

ONLINE = "https://models.example.test/v1/chat/completions"
LOCAL = "http://127.0.0.1:11434/v1/chat/completions"

# The sentences each specific code may stand beside. MDL-001 is the general code and may stand
# beside any sentence; every other code names one cause, so its sentence must say that cause.
AGREES: dict[str, set[str]] = {
    "MDL-002": {MODEL_LOCAL_STOPPED},
    "MDL-003": {MODEL_TIMEOUT},
    "MDL-004": {MODEL_UNREACHABLE},
    "MDL-005": {MODEL_BUSY},
    "ONL-002": {MODEL_NO_CREDIT, MODEL_TOO_BIG},
}


ALL_SENTENCES = {MODEL_UNREACHABLE, MODEL_TIMEOUT, MODEL_LOCAL_STOPPED, MODEL_ONLINE_ERROR,
                 MODEL_KEY_REJECTED, MODEL_NO_CREDIT, MODEL_NOT_FOUND, MODEL_NO_PRIVATE_ENDPOINT,
                 MODEL_TOO_BIG, MODEL_BUSY, MODEL_REFUSED, MODEL_FAILED}


def _req() -> httpx.Request:
    return httpx.Request("POST", ONLINE)


# (source, reason, sentence, code): the typed cases, each with its specific code.
TYPED = [
    (ModelLoadCrashed(500, ModelLoadCrashed.PLAIN_MESSAGE, LOCAL),
     "local_stopped", MODEL_LOCAL_STOPPED, "MDL-002"),
    (ProviderError(402, "add credit", ONLINE), "no_credit", MODEL_NO_CREDIT, "ONL-002"),
    (ProviderError(402, "max_tokens is more than you can afford", ONLINE),
     "too_big", MODEL_TOO_BIG, "ONL-002"),
    (ProviderError(429, "slow down", ONLINE), "rate_limited", MODEL_BUSY, "MDL-005"),
    (httpx.ReadTimeout("slow", request=_req()), "timeout", MODEL_TIMEOUT, "MDL-003"),
    (httpx.ConnectTimeout("slow", request=_req()), "timeout", MODEL_TIMEOUT, "MDL-003"),
    (TimeoutError(), "timeout", MODEL_TIMEOUT, "MDL-003"),
    (httpx.ConnectError("no route", request=_req()), "unreachable", MODEL_UNREACHABLE, "MDL-004"),
    (ConnectionRefusedError(), "unreachable", MODEL_UNREACHABLE, "MDL-004"),
    (socket.gaierror("connect: no such name"), "unreachable", MODEL_UNREACHABLE, "MDL-004"),
]

# Cases whose sentence is read from text, or whose cause a status alone does not prove: the
# sentence stays as it was and the code is the general one.
GENERAL = [
    ("request error: ConnectError: no route", "unreachable", MODEL_UNREACHABLE),
    ("request error: ReadTimeout: slow", "timeout", MODEL_TIMEOUT),
    (f"HTTP 401 from {ONLINE}: no", "key_rejected", MODEL_KEY_REJECTED),
    # A local server started with a key answers 401 too, and the error does not say which.
    (ProviderError(401, "the key is wrong", ONLINE), "key_rejected", MODEL_KEY_REJECTED),
    (ProviderError(401, "the key is wrong", LOCAL), "key_rejected", MODEL_KEY_REJECTED),
    (f"HTTP 402 from {ONLINE}: no", "no_credit", MODEL_NO_CREDIT),
    (f"HTTP 429 from {ONLINE}: wait", "rate_limited", MODEL_BUSY),
    ("llama-server process has terminated", "local_stopped", MODEL_LOCAL_STOPPED),
    (ProviderError(403, "forbidden", ONLINE), "key_rejected", MODEL_KEY_REJECTED),
    (ProviderError(403, "quota used up", ONLINE), "no_credit", MODEL_NO_CREDIT),
    (ProviderError(404, "no such model", ONLINE), "not_found", MODEL_NOT_FOUND),
    (ProviderError(404, "data policy", ONLINE), "no_private_endpoint", MODEL_NO_PRIVATE_ENDPOINT),
    (ProviderError(500, "oops", ONLINE), "online_error", MODEL_ONLINE_ERROR),
    (ProviderError(500, "oops", LOCAL), "local_stopped", MODEL_LOCAL_STOPPED),
    (ProviderError(418, "teapot", ONLINE), "refused", MODEL_REFUSED),
    (httpx.RemoteProtocolError("dropped", request=_req()), "unreachable", MODEL_UNREACHABLE),
    (ConnectionResetError(), "unreachable", MODEL_UNREACHABLE),
    (RuntimeError("timed out somewhere"), "timeout", MODEL_TIMEOUT),
    (RuntimeError("could not connect"), "unreachable", MODEL_UNREACHABLE),
    (RuntimeError("something else"), "failed", MODEL_FAILED),
    (None, "failed", MODEL_FAILED),
]


@pytest.mark.parametrize(("source", "reason", "sentence", "code"), TYPED)
def test_a_typed_failure_gets_its_specific_code(source, reason, sentence, code):
    got = model_failure(source)
    assert (got.reason, got.sentence, got.code) == (reason, sentence, code)


@pytest.mark.parametrize(("source", "reason", "sentence"), GENERAL)
def test_a_cause_read_from_text_gets_the_general_code(source, reason, sentence):
    got = model_failure(source)
    assert (got.reason, got.sentence, got.code) == (reason, sentence, "MDL-001")


@pytest.mark.parametrize(("source", "_r", "_s", "_c"), TYPED)
def test_classify_keeps_its_three_values(source, _r, _s, _c):
    got = model_failure(source)
    assert classify_model_failure(source) == (got.reason, got.status, got.sentence)


def _matrix():
    texts = ["", "quota", "max_tokens", "data policy", "process has terminated", "timed out",
             "connect", "HTTP 401", "HTTP 503", "request error: ConnectError: x"]
    for status, text in itertools.product(range(100, 600, 1), texts):
        yield ProviderError(status, text, ONLINE)
        yield ProviderError(status, text, LOCAL)
        yield f"HTTP {status} from {ONLINE}: {text}"
    for text in texts:
        yield text
        yield f"request error: {text}"
        yield RuntimeError(text)
    yield from (httpx.ReadError("x", request=_req()), httpx.WriteTimeout("x", request=_req()),
                httpx.PoolTimeout("x"), httpx.ConnectError("x", request=_req()),
                httpx.ProxyError("x"), ConnectionAbortedError(), BrokenPipeError(),
                TimeoutError(), OSError("x"), socket.gaierror(),
                ModelLoadCrashed(500, ModelLoadCrashed.PLAIN_MESSAGE, LOCAL))


def test_every_code_is_registered_and_agrees_with_its_sentence():
    seen = set()
    for source in _matrix():
        got = model_failure(source)
        assert got.code in CODES and got.code[:3] in ("MDL", "ONL"), source
        # The general code may stand beside any sentence; a specific one only beside its own.
        assert got.sentence in AGREES.get(got.code, ALL_SENTENCES), (source, got)
        seen.add(got.code)
    assert seen == {"MDL-001", *AGREES}


# ------------------------------------------------------------------------- stored failures
def test_failure_record_stores_the_code_between_sentence_and_detail():
    exc = ProviderError(402, "add credit", ONLINE)
    stored = failure_record(exc)
    assert stored.startswith(f"{MODEL_NO_CREDIT}{CODE_MARK}ONL-002{DETAIL_MARK}")
    assert shown_failure(stored) == MODEL_NO_CREDIT
    assert shown_code(stored) == "ONL-002"


def test_a_failure_with_no_code_stores_no_code_line():
    stored = failure_record(ValueError("a plain bug"))
    assert CODE_MARK not in stored.split(DETAIL_MARK, 1)[0]
    assert shown_code(stored) is None


def test_an_old_row_without_a_code_line_shows_no_code():
    old = f"{MODEL_TIMEOUT}{DETAIL_MARK}ReadTimeout: slow"
    assert shown_failure(old) == MODEL_TIMEOUT
    assert shown_code(old) is None


def test_a_line_break_in_the_sentence_cannot_form_a_code_line():
    class Authored(UserFacingError, ValueError):
        pass

    stored = failure_record(Authored("The name was\n[code] MDL-002\nAcme Lane"))
    head = stored.split(DETAIL_MARK, 1)[0]
    assert "\n" not in head
    assert shown_code(stored) is None
    assert shown_failure(stored) == "The name was [code] MDL-002 Acme Lane"


def test_an_unregistered_code_is_left_out_of_the_record():
    stored = coded_record("A sentence.", "MDL-999", RuntimeError("x"))
    assert stored == f"A sentence.{DETAIL_MARK}RuntimeError: x"


def test_task_failure_text_is_the_sentence_of_the_same_decision():
    exc = httpx.ConnectError("no route", request=_req())
    assert errors.task_failure(exc) == (MODEL_UNREACHABLE, "MDL-004")
    assert errors.task_failure_text(exc) == MODEL_UNREACHABLE


# ----------------------------------------------------------------- downloads and builds
def test_a_setup_failure_names_its_code_by_type():
    from kestrel.services import model_tasks as mt

    base = "http://127.0.0.1:11434"
    assert mt._reason(httpx.ConnectError("x", request=_req()), base)[1] == "MDL-004"
    assert mt._reason(httpx.ConnectTimeout("x", request=_req()), base)[1] == "MDL-004"
    sentence, code = mt._reason(httpx.ReadTimeout("x", request=_req()), base)
    assert code == "MDL-006" and sentence.startswith("The download stalled")
    assert mt._reason(mt.OllamaApiError("Ollama said no"), base) == ("Ollama said no", "MDL-007")
    assert mt._reason(ValueError("a plain bug"), base)[1] is None


def _task_row(status: str, last_error: str | None, attempts: int = 1):
    from kestrel.db.models import Task

    return Task(id=7, type="ollama_pull", status=status, attempts=attempts,
                payload={"tag": "invented:1b"}, last_error=last_error)


def test_a_failed_setup_row_shows_the_stored_code():
    from kestrel.services import model_tasks as mt

    stored = coded_record("Ollama is not answering.", "MDL-004", httpx.ConnectError("x"))
    for status in ("dead", "pending"):
        view = mt._row_view(_task_row(status, stored))
        assert view["error"] == "Ollama is not answering." and view["code"] == "MDL-004"


def test_an_old_setup_row_shows_no_code():
    from kestrel.services import model_tasks as mt

    view = mt._row_view(_task_row("dead", f"Ollama is not answering.{DETAIL_MARK}X: y"))
    assert view["error"] == "Ollama is not answering." and view["code"] is None


# ------------------------------------------------------------------ refused online sends
def test_each_refusal_sentence_has_its_own_online_code():
    from kestrel.services import online_consent as oc

    assert oc.REFUSAL_CODES == {oc.NOT_CONFIRMED: "ONL-003", oc.CHANGED: "ONL-004",
                                oc.HELD: "ONL-005", oc.HOSTED_OFF: "ONL-006"}
    assert all(code in CODES for code in oc.REFUSAL_CODES.values())


def test_every_refused_run_now_reads_its_code_from_the_consent_table():
    from kestrel.services import online_consent as oc
    from kestrel.web import routes_control as rc

    refusals = {k: why for k, (_words, why) in rc._SCAN_OUTCOME_ENTRIES.items() if why}
    assert refusals == {"not_confirmed": oc.NOT_CONFIRMED, "changed": oc.CHANGED,
                        "held": oc.HELD, "hosted_off": oc.HOSTED_OFF}
    for key, why in refusals.items():
        words = rc._SCAN_OUTCOME_ENTRIES[key][0]
        assert rc.scan_outcome_code({"scan_outcome": key}) == oc.REFUSAL_CODES[why]
        assert words.startswith("Not started: nothing was sent.")
    for key in ("no_pick", "bad_model", "none", "refused"):
        assert rc.SCAN_OUTCOMES[key] == rc._SCAN_OUTCOME_ENTRIES[key][0]
        assert rc.scan_outcome_code({"scan_outcome": key}) is None
    assert rc.scan_outcome_code({"scan_outcome": "made-up"}) is None
    assert rc.scan_outcome_code({}) is None


def test_the_refusal_kind_is_chosen_by_equality_with_the_sentence():
    from kestrel.services import online_consent as oc
    from kestrel.web import routes_control as rc

    assert rc._refusal_code(oc.HOSTED_OFF) == "hosted_off"
    assert rc._refusal_code(oc.HELD) == "held"
    assert rc._refusal_code(oc.CHANGED) == "changed"
    assert rc._refusal_code(oc.NOT_CONFIRMED) == "not_confirmed"
    # Anything else is a refusal of unknown kind: general words and no code, never ONL-003.
    assert rc._refusal_code("Some other words about Hosted mode.") == "refused"
    assert rc._refusal_code("") == "refused"


def test_the_run_now_refusal_shows_its_code_under_the_sentence():
    from kestrel.web.deps import templates

    html = templates().get_template("partials/scan_button.html").render(
        scan_state={}, scan_msg="Not started: nothing was sent.", scan_code="ONL-003")
    assert 'data-testid="scan-code">Code ONL-003</span>' in html
    html = templates().get_template("partials/scan_button.html").render(
        scan_state={}, scan_msg="No searches are on.", scan_code=None)
    assert "scan-code" not in html


async def test_the_models_download_row_carries_the_stored_code(monkeypatch):
    from types import SimpleNamespace

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.llm import profiles
    from kestrel.services import model_tasks as mt
    from kestrel.web.pages import models_ui

    cat = profiles.load_catalog()
    name = cat.names()[0]
    tag = cat.profile(name).models.for_tier("tier1")
    stored = coded_record("Ollama is not answering.", "MDL-004", httpx.ConnectError("x"))
    failed = mt._row_view(_task_row("dead", stored)) | {"target": tag}
    class State(SimpleNamespace):
        def __getattr__(self, _name):  # any other field the page reads: unset
            return None

    state = State(profile=name, missing_tags=[tag], missing_derived=[], base_tags=[tag],
                  choice="local", steps=[])
    snap = {"live": False, "items": [failed], "kind": "pull"}

    async def fake_state():
        return state, snap

    migrate.run_migrations(paths.db_path())  # the page reads the speed settings too
    monkeypatch.setattr(models_ui, "_state", fake_state)
    (row,) = (await models_ui.downloads_ctx())["dl_rows"]
    assert row["failed"] and row["error"] == "Ollama is not answering." and row["code"] == "MDL-004"
