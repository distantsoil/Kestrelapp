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
from kestrel.errors import UserFacingError

log = structlog.get_logger(__name__)

KEY_BYTES = 32

MAX_ARTIFACT_BYTES = paths.MAX_MODEL_FILE_BYTES


# Why a key file is refused. A file Kestrel can put aside and replace is one that is plainly not
# a key; a read error or a folder in the wrong place is not replaced (a passing fault would
# otherwise throw away a good key).
_SET_ASIDE = ("a link", "not a regular file", "the wrong size")


def _read_key(path: Path) -> tuple[bytes | None, str]:
    """(key, "") or (None, why): why is "" when there is simply no key yet, else the reason the
    file there is refused. A second name for the key (a hard link left by a snapshot or copy
    tool) is accepted: the key is read, never shared, and whoever could make one could write
    the key anyway."""
    import stat as _stat

    # Whether the folder is there first: checked the other way round, a folder another creator
    # makes between the two checks would read as "not a plain folder".
    if os.path.lexists(path.parent) and paths.own_folder(path.parent) is None:
        return None, "the secrets folder is not a plain folder"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, ""
    except OSError:
        return None, "unreadable"
    if _stat.S_ISLNK(info.st_mode):
        return None, "a link"
    if not _stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    if info.st_size != KEY_BYTES:
        return None, "the wrong size"
    key = paths.read_regular_file(path, KEY_BYTES, single_name=False)
    if key is None or len(key) != KEY_BYTES:
        return None, "unreadable"
    return key, ""


def load_key() -> bytes | None:
    """The existing signing key, or None (no key yet, or a file there that is refused, which is
    logged with the reason: every signed model is then refused until it retrains)."""
    key, why = _read_key(paths.model_signing_key_path())
    if why:
        log.warning("model_signing_key_refused", reason=why)
    return key


# What the person reads about a key Kestrel cannot use (Settings > Scoring and learning, the
# train route), one sentence per _read_key reason.
KEY_PROBLEMS: dict[str, str] = {
    "a link": "the key file is a link",
    "not a regular file": "the key file is not an ordinary file",
    "the wrong size": "the key file is the wrong size",
    "unreadable": "the key file could not be read",
    "the secrets folder is not a plain folder": (
        "the folder that holds it is a link or is not a plain folder"),
}
KEY_UNUSABLE = (
    "Kestrel cannot use its model signing key: {problem}. A new key can be made on Settings, "
    "Scoring and learning."
)
KEY_UNUSABLE_SENTENCES: tuple[str, ...] = tuple(
    KEY_UNUSABLE.format(problem=p) for p in KEY_PROBLEMS.values())


class SigningKeyUnavailable(UserFacingError, PermissionError):
    """The key cannot be used and was left as it is (a read error, or a secrets folder out of
    place). Its message is one of KEY_UNUSABLE_SENTENCES."""


def key_problem() -> str:
    """"" when the key is usable or there is none yet (one is made on first use), else the
    KEY_PROBLEMS sentence for why Kestrel cannot use it. Settings > Scoring and learning
    then offers a new key."""
    _key, why = _read_key(paths.model_signing_key_path())
    return KEY_PROBLEMS.get(why, KEY_PROBLEMS["unreadable"]) if why else ""


