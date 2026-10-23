"""Notification DELIVERY and BATCHING (SPEC-INTERFACES 6). The notification TABLE and
NotificationService have existed since Phase 1; this is the layer that actually pushes them to the
operator over a desktop toast (default on: win11toast on Windows, `osascript` on macOS,
`notify-send` on Linux), an optional ntfy topic (phone push), and an optional generic webhook.

BATCHING IS MANDATORY and the whole point: never more than one delivered notification per minute.
A delivery cycle collects every undelivered, still-enabled notification; if more than one is
pending it collapses them into a SINGLE summary; and per-item scored notifications are off by
default. A tool that pings forty times in a run gets muted, and then none of the useful
notifications land either, so the batch window protects the channel's usefulness."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Notification
from kestrel.web.redact import confidential, redact_text

log = structlog.get_logger()

# The event kind raised per individual scored item. It is delivered ONLY when notify.per_item_scored
# is explicitly turned on; otherwise it is suppressed (never rendered), so it can never flood.
PER_ITEM_KIND = "item_scored"
# Marker written to delivered_channels so a suppressed row is handled, never re-picked or shown.
_SUPPRESSED = ["suppressed"]


@dataclass
class DeliveryResult:
    sent: int = 0  # messages actually pushed this cycle: 0 or 1 (the batch guarantee)
    collapsed: int = 0  # how many notifications the single message represents
    suppressed: int = 0  # disabled-by-toggle notifications marked handled without delivery
    title: str | None = None
    channels: list[str] = field(default_factory=list)


def _enabled_for(settings: Settings, note: Notification) -> bool:
    n = settings.notify
    if note.kind == PER_ITEM_KIND:
        return n.per_item_scored
    return n.events.get(note.kind, True)


# Tools reported missing once per process, at debug: a machine without notify-send must not fill
# the log with one line per delivery cycle.
_missing_tools_logged: set[str] = set()

# osascript receives the operator's text as ARGUMENTS to this fixed script, never spliced into it:
# a title containing a quote or a backslash must not be able to change what the script does.
_OSASCRIPT = (
    "on run argv\n"
    "display notification (item 2 of argv) with title (item 1 of argv)\n"
    "end run"
)


def _tool_missing(name: str) -> None:
    if name not in _missing_tools_logged:
        _missing_tools_logged.add(name)
        log.debug("notify_toast_tool_missing", tool=name)


def _toast(title: str, body: str | None) -> bool:
    """Show a desktop notification with the platform's own mechanism. Returns True when one was
    shown, False when this machine has no way to show one. Raises only for a tool that exists and
    then fails; _dispatch catches that. Runs off the loop (subprocess calls block)."""
    if sys.platform == "win32":
        import win11toast

        from kestrel import paths
        from kestrel.desktop.shell import APP_USER_MODEL_ID

        # The installed app toasts as itself (the identity its shortcut carries); from source
        # there is no such shortcut, so it keeps win11toast's default rather than toast silently.
        if paths.is_frozen():
            win11toast.notify(title, body or "", app_id=APP_USER_MODEL_ID)
        else:
            win11toast.notify(title, body or "")
        return True
    if sys.platform == "darwin":
        tool = shutil.which("osascript")
        if tool is None:
            _tool_missing("osascript")
            return False
        argv = [tool, "-e", _OSASCRIPT, title, body or ""]
    else:
        tool = shutil.which("notify-send")
        if tool is None:
            _tool_missing("notify-send")
            return False
        # "--" ends option parsing so a title beginning with "-" is text, not a flag.
        argv = [tool, "--app-name=Kestrel", "--", title, body or ""]
    from kestrel.llm.profiles import no_console_window

    subprocess.run(argv, check=True, capture_output=True, timeout=10, **no_console_window())
    return True


async def _dispatch(settings: Settings, title: str, body: str | None) -> list[str]:
    """Push one message to every enabled channel, best-effort. Returns the channels that succeeded.
    A channel failure (no win11toast, network down) never raises: a missed toast must not break the
    run or block the other channels.

    Every channel is awaited off the event loop. This runs on uvicorn's single loop, where the
    worker and every request session live, and a synchronous `httpx.post` here used to freeze the
    worker AND the UI for up to five seconds per push whenever the network was slow."""
    n = settings.notify
    channels: list[str] = []
    if n.toast:
        try:
            if await asyncio.to_thread(_toast, title, body):
                channels.append("toast")
        except Exception as exc:  # a missing/failed toast backend is non-fatal
            log.debug("notify_toast_failed", error=str(exc))
    if n.ntfy_topic or n.webhook_url:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as http:
            if n.ntfy_topic:
                try:
                    await http.post(
                        f"https://ntfy.sh/{n.ntfy_topic}",
                        content=(body or title).encode("utf-8"),
                        headers={"Title": title},
                    )
                    channels.append("ntfy")
                except Exception as exc:  # network down is not a run failure
                    log.debug("notify_ntfy_failed", error=str(exc))
            if n.webhook_url:
                try:
                    await http.post(n.webhook_url, json={"title": title, "body": body})
                    channels.append("webhook")
                except Exception as exc:
                    log.debug("notify_webhook_failed", error=str(exc))
    return channels


async def deliver_pending(session: AsyncSession, settings: Settings) -> DeliveryResult:
    """One delivery cycle. Delivers AT MOST ONE message (a single notification, or a summary of
    several), marks every handled notification delivered, and returns what happened. Called on a
    cadence (default every 60s), so at most one notification reaches the operator per minute."""
    result = DeliveryResult()
    if not settings.notify.enabled:
        return result

    pending = (
        (
            await session.execute(
                select(Notification)
                .where((Notification.delivered_channels == [])
                       | Notification.delivered_channels.is_(None))
                .order_by(Notification.created_at, Notification.id)
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return result

    deliverable = []
    for note in pending:
        if _enabled_for(settings, note):
            deliverable.append(note)
        else:
            note.delivered_channels = list(_SUPPRESSED)  # handled, but never shown
            result.suppressed += 1

    if not deliverable:
        await session.commit()
        return result

    if len(deliverable) == 1:
        note = deliverable[0]
        title, body = note.title, note.body
    else:
        # Collapse the burst into ONE summary so the minute's single notification is still useful.
        title = f"Kestrel: {len(deliverable)} notifications"
        lines = [f"- {n.title}" for n in deliverable[:8]]
        if len(deliverable) > 8:
            lines.append(f"- ...and {len(deliverable) - 8} more")
        body = "\n".join(lines)

    # Confidential mode redacts everywhere. ntfy and the webhook are the two places a
    # notification LEAVES the machine, and a title like "Northwind Freight scored 4.7" would
    # carry the current employer to a third-party push service. Same map Tier 3 uses.
    conf, mapping = await confidential(session)
    title = redact_text(title, mapping, enabled=conf) or ""
    body = redact_text(body or "", mapping, enabled=conf) or ""

    channels = await _dispatch(settings, title, body)
    marker = channels or ["delivered"]  # mark handled even if every channel was off/failed
    for note in deliverable:
        note.delivered_channels = list(marker)
    await session.commit()

    result.sent = 1
    result.collapsed = len(deliverable)
    result.title = title
    result.channels = channels
    return result
