"""A Score again that ends without a score always says so (issue #144, hard rule 4).

When Tier 2 skips a role the operator asked to have scored (a posting with no description), the
role stays Not scored, never rejected, and one notification row records the end of the request.
The row and the role page read it to show "Not scored: the posting has no description. Paste it
to score.", and the next Pipeline page shows it once in its notice strip. Nothing here sends
anything anywhere."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Notification

KIND = "score_again_not_scored"
# In app only (D-UI-244 amended): the row is born with these delivered channels, so
# kestrel.services.delivery (which picks rows with none) never pushes it as a desktop toast, to
# ntfy or to the webhook, and no Settings switch is needed. The bell still lists it. "strip" is
# added once the Pipeline strip has shown it, independent of whether the bell marked it read.
IN_APP = "in_app"
STRIP = "strip"
NOT_SCORED_WHY = "Not scored: the posting has no description. Paste it to score."
TITLE = "Not scored: the posting has no description"
BODY = "You asked for a score. There is nothing to read yet. Paste the description to score it."


async def record_not_scored(session: AsyncSession, opportunity_id: int) -> bool:
    """Note that a requested score ended because the posting is empty, in the caller's session.
    At most one UNREAD note per role: a second skip while one is unread (in the bell) adds
    nothing, whether or not the Pipeline strip has shown it."""
    unread = (await session.execute(
        select(Notification.id).where(
            Notification.kind == KIND, Notification.opportunity_id == opportunity_id,
            Notification.read_at.is_(None),
        ).limit(1)
    )).first()
    if unread is not None:
        return False
    session.add(Notification(
        kind=KIND, severity="warning", title=TITLE, body=BODY, opportunity_id=opportunity_id,
        delivered_channels=[IN_APP],
    ))
    return True


async def asked_not_scored(session: AsyncSession, opp_ids: list[int]) -> set[int]:
    """The roles among `opp_ids` whose requested score ended for an empty posting."""
    if not opp_ids:
        return set()
    rows = await session.execute(
        select(Notification.opportunity_id).where(
            Notification.kind == KIND, Notification.opportunity_id.in_(opp_ids)
        ).distinct()
    )
    return {int(o) for (o,) in rows.all() if o is not None}


@dataclass
class Notice:
    text: str
    role_ids: list[int] = field(default_factory=list)


async def take_notice(session: AsyncSession) -> Notice | None:
    """The "ended without a score" notes the strip has not shown yet, as one sentence, then marks
    them shown so the strip appears once. Reading them in the bell does not hide the strip, and
    the strip does not mark them read. None when there are none. The caller commits."""
    # Filtered in SQL: only this kind's notes the strip has not shown yet.
    found = (await session.execute(
        select(Notification).where(
            Notification.kind == KIND,
            ~cast(Notification.delivered_channels, String).like(f'%"{STRIP}"%'),
        ).order_by(Notification.id)
    )).scalars().all()
    rows = [(n.id, n.opportunity_id) for n in found]
    if not rows:
        return None
    n = len(rows)
    for note in found:
        note.delivered_channels = [IN_APP, STRIP]
    text = (
        "1 role you asked to score was not scored: the posting has no description. Paste it to "
        "score." if n == 1 else
        f"{n} roles you asked to score were not scored: the postings have no description. "
        "Paste a description to score each one."
    )
    return Notice(text, [int(r[1]) for r in rows if r[1] is not None])
