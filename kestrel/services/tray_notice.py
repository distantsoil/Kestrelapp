"""The "Kestrel is still running" notice shown when the window is closed to the tray (issue #200).

One place for its wording and for recording it. The banner is an operating-system notification
that may never appear (macOS can show nothing for a notification that is not Kestrel's own), so
the same notice is also kept as a row in the notifications list, which is where the tray's
Recent notifications entry points. The row is marked handled (never re-delivered as a second
banner)."""

from __future__ import annotations

import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

KIND = "window_closed"
TITLE = "Kestrel is still running"
# Short enough for a macOS banner, which cuts a long body off. The way to quit on close instead
# lives in Settings > This computer, next to the switch, not in the banner.
BODY = "It keeps working while the window is closed. Open it from the tray icon."
BODY_MAC = "It keeps working while the window is closed. Open it from the menu bar icon."
# Written to delivered_channels so the delivery cycle never re-picks the row as a new banner.
HANDLED = ["tray"]


def body(platform: str | None = None) -> str:
    return BODY_MAC if (platform or sys.platform) == "darwin" else BODY


async def record(sm: async_sessionmaker) -> int:
    """Keep the notice in the notifications list. Returns the new row's id."""
    from kestrel.db.models import Notification  # not at import: the shell imports this module

    async with sm() as s:
        note = Notification(
            kind=KIND, severity="info", title=TITLE, body=body(), delivered_channels=list(HANDLED)
        )
        s.add(note)
        await s.commit()
        return note.id
