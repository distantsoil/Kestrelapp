"""Signed reranker files (D-SEC-13, follow-up to the restore hardening of issue #173).

A saved reranker is a pickle, and loading a pickle can run code. So every artifact Kestrel writes
is signed with HMAC-SHA256 under a per-install secret key, the signature is stored in the
`ml_model` row, and the loader verifies it BEFORE any unpickling. A file that was edited, written
by another install (another key) or saved before signing existed has no valid signature and is
refused; scoring falls back to the LLM tier (hard rule 4).

The key is 32 random bytes in `secrets/model_signing.key` under the data folder, created on first
use with owner-only permissions where the OS supports it. It is never part of a backup and never
restored from one (kestrel/services/backup.py), so a backup cannot carry a key that makes its own
files verify."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

import structlog

from kestrel import paths

log = structlog.get_logger(__name__)

KEY_BYTES = 32


def _read_key(path: Path) -> bytes | None:
    try:
        key = path.read_bytes()
    except OSError:
        return None
    return key if len(key) == KEY_BYTES else None


def load_key() -> bytes | None:
    """The existing signing key, or None (no key yet, or an unreadable or wrong-sized file)."""
    return _read_key(paths.model_signing_key_path())


def load_or_create_key() -> bytes:
    """The signing key, created with owner-only permissions on first use. A wrong-sized file is
    replaced: nothing signed under it could be trusted anyway (those rerankers retrain)."""
    path = paths.model_signing_key_path()
    key = _read_key(path)
    if key is not None:
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(KEY_BYTES)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # best effort; a no-op for the owner-only bits on Windows
    except OSError:
        log.info("model_signing_key_chmod_skipped")
    return _read_key(path) or key


def sign_bytes(data: bytes, key: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def sign_file(path: str | Path) -> str:
    """HMAC-SHA256 (hex) of the file's bytes under this install's key (created if needed)."""
    return sign_bytes(Path(path).read_bytes(), load_or_create_key())


def read_verified(path: str | Path | None, signature: str | None) -> bytes | None:
    """The file's bytes if `signature` is this install's valid signature of them, else None.

    The bytes that were verified are the bytes returned, so the caller unpickles exactly what was
    checked (no window between the check and the read). Never creates a key: with none, nothing
    on this install could have been signed."""
    if not path or not signature:
        return None
    key = load_key()
    if key is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not hmac.compare_digest(sign_bytes(data, key), str(signature)):
        return None
    return data
