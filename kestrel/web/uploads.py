"""Upload lifecycle: an UploadFile must be closed, not merely read (D-RR1-4).

Starlette's `UploadFile` wraps a `SpooledTemporaryFile`. Small uploads stay in memory, but past
the spool threshold it rolls to disk and holds an OS file handle for as long as it is open. Nothing
in the framework closes it: `request.form()` hands the route an object, the route reads it, and when
the request ends the last reference goes away and `SpooledTemporaryFile.__del__` reports
`ResourceWarning: Unclosed file` while ignoring the exception.

It is not cosmetic. An unclosed handle is a Windows sharing violation, the same class as the
restore lock bug: a file held open that something else needs to move or replace.

The rule is stated once, here, rather than repeated as a `finally` at each call site where one
omission would silently reintroduce it. Two shapes:

- `closing_upload`, a context manager, when the route does more work with the upload in scope and
  has early returns inside that work (documents.py's read-and-convert block).
- `read_upload`, when the route only needs the bytes, optionally size-limited.

Both close through `_close`, which is shielded from cancellation: a client disconnect cancels the
request, and an unshielded close of a rolled-to-disk upload (a threadpool await) would itself be
cancelled, leaving the handle open.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import structlog

log = structlog.get_logger()

# Chunk size for a size-limited read: large enough to be quick, small enough that an oversize
# upload is refused long before it is fully buffered.
_CHUNK = 1 << 20


class UploadTooLarge(Exception):
    """A generic over-limit refusal, for callers with no error type of their own."""


async def _close(upload: Any) -> None:
    """Close the upload, shielded from cancellation. A failure to close is logged and swallowed:
    cleanup must never mask the result of the request that is already finishing, and turning a
    successful upload into a 500 because the temp file would not delete is worse than the leak."""
    try:
        with anyio.CancelScope(shield=True):
            await upload.close()
    except Exception as exc:  # noqa: BLE001 - cleanup, never the request's own outcome
        log.warning(
            "upload_close_failed",
            filename=str(getattr(upload, "filename", "") or "")[:200],
            error=f"{type(exc).__name__}: {exc}",
        )


@contextlib.asynccontextmanager
async def closing_upload(upload: Any) -> AsyncIterator[Any]:
    """Yield the upload and close it on every exit path: success, refusal, and an exception raised
    while reading or converting. The last matters most, because a converter that raises on a
    hostile document is exactly when the reference would otherwise be dropped unread."""
    try:
        yield upload
    finally:
        await _close(upload)


async def read_upload(
    upload: Any,
    *,
    limit: int | None = None,
    too_large: Callable[[], BaseException] | None = None,
) -> bytes:
    """The upload's bytes, then the upload closed.

    With `limit`, the declared size is checked first when the multipart parser knows it, then the
    body is read in chunks and `too_large()` is raised the moment it passes the limit, so an
    oversize file is never fully buffered."""
    async with closing_upload(upload):
        if limit is None:
            return await upload.read()
        if too_large is None:
            raise ValueError("read_upload: a limit needs a too_large factory")
        declared = getattr(upload, "size", None)
        if isinstance(declared, int) and declared > limit:
            raise too_large()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise too_large()
            chunks.append(chunk)
        return b"".join(chunks)


LARGE_FIELD = 25 * 1024 * 1024 * 6  # a 25 MB file, url-encoded: a line break is %0D%0A


async def large_form(request: Any, max_rows: int) -> Any:
    """The form of an import step. These carry the whole file from one step to the next (a
    hidden field) and up to three fields per row, so Starlette's defaults (1 MB a url-encoded
    field, 1,000 fields) refused a tracker the importer accepts with a 400 the page never
    showed (issue #50, D-IMPORT-7). The limits follow the importers' own caps; the app listens
    on 127.0.0.1 only."""
    return await request.form(max_fields=max_rows * 3 + 100, max_part_size=LARGE_FIELD)
