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


def plain_os_reason(exc: OSError) -> str:
    """The OS reason for a failure without the file name it carries ("permission was denied")."""
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
