"""Undo for the Applications board (D-UI-100): every change shows a toast with Undo.

A snapshot of the card is kept in a `setting` row (`undo.pursuit.<token>`) for a day, taken just
before the change. Undoing a stage change writes a new `status_event` that moves the card back
(history is kept, never deleted); undoing a Remove puts the pursuit, its history and its outcome
back. Nothing here sends anything anywhere."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    EngagementOffer,
    InterviewQuestionSet,
    Notification,
    Offer,
    Opportunity,
    Outcome,
    Pursuit,
    Setting,
    StatusEvent,
    VerdictOverride,
)

log = structlog.get_logger()

PREFIX = "undo.pursuit."
KEEP = timedelta(days=1)
# The card fields an Undo puts back.
FIELDS = (
    "status", "document_id", "cover_letter_id", "priority", "applied_at", "last_activity_at",
    "next_action_at", "next_action", "notes_md", "outcome", "outcome_stage", "outcome_reason",
    "closed_at", "allow_reposts",
)
OPP_FIELDS = ("archived_at", "archive_reason", "stage_reason")


def row_dict(obj: object) -> dict:
    """Every column of a row, for putting it back as it was."""
    return {a.key: getattr(obj, a.key) for a in inspect(obj).mapper.column_attrs}


async def _latest_outcome(session: AsyncSession, pursuit_id: int) -> Outcome | None:
    return (
        await session.execute(
            select(Outcome).where(Outcome.pursuit_id == pursuit_id).order_by(Outcome.id.desc())
        )
    ).scalars().first()


async def snapshot(session: AsyncSession, p: Pursuit) -> dict:
    """The card as it is now, to hand to `save` before a change is made."""
    prev = await _latest_outcome(session, p.id)
    return {
        "kind": "fields", "pursuit_id": p.id,
        "fields": {k: getattr(p, k) for k in FIELDS},
        "prev_outcome": row_dict(prev) if prev is not None else None,
    }


async def remove_snapshot(session: AsyncSession, p: Pursuit, opp: Opportunity) -> dict:
    """Everything remove_from_list deletes for a card, so Remove can be undone."""
    from kestrel.web.pages.opportunity_actions import ADD_REASONS

    events = (
        (await session.execute(select(StatusEvent).where(StatusEvent.pursuit_id == p.id)))
        .scalars()
        .all()
    )
    outcomes = (
        (await session.execute(select(Outcome).where(Outcome.pursuit_id == p.id))).scalars().all()
    )
    question_sets = (
        (
            await session.execute(
                select(InterviewQuestionSet).where(InterviewQuestionSet.pursuit_id == p.id)
            )
        )
        .scalars()
        .all()
    )
    labels = (
        (
            await session.execute(
                select(VerdictOverride).where(
                    VerdictOverride.opportunity_id == opp.id,
                    VerdictOverride.reason.in_(ADD_REASONS),
                )
            )
        )
        .scalars()
        .all()
    )
    notifications = (
        (await session.execute(select(Notification.id).where(Notification.pursuit_id == p.id)))
        .scalars()
        .all()
    )
    return {
        "kind": "remove",
        "pursuit": row_dict(p),
        "opportunity_id": opp.id,
        "opp": {k: getattr(opp, k) for k in OPP_FIELDS},
        "events": [row_dict(e) for e in events],
        "outcomes": [row_dict(o) for o in outcomes],
        "labels": [row_dict(v) for v in labels],
        "question_sets": [row_dict(q) for q in question_sets],
        "notifications": list(notifications),
    }


async def dependents(session: AsyncSession, pid: int) -> dict:
    result = {}
    for model in (StatusEvent, Outcome, Offer, EngagementOffer, InterviewQuestionSet, Notification):
        rows = (
            await session.execute(select(model).where(model.pursuit_id == pid).order_by(model.id))
        ).scalars()
        result[model.__tablename__] = [row_dict(r) for r in rows]
    return result


async def save_added(session: AsyncSession, p: Pursuit, label_ids: list[int]) -> str:
    await session.flush()
    return await save(
        session,
        {
            "kind": "added",
            "pursuit": row_dict(p),
            "dependents": await dependents(session, p.id),
            "label_ids": label_ids,
        },
    )


async def _write_lock(session: AsyncSession) -> None:
    """Take SQLite's write lock before the compare-then-delete, so another tab's save cannot
    commit between the check and the delete (the same pattern as stories.undo_dismissal). A
    session that has already written holds the lock, and SQLite refuses a nested BEGIN."""
    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import OperationalError

    try:
        await session.execute(sql_text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        if "within a transaction" not in str(exc):
            raise


async def undo_added(session: AsyncSession, token: str, oid: int) -> bool:
    from kestrel.web.pages.opportunity_actions import remove_from_list

    await _write_lock(session)
    row = await session.get(Setting, PREFIX + token)
    if row is None or row.updated_at < (datetime.now(UTC) - KEEP).isoformat():
        return False
    data = row.value
    if data.get("kind") != "added" or data["pursuit"]["opportunity_id"] != oid:
        return False
    p = await session.get(Pursuit, data["pursuit"]["id"])
    if (
        p is None
        or row_dict(p) != data["pursuit"]
        or await dependents(session, p.id) != data["dependents"]
    ):
        return False
    opp = await session.get(Opportunity, oid)
    # Add never writes archive fields, so its Undo must preserve independent hiding.
    await remove_from_list(
        session, opp, commit=False, label_ids=data["label_ids"], clear_import_archive=False
    )
    await session.delete(row)
    return True


async def _mark_changes(session: AsyncSession, data: dict) -> None:
    """Record which fields (and whether the outcome) the change touched, so Undo reverts only
    those and never a later edit to something else. Called by `save` after the change is made and
    before the commit."""
    p = await session.get(Pursuit, int(data.get("pursuit_id") or 0))
    if p is None:
        return
    before: dict = data.get("fields") or {}
    data["changed"] = [k for k in FIELDS if k in before and getattr(p, k) != before[k]]
    # The values the change wrote: Undo reverts a field only while it still holds that value,
    # so a later move of the same card is never rolled back by an older toast.
    data["after"] = {k: getattr(p, k) for k in data["changed"]}
    newest = await _latest_outcome(session, p.id)
    data["after_outcome"] = row_dict(newest) if newest is not None else None
    prev = data.get("prev_outcome")
    data["outcome_changed"] = (
        newest is not None if prev is None else newest is None or row_dict(newest) != prev
    )


async def save(session: AsyncSession, data: dict) -> str:
    """Keep a snapshot and return its token. Snapshots older than a day are dropped here."""
    if data.get("kind") == "fields":
        await _mark_changes(session, data)
    await expire(session)
    token = secrets.token_urlsafe(9)
    session.add(Setting(key=PREFIX + token, value=data, updated_at=utcnow_iso()))
    await session.flush()
    return token


async def expire(session: AsyncSession) -> None:
    """Retire bounded Undo snapshots even when no new actions are saved."""
    cutoff = (datetime.now(UTC) - KEEP).isoformat()
    await session.execute(
        delete(Setting).where(
            (Setting.key.like(PREFIX + "%") | Setting.key.like("undo.applied.%")),
            Setting.updated_at < cutoff,
        )
    )


async def restore(
    session: AsyncSession,
    token: str,
    *,
    offer_close: bool = False,
) -> tuple[str | None, str]:
    """Put a snapshot back. Returns (track, message); track is None when there was nothing to
    undo (already used, expired, or the card is gone). The snapshot is used up either way."""
    row = await session.get(Setting, PREFIX + token)
    if row is None or not isinstance(row.value, dict):
        return None, "This can no longer be undone."
    data = dict(row.value)
    if data.get("kind") not in ("fields", "remove") or (data.get("offer") and not offer_close):
        return None, "This can no longer be undone."
    await session.delete(row)
    if row.updated_at < (datetime.now(UTC) - KEEP).isoformat():
        return None, "This can no longer be undone."
    if data.get("kind") == "remove":
        return await _restore_removed(session, data)
    p = await session.get(Pursuit, int(data.get("pursuit_id") or 0))
    if p is None:
        return None, "This can no longer be undone."
    if not await can_restore(session, data):
        return None, "This can no longer be undone."
    fields: dict = data.get("fields") or {}
    changed = set(data["after"])
    if "status" in changed and fields.get("status") and fields["status"] != p.status:
        session.add(
            StatusEvent(
                pursuit_id=p.id,
                from_status=p.status,
                to_status=fields["status"],
                note="Undid the last change",
                occurred_at=utcnow_iso(),
            )
        )
    for key in changed:
        setattr(p, key, fields[key])
    if data.get("outcome_changed"):
        prev = data.get("prev_outcome")
        newest = await _latest_outcome(session, p.id)
        if newest is not None and (prev is None or newest.id != prev["id"]):
            await session.delete(newest)
        elif prev is not None and newest is not None:
            for key, value in prev.items():
                if key != "id":
                    setattr(newest, key, value)
    await session.flush()
    return p.track, "Undone."


async def can_restore(session: AsyncSession, data: dict) -> bool:
    """Validate every part before reverting any part of an action."""
    p = await session.get(Pursuit, int(data.get("pursuit_id") or 0))
    after = data.get("after")
    if p is None or not isinstance(after, dict):
        return False
    if any(getattr(p, k) != v for k, v in after.items()):
        return False
    if data.get("outcome_changed"):
        newest = await _latest_outcome(session, p.id)
        if (row_dict(newest) if newest else None) != data.get("after_outcome"):
            return False
    return True


async def _restore_removed(session: AsyncSession, data: dict) -> tuple[str | None, str]:
    opp = await session.get(Opportunity, int(data.get("opportunity_id") or 0))
    if opp is None:
        return None, "This can no longer be undone."
    if "opp_after" not in data or any(getattr(opp, k) != v for k, v in data["opp_after"].items()):
        return None, "This can no longer be undone."
    notifications = []
    for nid in data.get("notifications", []):
        n = await session.get(Notification, nid)
        if n is not None:
            if n.pursuit_id is not None:
                return None, "This can no longer be undone."
            notifications.append(n)
    saved = dict(data.get("pursuit") or {})
    exists = (
        await session.execute(select(Pursuit.id).where(Pursuit.opportunity_id == opp.id))
    ).first()
    if exists is not None or not saved:
        return None, "This can no longer be undone."
    taken = await session.get(Pursuit, saved.get("id"))
    if taken is not None:
        saved.pop("id", None)
    for label in data.get("labels", []):
        if await session.get(VerdictOverride, label["id"]) is not None:
            return None, "This can no longer be undone."
    p = Pursuit(**saved)
    session.add(p)
    await session.flush()
    for n in notifications:
        n.pursuit_id = p.id
    for key, value in (data.get("opp") or {}).items():
        setattr(opp, key, value)
    for e in data.get("events") or []:
        session.add(StatusEvent(**{**e, "id": None, "pursuit_id": p.id}))
    for o in data.get("outcomes") or []:
        session.add(Outcome(**{**o, "id": None, "pursuit_id": p.id}))
    for v in data.get("labels") or []:
        session.add(VerdictOverride(**v))
    for q in data.get("question_sets") or []:  # regenerable at model cost, so put back
        session.add(InterviewQuestionSet(**{**q, "id": None, "pursuit_id": p.id}))
    await session.flush()
    return p.track, "Put back on your board."
