"""The "role received and being scored" notice for a capture from the browser extension (issue
#267).

A role the extension captures with the shared key, and that is new to Kestrel, gets one bell row
(kind role_captured) that names the role and opens it. The status rail poll, which every open page
makes, only reads: while a notice is waiting it renders a hidden element whose POST claims the
notice and answers with the toast, once, with a link to the role. So whatever page Kestrel has
open says the role arrived within one poll, and no GET ever changes anything (Codex review 4).
The role's title is stored as it is and redacted when shown, by the Confidential mode in force
then, in the toast here and in the bell list (pages/chrome.py) (Codex review 1). The toast
fades like any other (owner, item 66, 2026-11-25: "Fade after a few seconds"); the bell row
stays as the record.

Local only: the notice is a row in Kestrel's own database, read by Kestrel's own pages. Nothing
here sends anything anywhere (hard rule 1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import iso
from kestrel.db.models import Notification

KIND = "role_captured"

# The one wording of the notice (owner, item 66, 2026-11-25: "Approve", the text as written). The
# bell row and the toast both show it, filled with the role's title.
CAPTURE_NOTICE = "Role received and being scored: {title}"

# A notice is shown as a toast only while it is this fresh. One that waited longer (no Kestrel
# page was open) stays a bell row: "being scored" an hour later would no longer be news.
TOAST_WINDOW_S = 600

# What the bell channel records once the toast went out, so a second poll (or a second window)
# never shows it again.
TOAST_CHANNEL = "toast"


def _now() -> datetime:
    """This module's clock (tests patch it here, never the global one; D-TEST-2)."""
    return datetime.now(UTC)


async def record(session: AsyncSession, opp_id: int, title: str | None) -> None:
    """Add the notice for a role the extension just captured, from plain values the caller read
    before anything could expire them. The title is kept as it is: every place that shows the
    notice redacts it then (D-EXT-995). The caller commits."""
    session.add(Notification(
        kind=KIND, severity="info",
        title=CAPTURE_NOTICE.format(title=title or "Untitled role"),
        opportunity_id=opp_id, delivered_channels=[],
    ))


def _not_shown():
    return and_(
        Notification.kind == KIND,
        func.coalesce(func.json_array_length(Notification.delivered_channels), 0) == 0,
    )


def _fresh():
    return Notification.created_at >= iso(_now() - timedelta(seconds=TOAST_WINDOW_S))


async def pending(session: AsyncSession) -> int | None:
    """The id of the oldest fresh notice not shown yet, or None. Reads only: the rail poll, a GET
    every page makes, asks this and changes nothing."""
    return (await session.execute(
        select(Notification.id)
        .where(_not_shown(), _fresh())
        .order_by(Notification.created_at, Notification.id)
        .limit(1)
    )).scalar_one_or_none()


async def claim(session: AsyncSession) -> dict[str, Any] | None:
    """Claim the oldest fresh notice not shown yet and return it as the k-toast detail
    {message, href}, its role title redacted by the Confidential mode in force now; None when
    there is none. The claim is a conditional update, so two pages claiming at once show it
    once. Called only from a POST (pages/chrome.py). Commits."""
    from kestrel.web.redact import confidential, redact_text

    row = (await session.execute(
        select(Notification.id, Notification.title, Notification.opportunity_id)
        .where(_not_shown(), _fresh())
        .order_by(Notification.created_at, Notification.id)
        .limit(1)
    )).first()
    if row is None:
        return None
    claimed = (await session.execute(
        update(Notification)
        .where(Notification.id == row.id, _not_shown())
        .values(delivered_channels=[TOAST_CHANNEL])
        .returning(Notification.id)
    )).scalar_one_or_none()
    await session.commit()
    if claimed is None:
        return None
    conf, mapping = await confidential(session)
    toast: dict[str, Any] = {"message": redact_text(row.title, mapping, enabled=conf) or ""}
    if row.opportunity_id:
        toast["href"] = f"/opportunities/{row.opportunity_id}"
    return toast
