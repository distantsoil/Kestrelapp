"""Plain-language text for an exception that is about to be shown in a response.

Exception text can carry a file path, a URL or a key, so it is never put in a response. The
caller logs the real exception (structlog, with context) and shows this instead."""

from __future__ import annotations

import sqlite3
import zipfile

from kestrel.errors import UserFacingError, plain_os_reason

GENERIC = "an unexpected problem; the details are in the log"


def _generic(exc: BaseException) -> str:
    """The exception TYPE name (a class name, never its text) plus a pointer to the log."""
    return f"an unexpected problem ({type(exc).__name__}); the details are in the log"


def plain_error(exc: BaseException) -> str:
    """A short, safe reason: a Kestrel-authored message as written (UserFacingError), the OS
    reason without its file name, else a generic sentence. Never the text of any other
    exception."""
    if isinstance(exc, UserFacingError):
        return str(exc)
    if isinstance(exc, OSError):
        return plain_os_reason(exc)
    if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile)):
        return "the file is not a usable zip archive"
    if isinstance(exc, sqlite3.Error):
        return "the database could not be read"
    return _generic(exc)
