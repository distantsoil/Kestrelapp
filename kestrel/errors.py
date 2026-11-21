"""Exceptions whose text is written by Kestrel and is safe to show to the operator.

Any other exception can carry a file path, a URL or a key in its text (D-SEC-1), so a response
never renders it: the caller logs the real exception and shows `plain_error(exc)` instead
(kestrel/web/errors.py), which returns this class's message and nothing else's."""

from __future__ import annotations

from typing import NamedTuple

from kestrel.error_codes import CODES, lookup


class UserFacingError(Exception):
    """Base for errors whose message is an authored sentence, never library or OS text.

    Subclass it alongside the stdlib type callers already catch (`class X(UserFacingError,
    ValueError)`), so existing `except ValueError` clauses keep working. Never build the message
    from `str(other_exception)`; log that and say what happened in your own words."""


ROBOTS_SENTENCE = (
    "That site's robots.txt disallows reading this page, so Kestrel did not open it."
)


class RobotsDisallowed(UserFacingError, PermissionError):
    """A site's robots.txt forbids reading a page. The message is authored: it names robots.txt
    and nothing of the URL, so it is safe to show as written."""

    def __init__(self) -> None:
        super().__init__(ROBOTS_SENTENCE)


def plain_os_reason(exc: OSError) -> str:
    """The OS reason for a failure without the file name it carries ("permission was denied")."""
    if isinstance(exc, RobotsDisallowed):
        return str(exc)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "the network did not answer"
    if isinstance(exc, PermissionError):
        return "permission was denied"
    if isinstance(exc, FileNotFoundError):
        return "the file was not found"
    return (exc.strerror or "a file could not be read or written").lower()


def yaml_where(exc: Exception) -> str:
    """" (line N)" for a YAML parse error that knows its line, else "". Never the parser's own
    text, which quotes the offending line of the file."""
    mark = getattr(exc, "problem_mark", None)
    line = getattr(mark, "line", None)
    return f" (line {line + 1})" if isinstance(line, int) else ""


# ----------------------------------------------------------------------- model request failures
_LOCAL_MARKERS = ("127.0.0.1", "localhost", "[::1]", ":11434")

MODEL_UNREACHABLE = "Kestrel could not reach the AI."
MODEL_TIMEOUT = "The AI took too long to answer. Try again."
MODEL_LOCAL_STOPPED = (
    "The local AI stopped while loading the model. Restart Ollama, then try again."
)
MODEL_ONLINE_ERROR = "The online model returned an error. Try again in a minute."
MODEL_KEY_REJECTED = (
    "The online service did not accept the API key. Check it under Settings > Hosted."
)
MODEL_NO_CREDIT = (
    "The online account is out of credit or over its limit. Top up at the provider, then try again."
)
MODEL_NOT_FOUND = (
    "The service has no model that fits this request. Check the model name on the Models page."
)
MODEL_NO_PRIVATE_ENDPOINT = (
    "No provider serves this model under Kestrel's privacy setting, which blocks providers that "
    "may train on your data. Pick a paid model, or allow those providers under Settings > Hosted."
)
MODEL_TOO_BIG = "Your OpenRouter credit is too low for this request size."
MODEL_BUSY = "The online service is busy (rate limit). Wait a minute and try again."
MODEL_REFUSED = "The AI service refused the request."
MODEL_FAILED = "The AI request failed. Try again; the details are in the log."


class ModelFailure(NamedTuple):
    """One decision about a failed model call: the reason code that logs carry, the HTTP status
    or None, the fixed sentence a person reads, and the registry code shown beside it."""

    reason: str
    status: int | None
    sentence: str
    code: str


# The general model code: the cause was read from text, or is not one Kestrel can prove from the
# kind of failure (D-ERR-510). A specific code needs a typed fact: the exception's class, or the
# HTTP status the client stored on it.
MODEL_GENERAL_CODE = "MDL-001"


