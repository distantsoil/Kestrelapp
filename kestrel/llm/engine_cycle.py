"""One answer about the local AI engine per worker cycle, and the type that says a model call
could not reach it (D-WORKER-6 amended, round 11; docs/handoff/196-redesign.md).

The cycle takes one fresh probe at its start (`start`), and its work check and every drain read
that answer. The answer says "up" only until a model call cannot reach Ollama: the client marks
the engine lost (`mark_lost`), the answer is "down" for the rest of the cycle, and every drain on
Ollama stops leasing (worker.engine_lost_for, checked before each lease). That is all the cycle
state does: it never decides what a task is charged.

What a task is charged is decided from the failure's type and the task's stored row. The client
re-raises a call to Ollama's own address that failed to connect or lost its connection as the same
exception with `EngineUnreachable` mixed into its class (`lost_from`), so every existing reading of
the error by type or name is unchanged. The guard and each drain carry that fact to
TaskQueue.fail(engine_wait=...), which writes the wait on the row and gives back the one try the
lease charged. A timeout is not in the set: Ollama took the connection and the model was slow, so
it is the task's own and spends. No probe runs in the error path, and nothing is held per task.
"""

from __future__ import annotations

import types
from typing import Any, cast
from urllib.parse import urlsplit

import structlog

log = structlog.get_logger()

# {Ollama address: probe result (version or None, installed tags)} for the running cycle.
_PROBE: dict[str, Any] = {}
# Ollama addresses whose calls failed in transport during the running cycle.
_LOST: set[str] = set()
_ACTIVE = {"on": False}
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}


def same_engine(a: str | None, b: str | None) -> bool:
    """Whether two addresses name the same server: scheme defaults filled in, and every
    loopback spelling (localhost, 127.0.0.1, ::1) treated as one host."""
    def key(url: str | None) -> tuple[str, int] | None:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not host:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return ("loopback" if host in _LOOPBACK else host), port

    ka = key(a)
    return ka is not None and ka == key(b)


def start(url: str | None, probe: Any = None) -> None:
    """Begin a cycle: forget the last one, then hold this cycle's answer (when one was taken)."""
    _PROBE.clear()
    _LOST.clear()
    _ACTIVE["on"] = True
    if url is not None:
        _PROBE[url] = probe


def stop() -> None:
    """End the cycle: nothing outside a cycle reads a stale answer or a stale loss."""
    _PROBE.clear()
    _LOST.clear()
    _ACTIVE["on"] = False


def answer(url: str) -> tuple[bool, Any]:
    """(whether the cycle holds an answer for this address, that answer)."""
    if url in _PROBE:
        return True, _PROBE[url]
    return False, None


def lost() -> bool:
    """Whether a model call in the running cycle failed to reach Ollama."""
    return bool(_LOST)


class EngineUnreachable(Exception):
    """Marker for a model call that could not reach Ollama: it failed to connect or lost its
    connection. Mixed into the original exception's class by `lost_from`; raised directly only
    as EngineWaitError."""


class EngineWaitError(EngineUnreachable, RuntimeError):
    """Raised by a drain whose guarded call came back `engine_unreachable` (the guard keeps the
    fact, not the exception), so its task still waits instead of spending a try."""


_MARKED: dict[type, type[BaseException]] = {}


def _lost_types() -> tuple[type, ...]:
    import httpx

    return (httpx.NetworkError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
            ConnectionRefusedError)


def lost_from(exc: BaseException) -> BaseException | None:
    """The same failure with `EngineUnreachable` mixed into its class, or None when `exc` is not
    a failure to connect or a lost connection (a timeout, a protocol error of the client's own).
    The marked class keeps the original's name and bases, so isinstance checks and the error
    sentences and codes read exactly as before."""
    if isinstance(exc, EngineUnreachable):
        return exc
    if not isinstance(exc, _lost_types()):
        return None
    cls = type(exc)
    marked = _MARKED.get(cls)
    if marked is None:
        marked = cast("type[BaseException]",
                      types.new_class(cls.__name__, (cls, EngineUnreachable)))
        marked.__module__ = cls.__module__
        _MARKED[cls] = marked
    new = marked.__new__(marked, *exc.args)
    new.args = exc.args
    new.__dict__.update(exc.__dict__)
    new.__traceback__ = exc.__traceback__
    return new


def engine_unreachable(exc: BaseException | None) -> bool:
    """Whether `exc`, or an exception it was explicitly raised from (`raise ... from`), is a call
    that could not reach Ollama. Read by type only. The implicit context is not followed: an
    error of the drain's own raised while handling a lost connection is the drain's, so it
    spends (round 12)."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, EngineUnreachable):
            return True
        exc = exc.__cause__
        seen += 1
    return False


def mark_lost(url: str) -> None:
    """Called by the model client when a call to Ollama's address could not reach it: during a
    cycle, the rest of the cycle leases nothing on Ollama. Decides nothing about the task."""
    if not _ACTIVE["on"]:
        return
    if url not in _LOST:
        log.warning("engine_lost_mid_cycle")
    _LOST.add(url)
    _PROBE[url] = (None, [])


def classify(settings: Any, backend: Any, exc: BaseException) -> BaseException | None:
    """`exc` marked EngineUnreachable when the call went to Ollama's own address and failed to
    connect or lost its connection; None otherwise (an online backend, a timeout, any other
    error). A marked failure also marks the running cycle lost, which only stops new leases."""
    if not same_engine(getattr(backend, "base_url", ""), settings.llm.ollama_native_url):
        return None  # an online backend or another server is never Ollama's address
    lost = lost_from(exc)
    if lost is not None:
        mark_lost(settings.llm.ollama_native_url)
    return lost

