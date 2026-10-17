"""NotificationService — writes notification rows. Phases 7 and 12 raise notifications long before
Phase 16 builds the delivery channels (toast/ntfy/webhook), so recording exists from here;
delivery is a later concern (SPEC-INTERFACES.md 6, Phase 1 prompt)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.enums import NOTIFY_SEVERITY
from kestrel.db.models import Notification


class NotificationService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def notify(
        self,
        kind: str,
        title: str,
        *,
        body: str | None = None,
        severity: str = "info",
        opportunity_id: int | None = None,
        pursuit_id: int | None = None,
    ) -> int:
        if severity not in NOTIFY_SEVERITY:
            severity = "info"
        async with self._sm() as s:
            note = Notification(
                kind=kind,
                title=title,
                body=body,
                severity=severity,
                opportunity_id=opportunity_id,
                pursuit_id=pursuit_id,
                delivered_channels=[],
            )
            s.add(note)
            await s.commit()
            return note.id
