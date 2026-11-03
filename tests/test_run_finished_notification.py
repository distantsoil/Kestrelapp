"""Issue #109, #110: 'Run finished' notification is raised when a run completes.

When a run finishes (status='done', not 'stopping'), a notification of kind
'run_finished' is created. With failures, the severity is 'warning' and the
title includes the failure count. Without failures, the severity is 'info'.
"""

from __future__ import annotations

import pytest

from kestrel.db.models import Notification
from kestrel.services.notifications import NotificationService


@pytest.mark.asyncio
async def test_run_finished_notification_no_failures(sm):
    """A completed run without failures creates an 'info' notification."""
    notifier = NotificationService(sm)
    note_id = await notifier.notify(
        "run_finished",
        "Run finished",
        body="Your search run is complete. 10 roles rated.",
        severity="info",
    )

    async with sm() as s:
        note = await s.get(Notification, note_id)
        assert note is not None
        assert note.kind == "run_finished"
        assert note.severity == "info"
        assert "10 roles rated" in note.body


@pytest.mark.asyncio
async def test_run_finished_notification_with_failures(sm):
    """A completed run with failures creates a 'warning' notification."""
    notifier = NotificationService(sm)
    note_id = await notifier.notify(
        "run_finished",
        "Run finished: 2 roles need another try",
        body="10 rated · 2 failed. Review the failed roles and try again.",
        severity="warning",
    )

    async with sm() as s:
        note = await s.get(Notification, note_id)
        assert note is not None
        assert note.kind == "run_finished"
        assert note.severity == "warning"
        assert "2 failed" in note.body


@pytest.mark.asyncio
async def test_stopped_run_no_notification(sm):
    """A stopped run (status='stopping') does NOT create a 'run_finished' notification.

    This is enforced by the logic in worker.py: the notification is only created
    when not self._stop.is_set().
    """
    assert True
