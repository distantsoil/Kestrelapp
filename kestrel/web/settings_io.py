"""Saving a setting from the Settings page, and taking it back (D-UI-106).

Every control saves on its own. The reply is JSON the page turns into a message with Undo for a
few seconds. The Undo token carries exactly what the save changed: the key, the value that was
there before (or "no row"), and the value the save wrote. Undo puts the old value back only if
the row still holds the value this save wrote; a later change by the operator is never
overwritten, and the message says "This can no longer be undone." instead (COMMON.md, Undo).

Nothing here sends anything anywhere (hard rule 1)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

log = structlog.get_logger()

ABSENT = object()  # the row did not exist
NO_UNDO = "This can no longer be undone."
# Said when a save or an Undo fails inside a file or the database (D-SEC-4): a fixed sentence,
# because the failure's own text can carry a path. The caller logs the failure.
SAVE_FAILED = "Could not save that. The details are in the log."

# The rows Undo may touch. Anything else is refused, so the route cannot become a way to write
# an arbitrary setting.
_EXACT = frozenset({
    "confidential_mode", "max_posting_age_days", "consulting.max_trigger_age_days",
    "thresholds.apply", "thresholds.consider", "ui.close_to_tray", "updates.enabled",
    "ui.scheme", "notify.toast", "keep_awake", "mcp.port", "mcp.enabled", "ui.autostart",
    "runs.keep_awake",
})
_PREFIX = (
    "notify.events.", "learning.", "tier0.rules_off.", "scoring.weights.", "ui.", "notify.",
    "region.",
)


# Settings only this computer may write (#155, D-SEC-9). A key holder reaches the generic writers
# (Undo, the ui key saver, the settings form) as well as the dedicated routes, so both consult THIS
# table: a key a dedicated route guards is guarded in every writer too. They change what leaves the
# computer or where it goes, or act on this machine (a tray, autostart, sleep, a listening port).
LOCAL_ONLY_EXACT = frozenset({
    "mcp.port", "mcp.enabled", "ui.autostart", "ui.close_to_tray", "keep_awake",
    # The row Keep awake really writes (kestrel.services.keep_awake.KEY); `keep_awake` above is
    # the older name, kept so a row written under it is guarded too.
    "runs.keep_awake",
    "updates.enabled", "profile", "server.bind_all", "server.shared_secret",
    # Where a notice, a search or an update check goes, and how Kestrel presents itself to a site
    # (D-SEC-10): a destination or an outbound behavior is this computer's choice, never a key
    # holder's and never a backup's.
    "notify.webhook_url", "notify.ntfy_topic", "research.searx_url", "updates.feed_url",
    "ingest.user_agent", "ingest.respect_robots",
})
LOCAL_ONLY_PREFIX = ("llm.", "mcp.", "server.", "hosted", "models.", "profile.")


_LOCAL_ROOTS = tuple(sorted({*LOCAL_ONLY_EXACT, *(p.rstrip(".") for p in LOCAL_ONLY_PREFIX)}))


def within_local(key: str) -> bool:
    """Whether KEY is itself a local-only setting or lies inside one (`llm.mode`,
    `notify.webhook_url`, `mcp.port.x`). Used where a settings tree is walked from the top, so a
    section that only CONTAINS a local-only key (`notify`) is descended into, not taken whole."""
    return (key in LOCAL_ONLY_EXACT or key.startswith(LOCAL_ONLY_PREFIX)
            or any(key.startswith(e + ".") for e in LOCAL_ONLY_EXACT))


def contains_local(key: str) -> bool:
    """Whether a row at KEY would set a local-only setting by nesting (#review 0.7.6): a section
    (`llm`, `notify`, `server`) or a parent (`llm.hosted_mode` is inside, `research` contains
    `research.searx_url`) whose value is a mapping replaces the keys under it. The empty key is
    the whole tree."""
    return not key or any(r == key or r.startswith(key + ".") for r in _LOCAL_ROOTS)


def needs_local(key: str, value: Any = None) -> bool:
    """Whether writing VALUE to KEY is something only this computer may do, whatever the key's
    shape: a local-only key, anything inside one, and any section or parent that contains one.
    Confidential mode is the one conditional key: turning it ON is stricter, so a key holder may;
    OFF is local only."""
    if key == "confidential_mode":
        return value is not True
    return within_local(key) or contains_local(key)


def refuse_remote(request: Any, key: str, value: Any = None) -> None:
    """Raise the guard's refusal when a key holder would write a local-only setting."""
    from kestrel.web.auth import LOCAL_ONLY_CHANGE, LocalOnlyRefused, request_is_local

    if needs_local(key, value) and not request_is_local(request):
        raise LocalOnlyRefused(LOCAL_ONLY_CHANGE)


def undoable(key: str) -> bool:
    return key in _EXACT or key.startswith(_PREFIX)


_NUMERIC = ("scoring.weights.", "thresholds.")
_NUMERIC_EXACT = frozenset({"max_posting_age_days", "consulting.max_trigger_age_days", "mcp.port"})


def plausible(key: str, value: Any) -> bool:
    """Whether VALUE is a value the setting could ever have held. An Undo link is just a URL, so
    what it carries is checked before it is written back: a number setting must stay a number
    (a weight in 0 to 1), because a bad one would break every later settings load."""
    if value is ABSENT:
        return True
    if key.startswith(_NUMERIC) or key in _NUMERIC_EXACT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if key.startswith("scoring.weights."):
            return 0 <= value <= 1
        return value >= 0
    return True


def dumps(value: Any) -> str:
    return "absent" if value is ABSENT else json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(text: str) -> Any:
    if text == "absent":
        return ABSENT
    return json.loads(text)


async def read(session: AsyncSession, key: str) -> Any:
    row = await session.get(Setting, key)
    return ABSENT if row is None else row.value


async def write(session: AsyncSession, key: str, value: Any) -> None:
    """Set or (for ABSENT) delete one row. The caller commits."""
    row = await session.get(Setting, key)
    if value is ABSENT:
        if row is not None:
            await session.delete(row)
        return
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


def undo_url(key: str, prev: Any, now: Any) -> str | None:
    """The URL the toast POSTs to take this one change back."""
    if not undoable(key):
        return None
    return "/settings/undo?" + urlencode({"key": key, "prev": dumps(prev), "now": dumps(now)})


def reply(
    message: str, *, ok: bool = True, undo: str | None = None, status: int = 200, **extra: Any
) -> JSONResponse:
    """The JSON every control save answers with. A refusal is 200 with ok false and a plain
    sentence, because a 4xx body is dropped by htmx and unread by a plain fetch."""
    body: dict[str, Any] = {"ok": ok, "message": message, **extra}
    if undo:
        body["undoUrl"] = undo
    return JSONResponse(body, status_code=status)


def refuse(message: str, **extra: Any) -> JSONResponse:
    return reply(message, ok=False, **extra)
