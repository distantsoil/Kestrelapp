"""Score this role anyway (D-UI-74, amended): the record that a role is scored despite a Tier 0
rule.

One setting row per role, `tier0_override.<opportunity id>`, holding the rule that had set it
aside and when. It is state, not configuration (setting_overrides keeps the prefix out of the
config layer). The Score again dialog only queues the intent on a t1 task; Tier 1 writes this
row in the same commit as its result (kestrel.pipeline.tier1), so a task that errors or dies
records nothing. Once recorded, Tier 0 reads it so the next pass over the role (a reset to
`new`, an MCP re-evaluation) never sets it aside again (kestrel.pipeline.tier0), the startup
sweep re-queues it if it is stranded at t0_reject (kestrel.pipeline.orphans), and the role says
"You asked to score this despite your rule on seniority" (kestrel.web.fit.RuleWhy). The
operator's rules themselves are never changed."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

PREFIX = "tier0_override."


def key_for(opportunity_id: int) -> str:
    return f"{PREFIX}{int(opportunity_id)}"


async def record(session: AsyncSession, opportunity_id: int, stage_reason: str | None) -> None:
    """Store (or refresh) the operator's choice in the caller's session; the caller commits.
    The Tier 0 reason is kept as it was, so the role can name the rule after a later tier has
    rewritten its stage_reason."""
    from kestrel.web.fit import rule_id

    value = {"rule": rule_id(stage_reason), "reason": stage_reason or "", "at": utcnow_iso()}
    row = await session.get(Setting, key_for(opportunity_id))
    if row is None:
        session.add(Setting(key=key_for(opportunity_id), value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def get(session: AsyncSession, opportunity_id: int) -> dict | None:
    row = await session.get(Setting, key_for(opportunity_id))
    return row.value if row is not None and isinstance(row.value, dict) else None


async def overridden_ids(session: AsyncSession) -> set[int]:
    """Every role the operator chose to score despite a rule."""
    keys = (
        await session.execute(select(Setting.key).where(Setting.key.like(f"{PREFIX}%")))
    ).scalars().all()
    out: set[int] = set()
    for k in keys:
        tail = k[len(PREFIX):]
        if tail.isdigit():
            out.add(int(tail))
    return out
