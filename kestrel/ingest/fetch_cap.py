"""A per-provider cap on fetches per local day, kept in the database (D-SRC-15).

Remotive's terms advise at most 4 fetches a day for the provider, however many source rows use it,
so Kestrel enforces that in code. The count is stored in the `setting` table under one key per
adapter kind, not in memory, so quitting and restarting the app cannot reset it (hard rule 5).
A fetch is counted when it is reserved, before the network call: a crash mid-fetch still counts,
which errs on the side of fewer requests. The reservation is ONE conditional upsert statement,
which SQLite runs atomically, so a scheduled run and "Run now" overlapping can never both take
the last slot.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Setting, utcnow_iso

KEY_PREFIX = "fetch_cap:"


def _now() -> datetime:
    """The module's own clock; tests patch this alias."""
    return datetime.now().astimezone()


def local_day() -> str:
    return _now().date().isoformat()


def _key(kind: str) -> str:
    return f"{KEY_PREFIX}{kind}"


async def used_today(session: AsyncSession, kind: str) -> int:
    rec = (await session.execute(
        select(Setting.value).where(Setting.key == _key(kind))
    )).scalar_one_or_none()
    if isinstance(rec, dict) and rec.get("day") == local_day() and isinstance(
        rec.get("count"), int
    ):
        return rec["count"]
    return 0


_RESERVE = text(
    "INSERT INTO setting (key, value, updated_at) "
    "VALUES (:key, json_object('day', :day, 'count', 1), :now) "
    "ON CONFLICT(key) DO UPDATE SET "
    "value = json_object('day', :day, 'count', "
    "  CASE WHEN json_extract(setting.value, '$.day') = :day "
    "       THEN json_extract(setting.value, '$.count') + 1 ELSE 1 END), "
    "updated_at = :now "
    "WHERE json_extract(setting.value, '$.day') IS NOT :day "
    "   OR json_extract(setting.value, '$.count') < :cap"
)


async def reserve(session: AsyncSession, kind: str, cap: int) -> bool:
    """Count one fetch for today against the provider `kind` and return True, or return False
    (counting nothing) when the day's cap is already used. Committed before returning so the
    record is durable. One statement: the check and the increment cannot interleave."""
    result = await session.execute(
        _RESERVE, {"key": _key(kind), "day": local_day(), "now": utcnow_iso(), "cap": cap}
    )
    await session.commit()
    return result.rowcount == 1
