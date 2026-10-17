"""Keep the computer awake while Kestrel runs (operator request, D-RUNS-2).

A scheduled scan or a long Tier 2 drain is lost when Windows sleeps the machine halfway through
(the task rows survive and resume, but the run the operator expected overnight did not happen).
With the toggle on, Kestrel asks Windows to hold off system sleep for as long as the app is open.
The display is deliberately NOT held: `ES_SYSTEM_REQUIRED` without `ES_DISPLAY_REQUIRED`, so the
screen still turns off on its own schedule and only sleep is prevented.

`SetThreadExecutionState` is per thread and `ES_CONTINUOUS` lasts until the same thread changes
it or exits, so every call here must come from one long-lived thread. The uvicorn loop thread is
that thread: the lifespan applies the stored choice at startup and the Runs page route applies a
change, both as coroutines on that loop. Other platforms store the choice and report that it has
no effect there."""

from __future__ import annotations

import sys

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

log = structlog.get_logger()

KEY = "runs.keep_awake"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# ES_DISPLAY_REQUIRED (0x2) is left out on purpose: the screen may turn off.

NOT_WINDOWS = "has no effect on this platform; Windows only"


def _set_state(flags: int) -> int:
    """The one Win32 call, isolated for tests. Returns the previous state, 0 on failure."""
    import ctypes

    return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]


def apply(on: bool) -> str | None:
    """Hold off (or release) system sleep from the calling thread. Returns None when the request
    took, else a short reason it did not; never raises."""
    if not sys.platform.startswith("win"):
        return NOT_WINDOWS
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
    try:
        previous = _set_state(flags)
    except (OSError, AttributeError) as exc:
        log.warning("keep_awake_failed", on=on, error=f"{type(exc).__name__}: {exc}")
        return f"the request failed ({type(exc).__name__})"
    if previous == 0:
        log.warning("keep_awake_refused", on=on)
        return "Windows refused the request"
    log.info("keep_awake", on=on)
    return None


async def load(session: AsyncSession) -> bool:
    row = await session.get(Setting, KEY)
    return bool(row.value) if row is not None else False


async def save(session: AsyncSession, on: bool) -> None:
    row = await session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=bool(on)))
    else:
        row.value = bool(on)
        row.updated_at = utcnow_iso()


async def apply_stored(session: AsyncSession) -> str | None:
    """Startup: honor the stored choice. Off needs no call (a fresh thread holds nothing)."""
    if not await load(session):
        return None
    return apply(True)