def load_or_create_key() -> bytes:
    """The signing key, created with owner-only permissions on first use.

    A file in the key's place that is plainly not a key (a link, the wrong size, not a regular
    file) is never overwritten in place: it is renamed aside to
    `model_signing.key.refused-<time>` (a link is renamed as a link; whatever it points at is
    untouched), the replacement is logged at
    warning with the reason and the kept name, and a new key is written (D-SEC-55). Models
    signed under the old key are refused and retrain. A key that cannot be read for another
    reason (a read error, a secrets folder that is a link) is left alone and nothing is written:
    this raises SigningKeyUnavailable, and the save that needed the key fails and says so. The
    person can then make a new key on Settings > Scoring and learning (replace_key).

    A new key never replaces one that is there: it is written under a unique temporary name
    and given the key's name only if that name is free (an exclusive link), so when two
    creators race, the first key wins and the other reuses it."""
    path = paths.model_signing_key_path()
    key, why = _read_key(path)
    if key is not None:
        return key
    if why and why not in _SET_ASIDE:
        log.warning("model_signing_key_not_replaced", reason=why)
        raise SigningKeyUnavailable(KEY_UNUSABLE.format(problem=KEY_PROBLEMS[why]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if paths.own_folder(path.parent) is None:
        why = "the secrets folder is not a plain folder"
        log.warning("model_signing_key_not_replaced", reason=why)
        raise SigningKeyUnavailable(KEY_UNUSABLE.format(problem=KEY_PROBLEMS[why]))
    if why:
        kept = path.with_name(_set_aside(path, why, by="kestrel"))
        # Another creator may have put a good key in place between our read and the rename:
        # then what we moved is that key, and it goes straight back (exclusively).
        theirs, _ = _read_key(kept) if os.path.lexists(kept) else (None, "")
        if theirs is not None:
            try:
                os.link(kept, path)
            except FileExistsError:
                pass
            except OSError:
                log.warning("model_signing_key_not_put_back", kept_as=kept.name)
            else:
                kept.unlink(missing_ok=True)
                log.info("model_signing_key_put_back")
                return theirs
    else:
        log.info("model_signing_key_created")
    return _create_exclusive(path)


def replace_key() -> str | None:
    """Settings > Scoring and learning's "Make a new signing key": the one deliberate
    replacement of a key Kestrel cannot use, whatever the reason (a key that cannot be read,
    a secrets folder that is a link). The file, or the secrets folder, is renamed aside (never
    deleted, never followed), logged at warning, and a new key is made. Returns the name it
    was kept under, or None when the key was usable (or absent) and nothing was changed: a
    usable key is never replaced."""
    path = paths.model_signing_key_path()
    _key, why = _read_key(path)
    if not why:
        return None
    folder = path.parent
    if why == "the secrets folder is not a plain folder":
        kept = _set_aside(folder, why, by="person")
        folder.mkdir(parents=True, exist_ok=True)
        if paths.own_folder(folder) is None:  # something put a link back at once
            raise SigningKeyUnavailable(KEY_UNUSABLE.format(problem=KEY_PROBLEMS[why]))
    else:
        kept = _set_aside(path, why, by="person")
    _create_exclusive(path)
    return kept


def _set_aside(path: Path, why: str, *, by: str) -> str:
    """Rename `path` (a key file, a link, or the secrets folder) aside with the time in its name,
    as itself: a link is renamed as a link. Logged at warning. Returns the kept name."""
    from datetime import UTC, datetime

    kept = path.with_name(
        f"{path.name}.refused-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}")
    try:
        os.replace(path, kept)
    except FileNotFoundError:  # another creator set it aside first
        return kept.name
    log.warning("model_signing_key_replaced", reason=why, kept_as=kept.name, by=by)
    return kept.name


def _create_exclusive(path: Path) -> bytes:
    """Write a new key under a unique name (O_EXCL, never through a link) and link it to the
    key's name only if that is free. If a key appeared there first, that key is used."""
    import secrets as _secrets

    key = os.urandom(KEY_BYTES)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{_secrets.token_hex(8)}.tmp")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)  # best effort; a no-op for the owner-only bits on Windows
        except OSError:
            log.info("model_signing_key_chmod_skipped")
        try:
            os.link(tmp, path)  # exclusive: fails when a key is already there
        except FileExistsError:
            log.info("model_signing_key_created_elsewhere")
        except OSError:  # a file system without hard links: create the name exclusively
            _write_exclusive(path, key, flags)
    finally:
        tmp.unlink(missing_ok=True)
    # Whatever key now holds the name is the one used, so every model is signed with the key
    # that is on disk: ours, or the one a racing creator put there first.
    on_disk, why = _read_key(path)
    if on_disk is None:
        log.warning("model_signing_key_not_replaced", reason=why or "unreadable")
        raise SigningKeyUnavailable(KEY_UNUSABLE.format(
            problem=KEY_PROBLEMS.get(why, KEY_PROBLEMS["unreadable"])))
    return on_disk


def _write_exclusive(path: Path, key: bytes, flags: int) -> None:
    """The key written straight to its own name, created exclusively (never over a key that is
    there, never through a link). Used only where the file system has no hard links."""
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return  # a key appeared first; the caller reads and uses it
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())


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
    data = read_model_file(path)
    if data is None:
        return None
    if not hmac.compare_digest(sign_bytes(data, key), str(signature)):
        return None
    return data


def read_model_file(stored: str | Path | None) -> bytes | None:
    """The bytes of a model file named by the database, or None.

    A stored path is never read as it stands: it must resolve to an entry directly in the ml
    folder (which must not itself be a link), and that entry is read by name relative to the
    folder (paths.read_ml_file): a regular file only, never through a link, at most
    MAX_ARTIFACT_BYTES. So a row pointing at another file, a device or a pipe, directly or
    through a link, reads nothing."""
    folder = paths.ml_folder()
    if folder is None:
        return None
    target = paths.confined_file(stored, folder)
    if target is None or target.parent != folder:
        return None
    return paths.read_ml_file(target.name, MAX_ARTIFACT_BYTES)
