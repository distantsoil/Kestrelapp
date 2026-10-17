"""A model chosen for ONE run (D-UI-72, amended): Sources > Run now and Today's Scan now offer
"My usual models" or "A different model, this time only". The pick reaches the FULL REVIEW (Tier
2) only; the quick screen (Tier 1) always runs on the configured Tier 1 model.

The run is recorded on its own `scrape_run` row (`counts["model_override"]`, plus a readable
`counts["model_override_label"]`) for the Runs page. The pick reaches a role only through the run
that is processing it right now, never by looking the run up later:
- a role this run stores at stage `new` gets a one-shot marker row (`run_model.pending.<id>`,
  `attach`), which Tier 0 consumes (`take`) in the same commit that sets the role's stage, and
  puts on the Tier 1 task as `t2_model_override` (kestrel.pipeline.worker.T2_ONLY_KEY); Tier 1
  hands it to its Tier 2 task as `model_override`;
- a posting this run re-reads as changed gets `model_override` on its Tier 2 task directly.
A later Tier 0 pass (an MCP re-evaluation), the startup orphan sweep and every other run find no
marker and use the configured models. Everything is a row, so a restart mid-run keeps the pick on
queued tasks and unconsumed markers (hard rule 5). The configured tier models never change."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Setting

OVERRIDE_KEY = "model_override"
# On a t1 task: the run's pick, for the Tier 2 task Tier 1 enqueues only (never Tier 1 itself).
T2_ONLY_KEY = "t2_model_override"
LABEL_KEY = "model_override_label"
THIS_RUN_ONLY = "this run only"


def override_of(counts: dict | None) -> dict | None:
    """The override recorded in a scrape_run's counts, or None."""
    found = (counts or {}).get(OVERRIDE_KEY)
    return dict(found) if isinstance(found, dict) and found.get("model") else None


def label_of(counts: dict | None) -> str | None:
    """"<model> (this run only)" for a scrape_run's counts, or None for a run on usual models."""
    if override_of(counts) is None:
        return None
    base = (counts or {}).get(LABEL_KEY) or (counts or {}).get(OVERRIDE_KEY, {}).get("model")
    return f"{base} ({THIS_RUN_ONLY})"


def stamp(counts: dict[str, Any], override: dict | None, label: str | None) -> None:
    """Record `override` (and its label) on a run's counts dict, in place."""
    if override:
        counts[OVERRIDE_KEY] = dict(override)
        counts[LABEL_KEY] = label or str(override.get("model") or "")


PENDING_PREFIX = "run_model.pending."


def _pending_key(opportunity_id: int) -> str:
    return f"{PENDING_PREFIX}{int(opportunity_id)}"


async def attach(session: AsyncSession, opportunity_id: int, override: dict | None) -> None:
    """Mark a role the CURRENT run just stored (stage `new`) for this run's pick, in the
    caller's session (committed with the posting). No override, no marker."""
    if not override or not override.get("model"):
        return
    row = await session.get(Setting, _pending_key(opportunity_id))
    if row is None:
        session.add(Setting(key=_pending_key(opportunity_id), value=dict(override)))
    else:
        row.value = dict(override)


async def take(session: AsyncSession, opportunity_id: int) -> dict | None:
    """Consume a role's marker (Tier 0, in the commit that sets its stage): the pick, or None.
    A second pass over the role finds nothing."""
    row = await session.get(Setting, _pending_key(opportunity_id))
    if row is None:
        return None
    value = row.value
    await session.delete(row)
    return dict(value) if isinstance(value, dict) and value.get("model") else None


async def discard(session: AsyncSession, opportunity_id: int) -> None:
    """Drop a role's marker without using it (a role reset by hand starts on usual models)."""
    row = await session.get(Setting, _pending_key(opportunity_id))
    if row is not None:
        await session.delete(row)


async def sweep_stale(session: AsyncSession) -> int:
    """Delete markers whose role is gone or no longer at `new` (archived before Tier 0 saw it,
    then moved on). Called at worker startup; the caller commits."""
    from sqlalchemy import select

    from kestrel.db.models import Opportunity

    keys = (
        await session.execute(select(Setting.key).where(Setting.key.like(f"{PENDING_PREFIX}%")))
    ).scalars().all()
    ids = {int(k[len(PENDING_PREFIX):]): k for k in keys if k[len(PENDING_PREFIX):].isdigit()}
    if not ids:
        return 0
    new = set(
        (
            await session.execute(
                select(Opportunity.id).where(
                    Opportunity.id.in_(list(ids)), Opportunity.stage == "new"
                )
            )
        ).scalars()
    )
    gone = [k for oid, k in ids.items() if oid not in new]
    for k in gone:
        row = await session.get(Setting, k)
        if row is not None:
            await session.delete(row)
    return len(gone)
