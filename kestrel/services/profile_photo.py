"""The Profile photo (D-UI-117, D-UI-112): one small picture chosen from this computer, kept in
the data folder and shown only inside Kestrel. It is never read by a document, an export, a
render or a model call, and nothing here sends it anywhere.

The file is recognized by its first bytes, not by its name or the type the browser claims, and
only a PNG, JPEG, WebP or GIF is kept. An SVG is refused: it can carry script."""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from kestrel import paths
from kestrel.errors import UserFacingError

log = structlog.get_logger()

MAX_BYTES = 2 * 1024 * 1024
FOLDER = "profile"
STEM = "photo"
TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


class PhotoError(UserFacingError, ValueError):
    """A photo that cannot be kept; the message is plain words for the person."""


def kind_of(data: bytes) -> str | None:
    """png, jpg, webp or gif from the first bytes, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def folder() -> Path:
    return paths.data_dir() / FOLDER


def current() -> Path | None:
    """The stored photo, or None."""
    for ext in TYPES:
        path = folder() / f"{STEM}.{ext}"
        if path.is_file():
            return path
    return None


def version() -> str | None:
    """A cache-busting stamp for the stored photo, or None when there is none."""
    path = current()
    try:
        return str(path.stat().st_mtime_ns) if path else None
    except OSError:
        return None


MAX_EDGE = 512  # the longest side kept, in pixels: it only ever shows as a small circle
# A small file can declare a huge canvas (a decompression bomb): decoding it would take gigabytes.
# The declared size is read from the header and checked BEFORE any pixel is decoded.
MAX_PIXELS = 40_000_000
TOO_BIG_PIXELS = "That picture is too large. Choose one under 40 megapixels (about 8000 by 5000)."
# Every refusal save() can raise: the Settings route answers from this list (D-SEC-4).
TOO_BIG = "Choose a photo smaller than 2 MB."
NOT_A_PICTURE = "Choose a PNG, JPEG, WebP or GIF picture."
NOT_SAVED = "Could not save the photo on this computer."
REFUSALS: tuple[str, ...] = (TOO_BIG_PIXELS, TOO_BIG, NOT_A_PICTURE, NOT_SAVED)


def _shrunk(data: bytes, ext: str) -> tuple[bytes, str]:
    """The picture resized to at most MAX_EDGE on its long side (D-UI-112). Best effort: a file
    Pillow cannot decode is kept as it came, since the type was already checked by its first
    bytes. A GIF is kept as its first frame in a PNG."""
    import io

    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.size[0] * img.size[1] > MAX_PIXELS:  # header only; nothing decoded yet
                raise PhotoError(TOO_BIG_PIXELS)
            img = ImageOps.exif_transpose(img)
            if max(img.size) <= MAX_EDGE and ext != "gif":
                return data, ext
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            out = io.BytesIO()
            if ext == "gif":
                img.convert("RGBA").save(out, "PNG")
                return out.getvalue(), "png"
            fmt = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[ext]
            if fmt == "JPEG":
                img = img.convert("RGB")
            img.save(out, fmt)
            return out.getvalue(), ext
    except Image.DecompressionBombError as exc:
        raise PhotoError(TOO_BIG_PIXELS) from exc
    except PhotoError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        # A decode problem keeps the original bytes.
        log.info("profile_photo_resize_skipped", error_type=type(exc).__name__)
        return data, ext


def save(data: bytes) -> Path:
    """Keep this picture as the photo, replacing any earlier one. Raises PhotoError."""
    if len(data) > MAX_BYTES:
        raise PhotoError(TOO_BIG)
    ext = kind_of(data)
    if ext is None:
        raise PhotoError(NOT_A_PICTURE)
    data, ext = _shrunk(data, ext)
    target_dir = folder()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        remove()
        target = target_dir / f"{STEM}.{ext}"
        incoming = target_dir / f"{STEM}.{ext}.part"
        incoming.write_bytes(data)
        os.replace(incoming, target)
    except OSError as exc:
        log.warning("profile_photo_write_failed", error_type=type(exc).__name__)
        raise PhotoError(NOT_SAVED) from exc
    return target


async def save_async(data: bytes) -> Path:
    """`save` off the event loop: decoding and resizing a picture is CPU work."""
    import asyncio

    return await asyncio.to_thread(save, data)


def remove() -> None:
    for ext in TYPES:
        (folder() / f"{STEM}.{ext}").unlink(missing_ok=True)