def model_failure(source: object) -> ModelFailure:
    """Classify a failed call to a model or its key check. `source` is the exception, or the
    `request error: ...` text a guard result carries.

    The sentence is chosen by kind and is one of the constants above; nothing of the exception's
    text, URL, status line or message is ever copied into it (D-SEC-2, D-SEC-4), so a page, a
    toast or a JSON body that shows it cannot carry a URL, a path or a key. Each branch returns
    its sentence and its code together (D-ERR-510). A branch decided only by a typed fact (the
    exception's class, or the HTTP status the client stored on it) gets a specific code; a branch
    whose choice needed the message text, or text standing in for an exception, keeps its
    sentence and gets the general code, never a specific one."""
    import re
    import socket

    import httpx

    text = str(source or "")
    status = getattr(source, "status", None)
    typed_status = isinstance(status, int)
    if not typed_status:
        m = re.search(r"HTTP (\d{3})", text)
        status = int(m.group(1)) if m else None
    is_exc = isinstance(source, BaseException)
    kind = type(source).__name__ if is_exc else ""
    low = text.lower()
    named = re.search(r"request error: (\w+):", text)
    if named:
        kind = named.group(1)
    typed_kind = is_exc and not named
    local = any(m in low for m in _LOCAL_MARKERS)
    general = MODEL_GENERAL_CODE
    if kind == "ModelLoadCrashed" or "process has terminated" in low:
        crashed = typed_kind and _is_load_crash(source)
        return ModelFailure("local_stopped", status, MODEL_LOCAL_STOPPED,
                            "MDL-002" if crashed else general)
    if status is not None:
        # A 401 is a refused key, but a local server started with a key answers 401 too, and the
        # error does not carry which backend it came from: general until it does (D-ERR-515).
        if status == 401 or (status == 403 and "quota" not in low):
            return ModelFailure("key_rejected", status, MODEL_KEY_REJECTED, general)
        # Both 402 sentences are a credit problem, which the stored status alone proves; only
        # which of the two sentences reads the message text.
        credit_code = "ONL-002" if typed_status else general
        if status == 402 and "max_tokens" in low:
            return ModelFailure("too_big", status, MODEL_TOO_BIG, credit_code)
        if status == 402:
            return ModelFailure("no_credit", status, MODEL_NO_CREDIT, credit_code)
        if status == 403:
            return ModelFailure("no_credit", status, MODEL_NO_CREDIT, general)
        if status == 404:
            # A 404 can also be a wrong address, so neither sentence is proven: general.
            if "data policy" in low:
                return ModelFailure("no_private_endpoint", status, MODEL_NO_PRIVATE_ENDPOINT,
                                    general)
            return ModelFailure("not_found", status, MODEL_NOT_FOUND, general)
        if status == 429:
            return ModelFailure("rate_limited", status, MODEL_BUSY,
                                "MDL-005" if typed_status else general)
        if status >= 500:
            if local:
                return ModelFailure("local_stopped", status, MODEL_LOCAL_STOPPED, general)
            return ModelFailure("online_error", status, MODEL_ONLINE_ERROR, general)
        return ModelFailure("refused", status, MODEL_REFUSED, general)
    timed_out = is_exc and isinstance(source, TimeoutError | httpx.TimeoutException)
    if "Timeout" in kind or "timed out" in low or timed_out:
        return ModelFailure("timeout", None, MODEL_TIMEOUT, "MDL-003" if timed_out else general)
    if any(k in kind for k in ("Connect", "Network", "Protocol", "Transport", "Read", "Remote",
                               "Refused", "Reset", "Gaierror")) or "connect" in low:
        # Only a failure to open the connection proves the service was not reached; a reset or
        # a dropped reply means it was reached, so those keep the sentence and the general code.
        no_connect = is_exc and isinstance(
            source, httpx.ConnectError | ConnectionRefusedError | socket.gaierror)
        return ModelFailure("unreachable", None, MODEL_UNREACHABLE,
                            "MDL-004" if no_connect else general)
    return ModelFailure("failed", None, MODEL_FAILED, general)


def _is_load_crash(source: object) -> bool:
    from kestrel.llm.client import ModelLoadCrashed

    return isinstance(source, ModelLoadCrashed)


def classify_model_failure(source: object) -> tuple[str, int | None, str]:
    """(reason code, HTTP status or None, a fixed sentence): `model_failure` without the
    registry code, for the callers that log the reason and show the sentence only."""
    failure = model_failure(source)
    return failure.reason, failure.status, failure.sentence


