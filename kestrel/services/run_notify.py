"""What a finished run tells the person (#110, #111): at most two notifications per run, never one
per role, and never twice for the same role.

1. The outcome. With roles that need another try, ONE warning under "Something needs attention"
   that links to Needs another try. Without any, or when every one of them was already announced
   by an earlier run, the plain "Run finished" summary instead.
2. The roles worth a look. ONE alert for the roles that were new in this run and rated Your call
   or better ("Not a fit" never counts): the role named when there is one, otherwise the count
   with the strong ones broken out.

A role that fails again on a later run is not announced again: the ids already announced are kept
in one setting row and forgotten once a role leaves Needs another try, so a role that comes back
to the list later is new to it again. A role rated again in a later run was not first seen in that
run, so it never reaches the second alert. The notifications are rows; the person's switches,
quiet hours and Confidential mode apply where they are delivered (services.delivery), and the
role's title and company are redacted here too, so the stored row is safe on every surface."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.db.models import Quarantine, Run, Setting, Task
from kestrel.services.notifications import NotificationService

log = structlog.get_logger()

# The notification kind of the warning, in the "Something needs attention" group.
RETRY_KIND = "roles_need_retry"
# The ids of the roles already announced as needing another try (a list of integers).
SEEN_KEY = "notify.retry_announced"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def retry_title(failed: int) -> str:
    word = _plural(failed, "role needs", "roles need")
    return f"Run finished: {failed} {word} another try"


def worth_title(n: int, strong: int, first: dict[str, Any] | None, *, redact) -> str:
    """"1 new role worth a look: Title at Company", or "N new roles worth a look (K strong)"."""
    if n == 1 and first:
        title = redact(first.get("title")) or "Untitled"
        org = redact(first.get("org"))
        return f"1 new role worth a look: {title} at {org}" if org else (
            f"1 new role worth a look: {title}"
        )
    base = f"{n} new roles worth a look"
    return f"{base} ({strong} strong)" if strong else base


async def _failed_role_ids(s: AsyncSession, start: str | None) -> set[int]:
    """The roles that need another try because of THIS run: an open failure recorded since the
    run began, or a scoring task that gave up since then. With no start that resolves, nothing
    (never every old failure)."""
    from kestrel.web.queries import quarantine_needs_you_clause

    if not start:
        return set()
    ids: set[int] = set()
    rows = await s.execute(
        select(Quarantine.opportunity_id).where(
            quarantine_needs_you_clause(), Quarantine.created_at >= start,
            Quarantine.opportunity_id.is_not(None),
        )
    )
    ids.update(int(oid) for (oid,) in rows.all())
    dead = await s.execute(
        select(Task.payload).where(
            Task.type.in_(("t1", "t2")), Task.status == "dead", Task.updated_at >= start
        )
    )
    for (payload,) in dead.all():
        oid = (payload or {}).get("opportunity_id") if isinstance(payload, dict) else None
        if isinstance(oid, int):
            ids.add(oid)
    return ids


async def _open_role_ids(s: AsyncSession) -> set[int]:
    """Every role on Needs another try right now (its open, unqueued failures)."""
    from kestrel.web.queries import quarantine_needs_you_clause

    rows = await s.execute(
        select(Quarantine.opportunity_id).where(
            quarantine_needs_you_clause(), Quarantine.opportunity_id.is_not(None)
        )
    )
    return {int(oid) for (oid,) in rows.all()}


async def _announce_new(s: AsyncSession, now: set[int], still_open: set[int]) -> bool:
    """Whether any role in `now` has not been announced yet, and record them. The record keeps
    only roles still on the list, so one that left and returns is new again. Commits."""
    row = await s.get(Setting, SEEN_KEY)
    seen = {int(x) for x in (row.value if row and isinstance(row.value, list) else [])
            if isinstance(x, int)}
    fresh = bool(now - seen)
    kept = sorted((seen & (still_open | now)) | now)
    if row is None:
        s.add(Setting(key=SEEN_KEY, value=kept))
    else:
        row.value = kept
    await s.commit()
    return fresh


async def notify_run_finished(
    sm: async_sessionmaker[AsyncSession], *, run_id: int | None, rated: int
) -> None:
    """Raise the run's notifications. The caller isolates failures (a finished run must never fail
    because a notification could not be written)."""
    from kestrel.analytics import run_summary as rsum
    from kestrel.web.redact import confidential_once, redact_text

    notifier = NotificationService(sm)
    async with sm() as s:
        summary = await rsum.run_summary(s, None, paused=False)
        run_row = await s.get(Run, run_id) if run_id is not None else None
        start = (run_row.started_at if run_row else None) or summary.get("started_at")
        now_ids = await _failed_role_ids(s, start)
        # Roles, not attempts: a role whose scoring task gave up after three tries is one role.
        failed = max(int(summary.get("failed", 0) or 0), len(now_ids))
        # Every run prunes the record, so a role that left Needs another try and fails again
        # many runs later is new to the list again. With failures that no role can be named for,
        # there is nothing to compare: they are announced.
        fresh = await _announce_new(s, now_ids, await _open_role_ids(s))
        new_failures = fresh or not now_ids
        conf, mapping = await confidential_once(s)

    def redact(text: str | None) -> str:
        return redact_text(text or "", mapping, enabled=conf) or ""

    rated_word = _plural(rated, "role", "roles")
    if failed and new_failures:
        word = _plural(failed, "role", "roles")
        await notifier.notify(
            RETRY_KIND, retry_title(failed),
            body=f"{rated} rated · {failed} {word} failed. Open Needs another try to retry them.",
            severity="warning",
        )
    else:
        body = f"Your search run is complete. {rated} {rated_word} rated."
        if failed:  # already announced by an earlier run: counted, not announced again
            body += f" {failed} still {_plural(failed, 'needs', 'need')} another try."
        await notifier.notify("run_finished", "Run finished", body=body, severity="info")

    worth = int(summary.get("worth_new", 0) or 0)
    if worth > 0:
        strong = int(summary.get("worth_new_strong", 0) or 0)
        first = summary.get("worth_new_first")
        role_id = int(first["id"]) if worth == 1 and isinstance(first, dict) else None
        body = f"Your search found {worth} new {_plural(worth, 'role', 'roles')} rated Your call "\
            "or better."
        if strong:
            body += f" {strong} rated Strong."
        await notifier.notify(
            "high_score", worth_title(worth, strong, first, redact=redact), body=body,
            severity="info", opportunity_id=role_id,
        )
