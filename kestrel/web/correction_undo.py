"""Held Undos for requirement corrections (issue 170, D-UI-301).

A write to a correction (a save on the role page, a change or a Remove in Profile) hands the
browser a random one-use token, never the rows. The server keeps the `Undo` and the state each
written row was left in (its `updated_at`); redeeming the token puts the held rows back only when
that state still holds. So a token cannot carry forged content, cannot be replayed, and cannot
delete or overwrite a later answer made from another role, another tab or Profile.

Invariant: an Undo changes nothing unless every row it would touch is exactly as the write left
it (written rows keep their stamp, removed rows are still absent)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import iso
from kestrel.db.models import RequirementCorrection, Setting
from kestrel.learning import corrections as corr

PREFIX = "undo.correction."
KEEP = timedelta(days=1)


def _now() -> datetime:
    """This module's own clock; tests patch it (D-TEST-2)."""
    return datetime.now(UTC)


def _cutoff() -> str:
    return iso(_now() - KEEP)


async def hold(
    s: AsyncSession, undo: corr.Undo, *, opp: int | None = None, profile: bool = False
) -> str:
    """Keep `undo` for a day under a random token and sweep the old ones. Records the stamp of
    every row the write left in place. `opp` ties the token to a role page, `profile` to
    Profile; the other surface refuses it."""
    token = secrets.token_urlsafe(16)
    stamps: dict[str, str | None] = {}
    for rid in undo.written:
        row = await s.get(RequirementCorrection, rid)
        stamps[str(rid)] = row.updated_at if row is not None else None
    await s.execute(
        delete(Setting).where(Setting.key.like(PREFIX + "%"), Setting.updated_at < _cutoff())
    )
    s.add(
        Setting(
            key=PREFIX + token,
            value={"opp": opp, "profile": profile, "undo": undo.to_json(), "stamps": stamps},
        )
    )
    await s.commit()
    return token


class Held:
    """A held Undo, read back."""

    def __init__(self, row: Setting) -> None:
        data = row.value if isinstance(row.value, dict) else {}
        self.row = row
        self.opp: int | None = data.get("opp")
        self.profile: bool = bool(data.get("profile"))
        self.undo = corr.Undo.from_json(data.get("undo") or {})
        self.stamps: dict[str, Any] = dict(data.get("stamps") or {})

    @property
    def ids(self) -> set[int]:
        return {*self.undo.written, *(b["id"] for b in self.undo.before)}


async def find(s: AsyncSession, token: str) -> Held | None:
    """The held Undo for TOKEN, or None: an unknown token, or one older than a day (the snapshot,
    a removed correction's note included, is kept for a day and no longer)."""
    if not token:
        return None
    row = await s.get(Setting, PREFIX + token)
    if row is None or not isinstance(row.value, dict):
        return None
    if (row.updated_at or "") < _cutoff():
        return None
    return Held(row)


async def still_as_left(s: AsyncSession, held: Held) -> bool:
    """True when every row the Undo touches is as the write left it: each written row exists with
    the stamp recorded, and each removed row's id is still free."""
    for rid in held.undo.written:
        row = await s.get(RequirementCorrection, rid)
        if row is None or row.updated_at != held.stamps.get(str(rid)):
            return False
    for b in held.undo.before:
        if b["id"] not in held.undo.written and await s.get(RequirementCorrection, b["id"]):
            return False
    return True
