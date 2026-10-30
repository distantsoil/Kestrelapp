"""Notification DELIVERY and BATCHING (SPEC-INTERFACES 6). The notification TABLE and
NotificationService have existed since Phase 1; this is the layer that actually pushes them to the
operator over a desktop toast (default on: win11toast on Windows, `osascript` on macOS,
`notify-send` on Linux), an optional ntfy topic (phone push), and an optional generic webhook.

BATCHING IS MANDATORY and the whole point: never more than one delivered notification per minute.
A delivery cycle collects every undelivered, still-enabled notification; if more than one is
pending it collapses them into a SINGLE summary; and per-item scored notifications are off by
default. A tool that pings forty times in a run gets muted, and then none of the useful
notifications land either, so the batch window protects the channel's usefulness.

THE SOUND (D-UI-95, D-REL-7): every pop-up is silent, and Kestrel plays its own sound (kestrel.
services.alert_sound) once with the message a cycle delivers, so it too is at most once a minute."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import ui_settings
from kestrel.config import Settings
from kestrel.db.models import Notification, Setting
from kestrel.privacy import EmployerMap
from kestrel.services import alert_sound
from kestrel.web.redact import confidential, redact_text, redaction_map

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
    held: int = 0  # notifications left pending because Quiet hours are on right now
    sounded: bool = False  # Kestrel's own alert sound was started with this message
    title: str | None = None
    channels: list[str] = field(default_factory=list)


def kind_enabled(settings: Settings, kind: str) -> bool:
    """Whether the operator has this kind of alert turned on (a kind not listed is on)."""
    n = settings.notify
    if kind == PER_ITEM_KIND:
        return n.per_item_scored
    return n.events.get(kind, True)


def _enabled_for(settings: Settings, note: Notification) -> bool:
    return kind_enabled(settings, note.kind)


# One info line when Quiet hours first hold a cycle, then debug for the cycles that follow, until a
# cycle runs outside the window: an overnight window would otherwise log a line a minute.
_hold_logged = False


# Tools reported missing once per process, at debug: a machine without notify-send must not fill
# the log with one line per delivery cycle.
_missing_tools_logged: set[str] = set()

# notify-send's flag for the freedesktop "suppress-sound" hint: the pop-up makes no sound itself.
NOTIFY_SEND_SILENT = ("--hint=boolean:suppress-sound:true",)

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
        # Every pop-up is silent: the alert sound is Kestrel's own, played by deliver_pending
        # (kestrel.services.alert_sound) when the person turned it on (owner, D-REL-7: "I dont
        # want to use the system sound"), so the system's chime never plays on top of it, or in
        # spite of it being off.
        silent = {"silent": "true"}
        if paths.is_frozen():
            win11toast.notify(title, body or "", app_id=APP_USER_MODEL_ID, audio=silent)
        else:
            win11toast.notify(title, body or "", audio=silent)
        return True
    if sys.platform == "darwin":
        tool = shutil.which("osascript")
        if tool is None:
            _tool_missing("osascript")
            return False
        # `display notification` without a `sound name` clause is silent, which is the point.
        argv = [tool, "-e", _OSASCRIPT, title, body or ""]
    else:
        tool = shutil.which("notify-send")
        if tool is None:
            _tool_missing("notify-send")
            return False
        # The freedesktop hint keeps the pop-up silent (a desktop would otherwise play its own
        # sound). "--" ends option parsing so a title beginning with "-" is text, not a flag.
        argv = [tool, "--app-name=Kestrel", *NOTIFY_SEND_SILENT, "--", title, body or ""]
    from kestrel.llm.profiles import no_console_window, system_tool_env

    subprocess.run(
        argv, check=True, capture_output=True, timeout=10,
        **no_console_window(), **system_tool_env(),
    )
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


async def _confidentiality(session: AsyncSession, settings: Settings) -> tuple[bool, EmployerMap]:
    """`confidential(session)`, which reads every settings row: when one of them no longer
    validates (the delivery loop then runs on the settings file, kestrel.web.app) it raises, and
    the one row that matters here, Confidential mode, is read by itself. A bad row elsewhere must
    neither stop the alerts nor let a push leave the machine unredacted."""
    try:
        return await confidential(session)
    except (ValueError, TypeError):
        row = await session.get(Setting, "confidential_mode")
        on = bool(row.value) if row is not None else bool(settings.confidential_mode)
        return (True, await redaction_map(session)) if on else (False, EmployerMap())


async def deliver_pending(
    session: AsyncSession,
    settings: Settings,
    *,
    clock: Callable[[], time] | None = None,
) -> DeliveryResult:
    """One delivery cycle. Delivers AT MOST ONE message (a single notification, or a summary of
    several), marks every handled notification delivered, and returns what happened. Called on a
    cadence (default every 60s), so at most one notification reaches the operator per minute.

    Quiet hours (SPEC-INTERFACES 6): inside the window the cycle delivers nothing. To the person,
    every channel here is a pop-up (the desktop toast, the ntfy push to their phone and the
    webhook alike), so all of them wait together. The pending rows are left exactly as they are,
    not marked delivered, which means the first cycle after the window ends finds them and sends
    the usual single summary. The bell does not depend on this function: it lists every row.
    `clock` returns the local wall-clock time (default: the machine's); tests pass their own."""
    global _hold_logged
    result = DeliveryResult()
    if not settings.notify.enabled:
        return result
    quiet = ui_settings.quiet_now(settings.notify.quiet_hours, clock)
    if not quiet:
        _hold_logged = False

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

    if quiet:
        # A kind the operator turned off is not counted as held: it would never be shown anyway.
        result.held = sum(1 for note in pending if _enabled_for(settings, note))
        if result.held:
            report = log.debug if _hold_logged else log.info
            report("notify_held_quiet_hours", held=result.held)
            _hold_logged = True
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
    conf, mapping = await _confidentiality(session, settings)
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
    if settings.notify.sound:
        # Kestrel's own sound, once, with the one message this cycle delivered: never more than
        # once a minute, never inside Quiet hours or for a kind that is turned off (those returned
        # above), and independent of the window and of the Computer notifications switch.
        try:
            result.sounded = await alert_sound.play_async(
                settings.notify.sound_name, settings.notify.sound_volume
            )
        except Exception as exc:  # a sound that cannot play never undoes a delivery
            log.debug("notify_sound_failed", error=str(exc))
    return result
