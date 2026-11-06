"""Duplicate groups for the learning loop (#122, D-LRN-1): one role posted on several boards is one
judgment. Ingest groups the copies (`opportunity.dedup_group_id`) and keeps every row; a role with
no group is its own group. Calibrate, calibration and training count a group once, and a group is
labeled once any copy is rated or overridden."""

from __future__ import annotations

from sqlalchemy import ColumnElement, String, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Opportunity

_CHUNK = 500


def group_key(opportunity_id: int, dedup_group_id: str | None) -> str:
    """The group of one role: its dedup group, else the role on its own."""
    return dedup_group_id or f"__solo_{opportunity_id}"


def group_key_expr() -> ColumnElement[str]:
    """`group_key` as SQL, for counting distinct groups in one query."""
    return func.coalesce(
        func.nullif(Opportunity.dedup_group_id, ""),
        literal("__solo_").concat(cast(Opportunity.id, String)),
    )


async def group_keys(session: AsyncSession, opportunity_ids: list[int]) -> dict[int, str]:
    """The group key of each role id (ids that no longer exist are left out)."""
    out: dict[int, str] = {}
    ids = sorted(set(opportunity_ids))
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        for oid, gid in (
            await session.execute(
                select(Opportunity.id, Opportunity.dedup_group_id).where(Opportunity.id.in_(chunk))
            )
        ).all():
            out[oid] = group_key(oid, gid)
    return out
