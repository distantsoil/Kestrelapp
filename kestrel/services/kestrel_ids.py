"""The Kestrel ID: a short, opaque, stable ID for one application (D-IMPORT-9).

It is derived, not stored on the application: an HMAC of the pursuit's id and its role's
`first_seen_at` under a random salt kept in the `setting` table. The salt makes the ID carry
nothing about the person, the company or the role; `first_seen_at` keeps an application that was
removed and a later one that reuses its row number from sharing an ID. No schema change: nothing
about an application is added to its row.

The salt is created the first time an ID is handed out (an export, the import's copy of your
file). Until then no ID exists, so a review that reads one finds it unknown and writes nothing.
Nothing here leaves this computer."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity, Pursuit, Setting

SALT_KEY = "ids.salt"
_BODY = 10  # base32 characters: 50 bits, shown as K-XXXXX-XXXXX
_SHAPE = re.compile("^K[A-Z2-7]{" + str(_BODY) + "}$")


def make_id(salt: str, pursuit_id: int, first_seen_at: str | None) -> str:
    digest = hmac.new(
        salt.encode(), f"{pursuit_id}|{first_seen_at or ''}".encode(), hashlib.sha256
    ).digest()
    body = base64.b32encode(digest).decode().rstrip("=")[:_BODY]
    return f"K-{body[:5]}-{body[5:]}"


def normalize(raw: str | None) -> str:
    """The comparable form of a typed or pasted ID (case, dashes and spaces ignored), or "" when
    the text is not the shape of a Kestrel ID."""
    s = re.sub(r"[\s\-_]", "", (raw or "")).upper()
    return s if _SHAPE.match(s) else ""


async def get_salt(session: AsyncSession, *, create: bool = False) -> str | None:
    """The install's salt. `create` writes it (the caller commits); without it a missing salt
    reads as None and nothing is written."""
    row = await session.get(Setting, SALT_KEY)
    if row is not None and isinstance(row.value, str) and row.value:
        return row.value
    if not create:
        return None
    salt = secrets.token_hex(16)
    if row is None:
        session.add(Setting(key=SALT_KEY, value=salt))
    else:
        row.value = salt
    await session.flush()
    return salt


async def ids_by_pursuit(session: AsyncSession, *, create: bool = False) -> dict[int, str]:
    """pursuit id -> Kestrel ID for every application. Empty when no salt exists and `create` is
    off (no ID has been handed out)."""
    salt = await get_salt(session, create=create)
    if salt is None:
        return {}
    rows = (
        await session.execute(
            select(Pursuit.id, Opportunity.first_seen_at).join(
                Opportunity, Opportunity.id == Pursuit.opportunity_id
            )
        )
    ).all()
    return {pid: make_id(salt, pid, seen) for pid, seen in rows}