def is_model_failure(exc: BaseException) -> bool:
    """Whether `exc` is a failed HTTP call to a model or key check (an httpx error or a
    ProviderError), whose text carries a URL and the provider's own message."""
    names = {c.__name__ for c in type(exc).__mro__}
    return "ProviderError" in names or "HTTPError" in names


def task_failure(exc: BaseException) -> tuple[str, str | None]:
    """(sentence, registry code or None) for a failed background task, chosen together: a model
    call by its kind with its code, an authored error as written, an OS reason without its file
    name, else a fixed sentence. Only model calls carry a code here (D-ERR-510)."""
    if is_model_failure(exc):
        failure = model_failure(exc)
        return failure.sentence, failure.code
    if isinstance(exc, UserFacingError):
        return str(exc), None
    if isinstance(exc, OSError):
        return plain_os_reason(exc), None  # a ConnectionError or TimeoutError reads as the network
    return "an unexpected problem; the details are in the log", None


def task_failure_text(exc: BaseException) -> str:
    """The text a failed background task keeps for the Runs page: a fixed sentence by kind, never
    the exception's own text (a URL, a path, a provider message): an authored error as written, an
    OS reason without its file name, a model call by its kind, else the type name only."""
    return task_failure(exc)[0]


# ------------------------------------------------------------------ stored failures vs. pages
# Task.last_error is stored diagnosis, not a page: it keeps the plain sentence first and the
# exception's type and message after this marker, so a developer reading the database (or a log
# export) still sees the cause. Anything that shows a task's error on a page goes through
# `shown_failure`, which cuts the stored detail off (D-SEC-2, D-SEC-4).
DETAIL_MARK = "\n[detail] "
# An error code, when the failure has one, sits on its own line between the sentence and the
# detail: "sentence\n[code] MDL-003\n[detail] ..." (SPEC-INTERFACES.md §2.7, D-ERR-5). Task C of
# #174 writes it (`coded_record`); old rows and new rows both read cleanly.
CODE_MARK = "\n[code] "


def one_line(sentence: str) -> str:
    """`sentence` with every line break turned into a space, so text inside it (an authored
    message that quotes a name) can never form a `[code]` or `[detail]` line (D-ERR-8). Every
    writer of a stored failure uses it, through `coded_record` or directly."""
    return " ".join(part.strip() for part in sentence.splitlines() if part.strip())


def coded_record(sentence: str, code: str | None, exc: BaseException) -> str:
    """What a failed task stores: the sentence on one line, the code line when the failure has a
    registered code, then the exception type and message. The one writer of the stored form: any
    module that stores a sentence with a code calls this, never builds the lines itself.
    `sentence` and `code` come from one decision at the caller; an unregistered code is left out
    rather than stored (a test holds every code a caller passes to the registry)."""
    code_line = f"{CODE_MARK}{code}" if code and code in CODES else ""
    return f"{one_line(sentence)}{code_line}{DETAIL_MARK}{type(exc).__name__}: {exc}"


def failure_record(exc: BaseException) -> str:
    """What a failed task stores: the plain sentence, its code, then the exception type and
    message."""
    sentence, code = task_failure(exc)
    return coded_record(sentence, code, exc)


def _before_detail(stored: str | None) -> str:
    return (stored or "").split(DETAIL_MARK, 1)[0]


def shown_failure(stored: str | None) -> str:
    """The part of a stored task error that may reach a page: the sentence, without the detail
    and without the code line (a page shows the code on its own, through `shown_code`)."""
    return _before_detail(stored).split(CODE_MARK, 1)[0]


def shown_code(stored: str | None) -> str | None:
    """The error code stored with a failure, or None. Read only from the part before the detail,
    so a "[code]" line inside an exception message can never pose as one, and kept only when it
    is a registered code, live or retired (`error_codes.lookup`); anything else shows no code."""
    head = _before_detail(stored)
    if CODE_MARK not in head:
        return None
    candidate = head.split(CODE_MARK, 1)[1].strip()
    return candidate if lookup(candidate) is not None else None
