"""Failed starts in a row, and safe mode (#130, D-UI-252).

Kestrel always starts. When it still cannot (an optional feature that stops the server, a bad
install), a person must not be locked out of their own data. Every start attempt is counted in a
small file beside runtime.json (not the database: the database can be the problem), and the count
is cleared once the app is up. After `OFFER_AFTER` failed starts in a row the loading window offers
"Start with optional features off". Safe mode lasts for that process only: it is an environment
variable, so the next normal launch has every feature back.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

import structlog

from kestrel import paths

log = structlog.get_logger()

OFFER_AFTER = 2
SAFE_MODE_ENV = "KESTREL_SAFE_MODE"
OFFER_CODE = "safe_mode_offer"
OFFER_REASON = (
    "Kestrel could not start the last two times. You can start it with optional features off. "
    "Your roles, settings and files stay as they are."
)


def failed_starts() -> int:
    """Starts begun and not finished. A missing, unreadable or odd file reads as 0."""
    try:
        data = json.loads(paths.startup_health_json().read_text(encoding="utf-8"))
        count = int(data.get("failed_starts", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    return max(count, 0)


def _write(count: int) -> None:
    try:
        target = paths.startup_health_json()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps({"failed_starts": count}), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:  # a counter that cannot be saved must never stop a start
        log.warning("startup_health_write_failed", error=f"{type(exc).__name__}: {exc}")


def begin_start() -> int:
    """Count this attempt as failed until `mark_started` says otherwise (so a crash or a kill in
    the middle of a start counts too). Returns the count BEFORE this attempt."""
    before = failed_starts()
    _write(before + 1)
    return before


def mark_started() -> None:
    """The app is up: the streak ends."""
    if failed_starts():
        _write(0)


# Start failures safe mode cannot help: the migrations run in safe mode too, so a database a newer
# Kestrel saved, or one the migrations cannot open, fails the same way. They show their own
# message every time and never count toward the offer (0.7.6 review).
NOT_COUNTED = ("newer_database", "migrations_failed")


def not_counted(code: str) -> bool:
    return code in NOT_COUNTED


def start_failed(code: str) -> None:
    """A start ended with `code`. One safe mode cannot help is taken off the streak again
    (`begin_start` counted it), so it never leads to the offer."""
    if not_counted(code):
        abandon_start()


def abandon_start() -> None:
    """The person quit while the start was still running: that is not a failed start."""
    count = failed_starts()
    if count:
        _write(count - 1)


def safe_mode() -> bool:
    return os.environ.get(SAFE_MODE_ENV) == "1"


def enter_safe_mode() -> None:
    os.environ[SAFE_MODE_ENV] = "1"
    log.warning("safe_mode_on")


_offered = False  # this process has already shown the offer: Try again then really tries


def take_offer(before: int) -> bool:
    """True once per process when a start should stop and offer safe mode instead of trying the
    same thing a third time. Try again on the offer card starts normally."""
    global _offered
    if before >= OFFER_AFTER and not safe_mode() and not _offered:
        _offered = True
        return True
    return False


# ------------------------------------------------------------ the real cause of a failed start
# #148: a server that crashes while starting writes its traceback to logs/stderr.log, and the
# launcher used to report only "did not answer in time". These read that file back, for this
# attempt only, and give the card a few plain, redacted lines.
_CAUSE_READ_BYTES = 65536
_CAUSE_MAX_LINES = 3
_CAUSE_LINE_CHARS = 200
_STARTUP_FAILED = "Application startup failed"
_MASK = "a file on this computer"
_EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b")
# Where a path starts on a cause line: a quote opening one, a drive letter, a network share, or a
# POSIX path with at least one folder. Everything from there to the END OF THE LINE is masked
# (D-SEC-19): a folder or file name can hold any of `,` `;` `)` `]` `'` and spaces ("Smith, Jane",
# "Acme (Holdings) Ltd", "O'Brien Files"), so no end inside a line is safe. The full line stays in
# stderr.log for anyone reading the log on this computer.
# A quote or a bracket whose text holds a path separator before it closes starts a path too, so a
# relative path with a space in a folder name ('Jane Smith/kestrel.db', (Users/Jane Smith/x)) is
# masked from the quote or bracket on (fable review, 0.7.6).
_PATH_START = re.compile(
    r"""['"](?=[A-Za-z]:[\\/]|\\\\|~?/)"""
    r"""|['"(\[](?=[^'"()\[\]\n]*[\\/])"""
    r"""|(?<![A-Za-z])[A-Za-z]:[\\/]"""
    r"""|\\\\[^\\/\s]+[\\/]"""
    r"""|(?<![\w.])~?/[^/\s]*/"""
)


def stderr_log_size() -> int:
    """Where stderr.log ends now, so a later read sees only what this attempt wrote."""
    try:
        return (paths.logs_dir() / "stderr.log").stat().st_size
    except OSError:
        return 0


_ROOTED = re.compile(r"^(?:[/\\~]|\.{1,2}[/\\]|[A-Za-z]:)")
_RELATIVE = re.compile(r"[/\\].*[/\\]|[/\\][^/\\\s]*\.[A-Za-z0-9]{1,6}$")


def _mask_loose_path(out: str) -> str:
    """Catch a path the main pattern missed (a root folder with a space, a single part, a
    relative or drive-relative path). A word that starts like a path hides from there to the end;
    a relative-looking one hides everything after the exception's name. "read/write" stays."""
    pos = 0
    for word in out.split(" "):
        if _ROOTED.match(word) and len(word) > 1:
            return out[:pos] + _MASK
        if _RELATIVE.search(word):
            head, sep, _rest = out.partition(": ")
            if sep and " " not in head and not _RELATIVE.search(head):
                return f"{head}{sep}{_MASK}"
            return _MASK
        pos += len(word) + 1
    return out


def _plain(line: str, scrub) -> str:
    out = line.strip()
    found = _PATH_START.search(out)
    if found:
        out = out[: found.start()] + _MASK
    out = _mask_loose_path(out)
    out = scrub(out)
    if len(out) > _CAUSE_LINE_CHARS:
        out = out[: _CAUSE_LINE_CHARS - 3].rstrip() + "..."
    return out


def startup_cause(since: int = 0, redaction: Mapping[str, str] | None = None) -> list[str]:
    """Up to three plain lines saying why the server stopped while starting, read from the part of
    stderr.log written after byte `since`. Empty when the log shows no failed startup (so a slow
    start is never described as a crash), is missing, or cannot be read. Paths and anything shaped
    like a key are removed with the same masking the diagnostics bundle uses; REDACTION, the
    confidential-mode employer map when the caller has one, hides the current employer too."""
    try:
        with (paths.logs_dir() / "stderr.log").open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(since, end - _CAUSE_READ_BYTES, 0))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.rstrip() for ln in text.splitlines()]
    failed = [i for i, ln in enumerate(lines) if _STARTUP_FAILED in ln]
    if not failed:
        return []
    from kestrel.services.diagnostics import make_scrubber, saved_secret_values

    try:
        scrub = make_scrubber(redaction, saved_secret_values())
    except (OSError, ValueError):  # secrets unreadable: still mask by shape
        scrub = make_scrubber(redaction)
    exc = [ln for ln in lines[: failed[0] + 1] if _EXC_LINE.match(ln)]
    # The traceback is printed before uvicorn's "Application startup failed" line.
    out = ["The server stopped while it was starting."]
    if exc:
        out.append(_plain(exc[-1], scrub))
    return out[:_CAUSE_MAX_LINES]
