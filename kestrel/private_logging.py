"""Keep custom service addresses and credentials out of stdlib and app logs."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

_active: ContextVar[tuple[str, ...]] = ContextVar("private_connection", default=())
_installed = False


def _values() -> set[str]:
    from kestrel.config import _dotenv_state

    saved = _dotenv_state["secrets"]
    values = set(_active.get())
    values.update(saved.get(k, "") for k in ("OTHER_SERVICE_URL_SECRET", "OTHER_SERVICE_API_KEY"))
    for value in tuple(values):
        if value.startswith(("https://", "http://")):
            try:
                values.add(urlsplit(value).hostname or "")
            except ValueError:
                pass  # A damaged private entry is still redacted in full.
    return values - {""}


def redact(text: str) -> str:
    for value in sorted(_values(), key=len, reverse=True):
        text = text.replace(value, "[private service]")
    return text


def install() -> None:
    """Redact before any handler (including third-party HTTP and exception logging)."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


@contextmanager
def private_values(*values: str) -> Iterator[None]:
    """Protect unsaved credentials during the operator's connection test too."""
    install()
    token = _active.set((*_active.get(), *values))
    try:
        yield
    finally:
        _active.reset(token)
