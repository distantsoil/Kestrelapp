"""Plain-language text for an exception that is about to be shown in a response.

Exception text can carry a file path, a URL or a key, so it is never put in a response. The
caller logs the real exception (structlog, with context) and shows this instead."""

from __future__ import annotations

import errno
import sqlite3
import zipfile
from collections.abc import Iterable

from sqlalchemy.exc import SQLAlchemyError

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


# What an OS failure means in Kestrel's own words, by errno. A value, never a piece of the
# exception, so a path or a library message cannot ride along.
_OS_REASONS: dict[int, str] = {
    errno.EACCES: "permission was denied",
    errno.EPERM: "permission was denied",
    errno.ENOENT: "the file was not found",
    errno.ENOSPC: "there is no space left on the disk",
    errno.EROFS: "the disk is read-only",
    errno.EBUSY: "the file is in use by another program",
    errno.EISDIR: "a folder was found where a file was expected",
    errno.ENOTDIR: "a file was found where a folder was expected",
}
OS_DEFAULT = "a file could not be read or written"


def fixed_reason(exc: BaseException) -> str:
    """A reason for a failure that is one of Kestrel's own sentences and nothing else (D-SEC-4).

    Every return value is a string written in this file, picked by the exception's TYPE or errno.
    No text, argument or attribute of the exception is ever copied into the result, so a path,
    a URL or a key in the exception cannot reach a response, and a code scanner can follow that
    from the source to the page. Use it where a response is built straight from a failure and the
    exception is not one of Kestrel's own authored errors; `plain_error` stays for the places that
    must show an authored message. The exception itself goes to the log by the caller."""
    if isinstance(exc, PermissionError):
        return "permission was denied"
    if isinstance(exc, FileNotFoundError):
        return "the file was not found"
    if isinstance(exc, TimeoutError):
        return "it took too long"
    if isinstance(exc, OSError):
        return _OS_REASONS.get(exc.errno or 0, OS_DEFAULT)
    if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile)):
        return "the file is not a usable zip archive"
    if isinstance(exc, (sqlite3.Error, SQLAlchemyError)):
        return "the database could not be read"
    return GENERIC


LINE_SLOT = "{line}"  # in a sentence below: stands for one whole number, a YAML line


def authored_error(exc: BaseException, sentences: Iterable[str], fallback: str) -> str:
    """One of Kestrel's own sentences for an error whose messages are a short, closed list.

    `sentences` is the whole list the raising code can use (module constants next to the `raise`
    lines). The exception's text is only compared with that list to pick the entry to show; the
    result is always an entry of `sentences` or `fallback`, never a copy of the exception's text,
    so a message that is not on the list (a library's, or one built from an OS error) shows the
    fallback instead (D-SEC-4). The caller logs the exception.

    One entry may hold `{line}` once, for a message that names a line of a file ("... is not valid
    YAML (line 12)."). It matches a whole number of up to six digits in that place, and the result
    is the entry with that number written back from its digits: nothing else of the message can
    pass through."""
    text = str(exc)
    for sentence in sentences:
        if LINE_SLOT not in sentence:
            if text == sentence:
                return sentence
            continue
        head, tail = sentence.split(LINE_SLOT, 1)
        if text.startswith(head) and text.endswith(tail):
            digits = text[len(head):len(text) - len(tail)]
            if digits.isascii() and digits.isdigit() and len(digits) <= 6:
                return f"{head}{int(digits)}{tail}"
    return fallback
