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
