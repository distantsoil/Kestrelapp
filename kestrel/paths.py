"""Path and packaged-resource resolution. This module is imported before everything else and has
no Kestrel dependencies of its own (config.py depends on it, never the reverse).

Rules it enforces (CLAUDE.md, SPEC.md 2, SPEC-INTERFACES.md 1.4):
  - Nothing writable lives beside the executable. All writes go under the per-OS data home:
      Windows  %LOCALAPPDATA%\\Kestrel (the packaged desktop app's home, unchanged)
      macOS    ~/Library/Application Support/Kestrel
      Linux    $XDG_DATA_HOME/kestrel, else ~/.local/share/kestrel
    KESTREL_DATA_HOME overrides all three (tests, and an operator who wants it elsewhere).
  - Packaged defaults (config/*.yml, models/Modelfile.*) are accessed via importlib.resources,
    never __file__-relative arithmetic, so the frozen read-only install works identically to dev.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

APP_NAME = "Kestrel"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_home() -> Path:
    """The writable root, per OS (see the module docstring), overridable with KESTREL_DATA_HOME."""
    override = os.environ.get("KESTREL_DATA_HOME")
    if override:
        return Path(override)
    return default_data_home()


def default_data_home() -> Path:
    """The per-OS default data home, ignoring KESTREL_DATA_HOME (the single-instance lock compares
    against it, and the installer's AppMutex belongs to this folder only)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(local) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME.lower()


def data_dir() -> Path:
    return data_home() / "data"


def config_dir() -> Path:
    return data_home() / "config"


def logs_dir() -> Path:
    return data_home() / "logs"


def dotenv_path() -> Path:
    return data_home() / ".env"


def runtime_json() -> Path:
    return data_home() / "runtime.json"


def startup_health_json() -> Path:
    """How many starts in a row have not finished (kestrel.startup_health, D-UI-252). Next to
    runtime.json, outside the database, so it can be read when the database is the problem."""
    return data_home() / "startup.json"


def window_state_json() -> Path:
    """The desktop window's own remembered state (its light or dark theme), read before the
    loading page paints so the first frame matches (D-UI-89)."""
    return data_home() / "window.json"


def db_path() -> Path:
    return data_dir() / "kestrel.db"


def sounds_dir() -> Path:
    """Where Kestrel keeps the alert sounds it renders for itself (kestrel.services.alert_sound):
    one small WAV per sound and volume. A cache: deleting it only makes the next alert draw them
    again."""
    return data_home() / "cache" / "sounds"


def ml_dir() -> Path:
    """Where the reranker and its fitted PCA are persisted (SPEC-MODULES 5.4). Lives under the
    writable data dir, never beside the read-only packaged app."""
    return data_dir() / "ml"


def ml_folder() -> Path | None:
    """The ml folder, resolved, for reading or removing model files; None when it is missing or
    is itself a link (a symlink or a junction). Kestrel creates it as a plain folder, so a link
    in its place was put there by someone else and nothing is read from or deleted through it."""
    return own_folder(ml_dir())


def own_folder(folder: Path) -> Path | None:
    """`folder`, resolved, when it is a real folder in its own place; None when it is missing or
    is itself a link (a symlink or a junction). Kestrel creates its folders with mkdir, so a link
    in their place was put there by someone else and nothing is read from or shared through it."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return None
        resolved = folder.resolve()
        # A Windows junction is not a symlink to is_symlink(), but it does move where the folder
        # resolves to: the folder must resolve to its own name inside its resolved parent.
        if resolved != folder.parent.resolve() / folder.name:
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


# The only files Kestrel writes in the ml folder (learning/reranker._save_model). Reset removes,
# backup copies and the Learning tab credits to Kestrel only entries directly in that folder
# named like these, and nothing else: an operator's own file there is never touched.
ML_FILE_PATTERNS: tuple[str, ...] = ("reranker_*.pkl", "features_*.pkl")
# The largest model file Kestrel reads back or copies into a backup. A saved reranker and its
# fitted PCA are a few megabytes at most; the cap only stops a file from being something huge.
MAX_MODEL_FILE_BYTES = 256 * 1024 * 1024

# Where the OS can work relative to an open folder, every ml-folder operation below does: the
# folder is opened once without following a link, and each name is listed, checked, read or
# removed relative to that open folder, so swapping the folder for a link mid-operation reaches
# nothing outside it. Elsewhere (Windows) the same checks run on paths, between which a swap by
# someone already able to write the data folder remains possible (D-SEC-51).
_DIR_FD = (
    hasattr(os, "O_DIRECTORY")
    and os.listdir in os.supports_fd
    and os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Only used where _DIR_FD is true, which needs os.O_DIRECTORY to exist; read this way so the code
# also type-checks as Windows sees it, where the name is absent and these calls never run.
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | _NOFOLLOW


def is_model_file_name(name: str) -> bool:
    """Named exactly as Kestrel names its model files: plain ASCII, no folder part, and matched
    with case (so an operator's `Reranker_Notes.PKL` on a Windows volume is not one)."""
    import fnmatch

    return (_plain_name(name) and name.isascii()
            and any(fnmatch.fnmatchcase(name, p) for p in ML_FILE_PATTERNS))


def _plain_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


_REFUSED = -1  # the OS can open the folder relative-safe, and opening it was refused


@contextmanager
def _ml_dir_fd() -> Iterator[int | None]:
    """The ml folder opened without following a link, for the caller to work relative to.
    None where the OS cannot do that (the caller then works on paths); _REFUSED when it can but
    the folder would not open that way (it became a link, or went away): do nothing then."""
    if not _DIR_FD:
        yield None
        return
    try:
        fd = os.open(ml_dir(), os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError:
        yield _REFUSED
        return
    try:
        yield fd
    finally:
        os.close(fd)


def ml_entries() -> list[tuple[str, os.stat_result]]:
    """Each entry directly in the ml folder with its own status (a link is described as a link,
    never followed), sorted by name. Empty when the folder is missing, a link or unreadable."""
    folder = ml_folder()
    if folder is None:
        return []
    out: list[tuple[str, os.stat_result]] = []
    with _ml_dir_fd() as fd:
        if fd == _REFUSED:
            return []
        try:
            names = os.listdir(fd) if fd is not None else os.listdir(folder)
        except OSError:
            return []
        for name in sorted(names):
            if not _text_name(name):  # cannot be listed on a page or named in a zip
                continue
            try:
                info = (os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None
                        else os.lstat(folder / name))
            except OSError:
                continue
            out.append((name, info))
    return out


def read_regular_file(
    path: Path, limit: int, *, dir_fd: int | None = None, single_name: bool = True,
) -> bytes | None:
    """At most `limit` bytes of a regular file, or None (missing, a link, not a regular file,
    a file with a second name elsewhere (a hard link), larger than `limit`, unreadable). The open
    never follows a link in the last part of the path where the OS can refuse one, and never
    blocks, so a pipe or a device in its place reads nothing. Every file Kestrel writes has one
    name, so a hard link was made by someone else, and its other name may be outside."""
    return _read_regular(path, limit, dir_fd, single_name=single_name)[0]


def _read_regular(
    path: Path, limit: int, dir_fd: int | None, *, single_name: bool = True,
) -> tuple[bytes | None, str]:
    """read_regular_file, with why nothing was read ("" when something was): one of the
    LEFT_OUT_* sentences, never the OS's own text (which can carry a path)."""
    try:
        fd = os.open(path, _READ_FLAGS, dir_fd=dir_fd) if dir_fd is not None else os.open(
            path, _READ_FLAGS)
    except OSError as exc:
        return None, _open_failure(exc)
    try:
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            why = ""
            if not stat.S_ISREG(info.st_mode):
                why = LEFT_OUT_NOT_A_FILE
            elif single_name and info.st_nlink != 1:
                why = LEFT_OUT_SECOND_NAME
            elif info.st_size > limit:
                why = LEFT_OUT_TOO_LARGE
            if why:
                import structlog

                structlog.get_logger().warning("stored_file_refused", reason=why)
                return None, why
            data = fh.read(limit + 1)
    except OSError as exc:
        return None, _open_failure(exc)
    if len(data) > limit:  # it grew after the size check
        return None, LEFT_OUT_TOO_LARGE
    return data, ""


def _open_failure(exc: OSError) -> str:
    """Why an open or read failed, as a LEFT_OUT_* sentence. An open with O_NOFOLLOW meets a
    link in the last part as ELOOP (EMLINK on some BSDs)."""
    import errno

    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return LEFT_OUT_LINK
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return LEFT_OUT_MISSING
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return LEFT_OUT_DENIED
    return LEFT_OUT_UNREADABLE


def read_ml_file(name: str, limit: int) -> bytes | None:
    """At most `limit` bytes of the regular file `name` directly in the ml folder, or None. A
    link there, or the folder itself being a link, reads nothing."""
    folder = ml_folder()
    if folder is None or not _plain_name(name):
        return None
    with _ml_dir_fd() as fd:
        if fd == _REFUSED:
            return None
        if fd is not None:
            return read_regular_file(Path(name), limit, dir_fd=fd)
        if (folder / name).is_symlink():
            return None
        return read_regular_file(folder / name, limit)


def remove_ml_file(name: str) -> bool:
    """Remove the entry `name` directly in the ml folder as it stands (a link goes as a link and
    never takes the file it points at). False when there is nothing to remove, it is a folder,
    or it could not be removed (logged by error type)."""
    folder = ml_folder()
    if folder is None or not _plain_name(name):
        return False
    with _ml_dir_fd() as fd:
        if fd == _REFUSED:
            return False
        try:
            info = (os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None
                    else os.lstat(folder / name))
            if stat.S_ISDIR(info.st_mode):
                return False
            if fd is not None:
                os.unlink(name, dir_fd=fd)
            else:
                (folder / name).unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:  # a link to a folder on Windows, a locked file
            import structlog

            structlog.get_logger().warning("ml_file_not_removed", error=type(exc).__name__)
            return False
    return True


# The largest single file Kestrel copies into something the person shares (a backup or a
# diagnostics bundle). Logs rotate at 5 MB and model files are a few MB; the cap only stops a
# file from being something huge.
MAX_SHARED_FILE_BYTES = 256 * 1024 * 1024
_realpath = os.path.realpath  # this module's own name for it, so a test can stand in for it

# Why a file was left out of something the person shares (a backup's note, the diagnostics
# bundle, a download that is refused). Each ends a sentence that starts with the file's name, so
# a refused link reads as a link and never as a missing file.
LEFT_OUT_LINK = "it is a link, and Kestrel does not follow links"
LEFT_OUT_SECOND_NAME = "it has a second name elsewhere on this computer (a hard link)"
LEFT_OUT_NOT_A_FILE = "it is not an ordinary file"
LEFT_OUT_TOO_LARGE = "it is larger than Kestrel copies"
LEFT_OUT_UNREADABLE = "it could not be read"
LEFT_OUT_DENIED = "permission to read it was denied"
LEFT_OUT_MISSING = "it is not there"
LEFT_OUT_NAME_NOT_TEXT = "its name is not text"
LEFT_OUT_FOLDER = "its folder is a link or is not in its own place"
LEFT_OUT_OUTSIDE = "it is not inside Kestrel's own folder for it"
LEFT_OUT_REASONS: tuple[str, ...] = (
    LEFT_OUT_LINK, LEFT_OUT_SECOND_NAME, LEFT_OUT_NOT_A_FILE, LEFT_OUT_TOO_LARGE,
    LEFT_OUT_UNREADABLE, LEFT_OUT_DENIED, LEFT_OUT_MISSING, LEFT_OUT_NAME_NOT_TEXT, LEFT_OUT_FOLDER,
)

# Why the database is not read into a backup or the diagnostics bundle. Only the database file
# itself is checked: a data folder moved to another drive behind a link is a normal setup, and
# documents in it are served the same way.
DATABASE_IS_LINK = "the database file is a link"
DATABASE_HAS_SECOND_NAME = (
    "the database file has a second name elsewhere on this computer (a hard link)")
DATABASE_NOT_A_FILE = "the database is not an ordinary file"
DATABASE_UNCHECKED = "the database file could not be checked"
DATABASE_REFUSALS: tuple[str, ...] = (
    DATABASE_IS_LINK, DATABASE_HAS_SECOND_NAME, DATABASE_NOT_A_FILE, DATABASE_UNCHECKED,
)


def database_refusal(db: Path) -> str | None:
    """None when the database file at `db` may be read into something the person shares (or is
    not there yet), else the reason it may not: it is a link, it has a second name (a hard link
    whose other name may be outside), or it is not a regular file. Kestrel makes the database
    itself with one name, so each of these was made by someone else."""
    try:
        info = os.lstat(db)
    except FileNotFoundError:
        return None
    except OSError:
        return DATABASE_UNCHECKED
    if stat.S_ISLNK(info.st_mode):
        return DATABASE_IS_LINK
    if not stat.S_ISREG(info.st_mode):
        return DATABASE_NOT_A_FILE
    if info.st_nlink != 1:
        return DATABASE_HAS_SECOND_NAME
    return None
_MAX_DEPTH = 8  # folders below a root that a recursive walk enters


def shareable_files(
    root: Path,
    include: Callable[[str], bool],
    *,
    recursive: bool = False,
    limit: int = MAX_SHARED_FILE_BYTES,
    unreadable: Callable[[str, str], None] | None = None,
) -> Iterator[tuple[str, bytes]]:
    """(path relative to `root`, bytes) for every file under `root` that may go into something
    the person shares: a backup, an export, a diagnostics bundle. Every walk that collects files
    for one uses this, and nothing else (D-SEC-55).

    Only regular files with a single name (no hard link) whose relative path `include` accepts,
    each found without following a link at any level: `root` must be a real folder in its own
    place (own_folder), a link or junction inside it is skipped, never entered or read, and each
    file is read through the folder it was listed from, opened without following a link where
    the OS can (POSIX), so nothing swapped in between listing and reading is followed. On Windows
    the same checks run on paths (the residual race of D-SEC-51). Sorted by relative path.

    `unreadable(rel, why)` hears of everything left out that the person may have expected: a
    file `include` accepts that is a link, has a second name, is not a regular file or could not
    be read; in a recursive walk any link at all (it may stand for a folder); and the root itself
    ("") when it is there but is a link or out of place. `why` is one of the LEFT_OUT_*
    sentences. A folder that is simply missing is not reported."""
    report = unreadable or (lambda rel, why: None)
    if own_folder(root) is None:
        if os.path.lexists(root):  # there, but a link or out of place: said, never followed
            report("", LEFT_OUT_FOLDER)
        return
    if _DIR_FD:
        try:
            fd = os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        except OSError:
            report("", LEFT_OUT_FOLDER)
            return
        try:
            yield from _shareable_fd(fd, "", include, recursive, limit, 0, report)
        finally:
            os.close(fd)
    else:
        yield from _shareable_paths(root, "", include, recursive, limit, 0, report)


def _shareable_fd(fd: int, prefix: str, include: Callable[[str], bool], recursive: bool,
                  limit: int, depth: int, report: Callable[[str, str], None],
                  ) -> Iterator[tuple[str, bytes]]:
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return
    for name in names:
        rel = f"{prefix}{name}"
        if not _text_name(name):
            report(_shown_name(rel), LEFT_OUT_NAME_NOT_TEXT)
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as exc:
            if include(rel):
                report(rel, _open_failure(exc))
            continue
        if stat.S_ISDIR(info.st_mode):
            if not recursive or depth >= _MAX_DEPTH:
                continue
            try:
                sub = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                report(f"{rel}/", _open_failure(exc))
                continue
            try:
                yield from _shareable_fd(sub, f"{rel}/", include, recursive, limit, depth + 1,
                                         report)
            finally:
                os.close(sub)
            continue
        why = _left_out(info, rel, include, recursive)
        if why is None:
            continue
        if why:
            report(rel, why)
            continue
        data, why = _read_regular(Path(name), limit, fd)
        if data is None:
            report(rel, why)
        else:
            yield rel, data


def _shareable_paths(folder: Path, prefix: str, include: Callable[[str], bool], recursive: bool,
                     limit: int, depth: int, report: Callable[[str, str], None],
                     ) -> Iterator[tuple[str, bytes]]:
    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        rel = f"{prefix}{entry.name}"
        if not _text_name(entry.name):
            report(_shown_name(rel), LEFT_OUT_NAME_NOT_TEXT)
            continue
        try:
            # A junction is a folder link that is_symlink() does not see (Python 3.12+ names it).
            if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                if recursive or include(rel):
                    report(rel, LEFT_OUT_LINK)
                continue
            # os.lstat, not entry.stat: on Windows a scandir entry reports st_nlink as 0.
            info = os.lstat(entry.path)
        except OSError as exc:
            if include(rel):
                report(rel, _open_failure(exc))
            continue
        if stat.S_ISDIR(info.st_mode):
            if recursive and depth < _MAX_DEPTH:
                if own_folder(Path(entry.path)) is None:
                    report(f"{rel}/", LEFT_OUT_FOLDER)
                    continue
                yield from _shareable_paths(Path(entry.path), f"{rel}/", include, recursive,
                                            limit, depth + 1, report)
            continue
        why = _left_out(info, rel, include, recursive)
        if why is None:
            continue
        if why:
            report(rel, why)
            continue
        data, why = _read_regular(Path(entry.path), limit, None)
        if data is None:
            report(rel, why)
        else:
            yield rel, data


def _left_out(info: os.stat_result, rel: str, include: Callable[[str], bool],
              recursive: bool) -> str | None:
    """For a non-folder entry found by lstat: None to pass over it in silence (a name the
    caller does not want), "" to read it, else the LEFT_OUT_* reason it is left out."""
    if stat.S_ISLNK(info.st_mode):
        # A link is said when its name is wanted, or in a recursive walk, where it may be a
        # folder the person expected to be copied. Never followed to find out.
        return LEFT_OUT_LINK if recursive or include(rel) else None
    if not include(rel):
        return None
    if not stat.S_ISREG(info.st_mode):
        return LEFT_OUT_NOT_A_FILE
    if info.st_nlink != 1:
        return LEFT_OUT_SECOND_NAME
    return ""


def read_servable(path: Path, root: Path, limit: int = MAX_SHARED_FILE_BYTES) -> bytes | None:
    """The bytes of a stored file to hand the person as a download, or None (D-SEC-55).
    read_servable_why says why not."""
    return read_servable_why(path, root, limit)[0]


def read_servable_why(path: Path, root: Path, limit: int = MAX_SHARED_FILE_BYTES,
                      ) -> tuple[bytes | None, str]:
    """(bytes, "") of a stored file to hand the person as a download, or (None, why), where
    `why` is one of the LEFT_OUT_* sentences, so a refused link is never shown as a missing file.

    `root` (the documents or prep packs folder) must be a real folder in its own place, `path`
    must lie inside it, and the file must be a regular file with a single name. Where the OS can
    (POSIX) every folder from `root` down is opened without following a link and the file is
    read relative to the last one, so nothing swapped in between a check and the read is
    followed; on Windows the same checks run on paths (D-SEC-51). The route serves exactly these
    bytes, never the path."""
    base = own_folder(root)
    if base is None:
        return None, (LEFT_OUT_FOLDER if os.path.lexists(root) else LEFT_OUT_MISSING)
    try:
        rel = (Path(_realpath(path.parent)) / path.name).relative_to(base)
    except (OSError, ValueError):
        return None, LEFT_OUT_OUTSIDE
    parts = rel.parts
    if not parts or not all(_plain_name(p) for p in parts):
        return None, LEFT_OUT_OUTSIDE
    if _DIR_FD:
        try:
            fd = os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        except OSError:
            return None, LEFT_OUT_FOLDER
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    return None, (LEFT_OUT_MISSING if _open_failure(exc) == LEFT_OUT_MISSING
                                  else LEFT_OUT_FOLDER)
                os.close(fd)
                fd = nxt
            return _read_regular(Path(parts[-1]), limit, fd)
        finally:
            os.close(fd)
    folder = root
    for part in parts[:-1]:
        folder = folder / part
        if own_folder(folder) is None:
            return None, (LEFT_OUT_FOLDER if os.path.lexists(folder) else LEFT_OUT_MISSING)
    target = folder / parts[-1]
    if target.is_symlink():
        return None, LEFT_OUT_LINK
    return _read_regular(target, limit, None)


def _text_name(name: str) -> bool:
    """A name that is text (a file name that is not valid UTF-8 cannot go into a zip or a page)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _shown_name(name: str) -> str:
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def secrets_dir() -> Path:
    """Per-install secrets that must never leave this computer (D-SEC-13). Never backed up and
    never restored from a backup."""
    return data_dir() / "secrets"


def model_signing_key_path() -> Path:
    """The 32-byte key that signs the saved reranker files (kestrel/learning/signing.py)."""
    return secrets_dir() / "model_signing.key"


def evals_dir() -> Path:
    """Where the bench-harness gold sets live (SPEC.md 8.6). The shipped placeholder files
    (`evals/goldset.<track>.jsonl`) are checked into the repo root, one level above the `kestrel`
    package -- a dev-time source file, not packaged resource data. A frozen install has no writable
    repo tree beside the executable, so regeneration there lands under the writable data dir
    instead; the current working directory (where `kestrel goldset regen` is actually run from in
    dev) resolves the repo root without any `__file__` arithmetic."""
    d = data_dir() / "evals" if is_frozen() else Path.cwd() / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def documents_dir() -> Path:
    """Where rendered resumes, capability statements and letters are written."""
    return data_dir() / "documents"


def prep_packs_dir() -> Path:
    """Where rendered prep packs are written."""
    return data_dir() / "prep_packs"


def confined_file(stored: object, root: Path) -> Path | None:
    """A file path read back from the database, accepted only when it resolves inside `root`.

    A restored or edited backup can point a stored path at any file on the computer, so a path
    from the database is never opened, served or deleted as it stands. Returns the resolved path,
    or None for an empty value, a path that cannot be resolved, or one outside `root` (it is
    then treated as missing, and logged). It does not check that the file exists."""
    if not stored or not isinstance(stored, (str, os.PathLike)):
        return None
    try:
        target = Path(stored).resolve()
        # Compared as normcased strings, because a Windows or macOS volume ignores case: the
        # same folder written two ways must neither be refused nor let a different one through.
        inside = os.path.normcase(str(target))
        base = os.path.normcase(str(root.resolve()))
        if inside == base or inside.startswith(base.rstrip(os.sep) + os.sep):
            return target
    except (OSError, ValueError, RuntimeError):
        pass
    import structlog

    structlog.get_logger().warning("stored_file_path_outside_data_folder")
    return None


def stored_file_why(stored: object) -> str:
    """Why a stored path that confined_file refused is not served, as a LEFT_OUT_* sentence:
    the stored name is itself a link, or names something outside the folder, or nothing is
    there. Looks at the name as stored and never follows it."""
    if not stored or not isinstance(stored, (str, os.PathLike)):
        return LEFT_OUT_MISSING
    try:
        if os.path.islink(stored):
            return LEFT_OUT_LINK
        if os.path.lexists(stored):
            return LEFT_OUT_OUTSIDE
    except (OSError, ValueError):
        return LEFT_OUT_UNREADABLE
    return LEFT_OUT_MISSING


def stored_pdf(stored: object) -> Path | None:
    """`confined_file` for a document PDF (the documents folder)."""
    return confined_file(stored, documents_dir())


def stored_prep_pdf(stored: object) -> Path | None:
    """`confined_file` for a prep pack PDF (the prep packs folder)."""
    return confined_file(stored, prep_packs_dir())


def exports_dir() -> Path:
    """Kestrel's own folder for files the operator asked it to save, used when the Downloads
    folder cannot be found or written (D-UI-41)."""
    return data_home() / "exports"


def _windows_downloads() -> Path | None:
    """The Downloads Known Folder (FOLDERID_Downloads), which follows a Downloads folder the
    operator moved to another drive; None when the shell call is unavailable or fails."""
    import ctypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    # {374DE290-123F-4565-9164-39C4925E467B}
    folder_id = _GUID(
        0x374DE290, 0x123F, 0x4565,
        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
    )
    out = ctypes.c_wchar_p()
    try:
        windll = ctypes.windll  # type: ignore[attr-defined]
        hr = windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(out)
        )
        try:
            return Path(out.value) if hr == 0 and out.value else None
        finally:
            windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError, ValueError):
        return None


def downloads_dir() -> Path:
    """Where the desktop app saves a file the operator asked for, such as the diagnostics zip
    (D-UI-41): the user's Downloads folder (on Windows the Known Folder, so a relocated
    Downloads is honored), else Kestrel's own exports folder under the data home."""
    if sys.platform == "win32":
        found = _windows_downloads()
        if found is not None and found.is_dir():
            return found
    home_downloads = Path.home() / "Downloads"
    if home_downloads.is_dir():
        return home_downloads
    return exports_dir()


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- packaged resources
def _resources() -> Traversable:
    return files("kestrel.resources")


def packaged_config() -> Traversable:
    return _resources() / "config"


def packaged_models() -> Traversable:
    return _resources() / "models"


def packaged_icons() -> Traversable:
    """The rasterized tray icons, generated from docs/images by scripts/make_icons.py."""
    return _resources() / "icons"


def packaged_app_icon() -> Traversable:
    """The indigo app icon (.ico, the installer's and Kestrel.exe's), for the Windows
    notification identity (shell.register_notification_icon)."""
    return _resources() / "icons" / "kestrel.ico"


def packaged_embed_model() -> Traversable:
    """The bundled in-process embedding model (kestrel.llm.inprocess_embed). Written at build time
    by scripts/fetch_embed_model.py and never committed; absent in a fresh source checkout."""
    return _resources() / "embed_model"


def packaged_migrations() -> Traversable:
    """The Alembic migrations directory (env.py + versions/), shipped as package data."""
    return files("kestrel.db.migrations")


def web_templates_dir() -> str:
    """The Jinja templates directory as a real path (Jinja2Templates needs a directory string).
    One-folder installs and dev checkouts are unzipped, so files() is already a real directory."""
    return str(files("kestrel.web") / "templates")


def web_static_dir() -> str:
    """The vendored static assets directory (HTMX, Alpine, prebuilt CSS) as a real path. No CDN at
    runtime; the packaged app serves these from disk (SPEC-INTERFACES 1.4)."""
    return str(files("kestrel.web") / "static")


def tiktoken_cache_dir() -> str:
    """The vendored cl100k_base cache dir, as a real path. Set as TIKTOKEN_CACHE_DIR before first
    use so tokenization is offline (the packaged app never downloads the encoding)."""
    return str(_resources() / "tiktoken_cache")


@contextmanager
def resource_path(traversable: Traversable) -> Iterator[Path]:
    """Yield a real filesystem path for a packaged resource (for tools like `ollama create -f`).
    In dev and one-folder installs this is the resource itself; as_file extracts if it is zipped."""
    with as_file(traversable) as p:
        yield p


# ------------------------------------------------------------------------------ config seeding
CONFIG_FILENAMES = ("settings.yml", "filters.yml", "sources.yml", "profile.example.yml")


def config_file(name: str) -> Path | None:
    """Resolve a config file: the operator's seeded copy under config_dir wins; otherwise fall
    back to the packaged default (so a fresh, unseeded install still loads)."""
    seeded = config_dir() / name
    if seeded.exists():
        return seeded
    packaged = packaged_config() / name
    return Path(str(packaged)) if packaged.is_file() else None


def seed_config() -> list[str]:
    """On first run, copy packaged config defaults into config_dir when it holds no *.yml yet.
    Returns the list of filenames written. profile.example.yml is seeded; the operator copies it
    to profile.yml (which is never shipped and never committed)."""
    ensure_dirs()
    existing = list(config_dir().glob("*.yml"))
    if existing:
        return []
    written: list[str] = []
    src_dir = packaged_config()
    for name in CONFIG_FILENAMES:
        src = src_dir / name
        if src.is_file():
            (config_dir() / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written.append(name)
    return written
