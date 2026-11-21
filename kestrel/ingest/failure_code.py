"""The error code of a source check that did not finish cleanly (#174, SPEC-INTERFACES §2.7).

The runner knows the cause at the moment it happens: a typed exception, or a sentence an adapter
wrote for the person (`partial_error`, an `AdapterError` message). This module turns that into one
registered `SRC-` code, once, so the code is stored with the run (`ERROR_CODE_KEY` in its counts)
and an old run with no stored code shows none (D-ERR-5). The sentences are Kestrel's own, never an
exception's text, so matching on them is safe; anything it does not recognize gets the plain
"part of the check" or "could not read" code, never a guessed specific one (D-ERR-20)."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from kestrel import error_codes
from kestrel.errors import RobotsDisallowed

ERROR_CODE_KEY = "error_code"

SETUP_INVALID = "SRC-001"
RATE_LIMITED = "SRC-002"
SERVER_ERROR = "SRC-003"
TIMED_OUT = "SRC-004"
ROBOTS = "SRC-005"
NEEDS_KEY = "SRC-006"
DAILY_LIMIT = "SRC-007"
NOT_FOUND = "SRC-008"
PARTIAL = "SRC-009"
UNREADABLE = "SRC-010"
NOT_SAVED = "SRC-011"
UNEXPECTED = "SRC-012"
NO_BOARDS = "SRC-013"
NONE_SAVED = "SRC-014"

def _specific(code: object) -> str | None:
    """`code` only when it is a registered source code; anything else is no specific code."""
    if not isinstance(code, str):
        return None
    entry = error_codes.lookup(code)
    return code if entry is not None and entry.area == "SRC" else None


def _key_sentences() -> frozenset[str]:
    """The sentences the keyed boards write when a key is missing. Constants the adapters
    export, so a sentence is matched by equality, never by a search."""
    from kestrel.ingest.adapters import adzuna, usajobs

    return frozenset((adzuna.NEEDS_KEY, usajobs.NEEDS_KEY))


def code_for_kind(kind: object) -> str | None:
    """The one code a notice kind carries; None (the general code) for a kind that names no
    proven cause (a page that failed for another reason, a feed turned off, Kestrel's own cap,
    anything else the adapter said in its own words)."""
    from kestrel.ingest.base import NoticeKind

    return {
        NoticeKind.ROBOTS_REFUSED: ROBOTS,
        NoticeKind.RATE_LIMITED: RATE_LIMITED,
        NoticeKind.SERVER_ERROR: SERVER_ERROR,
        NoticeKind.NEEDS_KEY: NEEDS_KEY,
        NoticeKind.DAILY_LIMIT: DAILY_LIMIT,
    }.get(kind) if isinstance(kind, NoticeKind) else None


def typed_code_for_message(message: object) -> str | None:
    """A specific code for a sentence an adapter wrote, only when the adapter named it: the
    kind a `Problem` carries (chosen where the sentence is chosen, so the two agree), or a
    sentence equal to an exported constant. The HTTP status is never used: the same status
    reads differently on different boards (D-ERR-20)."""
    from kestrel.ingest.base import kind_of

    if isinstance(message, str) and message in _key_sentences():
        return NEEDS_KEY
    return code_for_kind(kind_of(message))


def code_for_notices(notices: Iterable[object] | None, *, fallback: str) -> str:
    """The code of a fetch that stopped early with `notices`: the one code every notice that is
    not Kestrel's own cap shares, or `fallback` when they disagree, when one names no proven
    cause, or when there is only a cap. Uncertain is general (D-ERR-24 round 9)."""
    from kestrel.ingest.base import Notice, NoticeKind

    causes = [n for n in (notices or ()) if isinstance(n, Notice) and n.kind is not
              NoticeKind.CAPPED]
    codes = {code_for_kind(n.kind) for n in causes}
    if len(codes) == 1:
        (code,) = codes
        return code or fallback
    return fallback


def code_for_partial(message: str | None, *, fallback: str) -> str:
    """The code for one sentence a fetch stopped early with."""
    return typed_code_for_message(message) or fallback


def _next_cause(err: BaseException) -> BaseException | None:
    """What caused `err`: its explicit `from` cause; and, for an AdapterError only (the wrapper
    adapters raise from an `except` block), the exception being handled unless `from None`
    suppressed it. Another exception type that was raised inside a handler is a bug of its own."""
    from kestrel.ingest.base import AdapterError

    if err.__cause__ is not None:
        return err.__cause__
    if isinstance(err, AdapterError) and not err.__suppress_context__:
        return err.__context__
    return None


def _chain(exc: BaseException):
    """The exception and what caused it, nearest first (bounded, never loops)."""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen and len(seen) < 6:
        seen.append(cur)
        yield cur
        cur = _next_cause(cur)


def code_for_exception(exc: BaseException) -> str:
    """The code for the failure that ended a check. Read only from types: a setup error, a site
    that disallows reading, a 429, a 5xx or a timeout from the shared client (on the exception
    or the one that caused it), or a sentence an adapter coded. Anything else is the general
    code; a 404 or any other status alone is not enough to say more."""
    from kestrel.ingest.base import AdapterError, SourceConfigError

    for err in _chain(exc):
        if isinstance(err, RobotsDisallowed):
            return ROBOTS
        if isinstance(err, SourceConfigError):
            return SETUP_INVALID
        if isinstance(err, httpx.HTTPStatusError):
            status = err.response.status_code
            if status == 429:
                return RATE_LIMITED
            if status >= 500:
                return SERVER_ERROR
            return UNREADABLE
        if isinstance(err, (httpx.TimeoutException, TimeoutError)):
            return TIMED_OUT
        if isinstance(err, AdapterError):
            typed = typed_code_for_message(err.args[0] if err.args else None) or _specific(
                getattr(err, "code", None))
            if typed:
                return typed
    return UNREADABLE if isinstance(exc, AdapterError) else UNEXPECTED


def stored_code(counts: object, area: str = "SRC") -> str | None:
    """The code stored with a finished check, or None. Only a registered code of `area` counts,
    and anything but a dict of counts (a restored backup can hold any JSON) is no code."""
    if not isinstance(counts, dict):
        return None
    value = counts.get(ERROR_CODE_KEY)
    if not isinstance(value, str):
        return None
    entry = error_codes.lookup(value)
    return value if entry is not None and entry.area == area else None
