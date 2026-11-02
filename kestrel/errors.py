"""Exceptions whose text is written by Kestrel and is safe to show to the operator.

Any other exception can carry a file path, a URL or a key in its text (D-SEC-1), so a response
never renders it: the caller logs the real exception and shows `plain_error(exc)` instead
(kestrel/web/errors.py), which returns this class's message and nothing else's."""

from __future__ import annotations


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


def classify_model_failure(source: object) -> tuple[str, int | None, str]:
    """(reason code, HTTP status or None, a fixed sentence) for a failed call to a model or its
    key check. `source` is the exception, or the `request error: ...` text a guard result carries.

    The sentence is chosen by kind and is one of the constants above; nothing of the exception's
    text, URL, status line or message is ever copied into it (D-SEC-2, D-SEC-4), so a page, a
    toast or a JSON body that shows it cannot carry a URL, a path or a key. The caller logs the
    code and status."""
    import re

    text = str(source or "")
    status = getattr(source, "status", None)
    if not isinstance(status, int):
        m = re.search(r"HTTP (\d{3})", text)
        status = int(m.group(1)) if m else None
    kind = type(source).__name__ if isinstance(source, BaseException) else ""
    low = text.lower()
    named = re.search(r"request error: (\w+):", text)
    if named:
        kind = named.group(1)
    local = any(m in low for m in _LOCAL_MARKERS)
    if kind == "ModelLoadCrashed" or "process has terminated" in low:
        return "local_stopped", status, MODEL_LOCAL_STOPPED
    if status is not None:
        if status == 401 or (status == 403 and "quota" not in low):
            return "key_rejected", status, MODEL_KEY_REJECTED
        if status == 402 and "max_tokens" in low:
            return "too_big", status, MODEL_TOO_BIG
        if status == 402 or status == 403:
            return "no_credit", status, MODEL_NO_CREDIT
        if status == 404:
            if "data policy" in low:
                return "no_private_endpoint", status, MODEL_NO_PRIVATE_ENDPOINT
            return "not_found", status, MODEL_NOT_FOUND
        if status == 429:
            return "rate_limited", status, MODEL_BUSY
        if status >= 500:
            return ("local_stopped", status, MODEL_LOCAL_STOPPED) if local \
                else ("online_error", status, MODEL_ONLINE_ERROR)
        return "refused", status, MODEL_REFUSED
    if "Timeout" in kind or "timed out" in low or isinstance(source, TimeoutError):
        return "timeout", None, MODEL_TIMEOUT
    if any(k in kind for k in ("Connect", "Network", "Protocol", "Transport", "Read", "Remote",
                               "Refused", "Reset", "Gaierror")) or "connect" in low:
        return "unreachable", None, MODEL_UNREACHABLE
    return "failed", None, MODEL_FAILED


def is_model_failure(exc: BaseException) -> bool:
    """Whether `exc` is a failed HTTP call to a model or key check (an httpx error or a
    ProviderError), whose text carries a URL and the provider's own message."""
    names = {c.__name__ for c in type(exc).__mro__}
    return "ProviderError" in names or "HTTPError" in names


def task_failure_text(exc: BaseException) -> str:
    """The text a failed background task keeps for the Runs page: a fixed sentence by kind, never
    the exception's own text (a URL, a path, a provider message): an authored error as written, an
    OS reason without its file name, a model call by its kind, else the type name only."""
    if is_model_failure(exc):
        return classify_model_failure(exc)[2]
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, OSError):
        return plain_os_reason(exc)  # a ConnectionError or TimeoutError reads as the network
    return f"an unexpected problem ({type(exc).__name__}); the details are in the log"


# ------------------------------------------------------------------ stored failures vs. pages
# Task.last_error is stored diagnosis, not a page: it keeps the plain sentence first and the
# exception's type and message after this marker, so a developer reading the database (or a log
# export) still sees the cause. Anything that shows a task's error on a page goes through
# `shown_failure`, which cuts the stored detail off (D-SEC-2, D-SEC-4).
DETAIL_MARK = "\n[detail] "


def failure_record(exc: BaseException) -> str:
    """What a failed task stores: the plain sentence, then the exception type and message."""
    return f"{task_failure_text(exc)}{DETAIL_MARK}{type(exc).__name__}: {exc}"


def shown_failure(stored: str | None) -> str:
    """The part of a stored task error that may reach a page: everything before the detail."""
    return (stored or "").split(DETAIL_MARK, 1)[0]
