"""A safe Content-Disposition header for a download whose name comes from a person's own file."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

_UNSAFE = re.compile(r'[\x00-\x1f\x7f"\\/;:*?<>|%]')


def attachment_disposition(name: str, fallback: str = "download.csv") -> str:
    """`attachment` with an ASCII `filename` every client can read and, when the name has other
    letters, an RFC 5987 `filename*` that carries them exactly. Header values are latin-1 only, so
    a raw non-ASCII name would raise; control characters and quotes are never passed through."""
    clean = _UNSAFE.sub("", str(name)).strip() or fallback
    ascii_name = (
        unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii").strip(" .")
    )
    ext = "." + fallback.rsplit(".", 1)[-1] if "." in fallback else ""
    if not ascii_name or ascii_name == ext.lstrip("."):
        ascii_name = fallback
    elif ext and "." not in ascii_name:
        ascii_name += ext
    value = f'attachment; filename="{ascii_name}"'
    if ascii_name != clean:
        value += f"; filename*=UTF-8''{quote(clean, safe='')}"
    return value


def pdf_download(data: bytes, name: str):
    """A PDF download built from bytes already read and checked (paths.read_servable), never
    from a path the response would open again later (D-SEC-55)."""
    from fastapi.responses import Response

    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": attachment_disposition(name, "document.pdf")})


PDF_MISSING = "The PDF file is missing from disk."


def pdf_not_served(why: str) -> str:
    """What a download says when paths.read_servable_why refused the file: missing only when it
    is missing, otherwise the true reason, so a refused link is never called a missing file."""
    from kestrel import paths

    if why == paths.LEFT_OUT_MISSING or why not in paths.LEFT_OUT_REASONS:
        return PDF_MISSING
    return f"The PDF was not served: {why}."
