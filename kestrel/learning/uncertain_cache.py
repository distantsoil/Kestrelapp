"""A cache of the active-learning order, per track (issue 171 review, D-LEARN-8).

Today's quick-round strip asks for its roles on every 30 s poll and every refresh, and Calibrate
asks twice per page (the Quick tab and the Detailed queue). With a few thousand candidates one
`active.rank_uncertain` costs about a second on the event loop. The order itself is unchanged:
this module stores the full output of `rank_uncertain` (ids, uncertainty, components) and serves
slices of it while nothing it reads has changed.

What the order reads, and so what the fingerprint covers: tier-2 evaluations, the track's labels,
verdict overrides, the current-model embeddings, the candidate roles' fields that `calibratable()`
and the duplicate grouping read (description, archive, dedup group, canonical copy, first seen),
the apply threshold, the embedding model, and today's date (recency is whole days). Any change to
one of them misses the cache and ranks again. The cache is process memory only: it is derived
data, so a restart just ranks once more (hard rule 5 is about work, not caches)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.models import Evaluation, Label, Opportunity, OpportunityEmbedding, VerdictOverride
from kestrel.learning.active import UncertainItem, rank_uncertain
from kestrel.llm.embeddings import current_model

_ALL = 10**9  # rank_uncertain's limit for "the whole order"


@dataclass(frozen=True)
class _Entry:
    key: tuple
    order: tuple[tuple[int, int, float, dict], ...]  # (opportunity id, evaluation id, u, parts)


_cache: dict[str, _Entry] = {}


def clear() -> None:
    _cache.clear()


async def _fingerprint(session: AsyncSession, track: str, settings: Settings) -> tuple:
    model = current_model(settings)
    opp = (
        await session.execute(
            select(
                func.count(),
                func.max(Opportunity.id),
                func.count(Opportunity.archived_at),
                func.total(Opportunity.is_canonical),
                func.total(Opportunity.dedup_group_id),
                func.total(func.length(Opportunity.description_md)),
                func.max(Opportunity.first_seen_at),
                func.min(Opportunity.first_seen_at),
            ).where(Opportunity.track == track)
        )
    ).one()
    ev = (
        await session.execute(
            select(
                func.count(),
                func.max(Evaluation.id),
                func.total(Evaluation.score),
                func.total(Evaluation.weighted_score),
            ).where(Evaluation.tier == 2)
        )
    ).one()
    lab = (
        await session.execute(select(func.count(), func.max(Label.id)).where(Label.track == track))
    ).one()
    # Counts and sums let offsetting changes cancel (archive one role, put back another; a label
    # id reused after a delete; a role moved between duplicate groups), so the sets themselves
    # are part of the key, as ordered id lists (D-LEARN-10). They are short: the hidden roles,
    # the roles in a duplicate group, and the labels of one track.
    sets = []
    for cols, where in (
        ((Opportunity.id,), (Opportunity.track == track, Opportunity.archived_at.is_not(None))),
        ((Opportunity.id, Opportunity.dedup_group_id, Opportunity.is_canonical),
         (Opportunity.track == track, Opportunity.dedup_group_id.is_not(None))),
        ((Label.id, Label.opportunity_id, Label.score, Label.verdict), (Label.track == track,)),
    ):
        first = cols[0]
        part = func.coalesce(func.cast(first, String), "")
        for c in cols[1:]:
            part = part.op("||")(":").op("||")(func.coalesce(func.cast(c, String), ""))
        inner = select(part.label("k")).where(*where).order_by(first).subquery()
        sets.append(await session.scalar(select(func.group_concat(inner.c.k, ","))))
    ov = (
        await session.execute(
            select(func.count(), func.max(VerdictOverride.id), func.max(VerdictOverride.created_at))
        )
    ).one()
    emb = (
        await session.execute(
            select(
                func.count(),
                func.total(OpportunityEmbedding.opportunity_id),
                func.max(OpportunityEmbedding.text_hash),
                func.min(OpportunityEmbedding.text_hash),
            ).where(OpportunityEmbedding.model == model)
        )
    ).one()
    return (
        str(paths.db_path()),
        track,
        model,
        float(settings.thresholds.apply),
        date.today().isoformat(),
        tuple(opp),
        tuple(ev),
        tuple(lab),
        tuple(sets),
        tuple(ov),
        tuple(emb),
    )


async def ranked(
    session: AsyncSession, track: str, settings: Settings, *, limit: int = 10
) -> list[UncertainItem]:
    """`rank_uncertain(session, track, settings, limit=limit)`, served from the cache while its
    inputs are unchanged. The rows of the slice are loaded fresh in `session`."""
    key = await _fingerprint(session, track, settings)
    entry = _cache.get(track)
    if entry is None or entry.key != key:
        full = await rank_uncertain(session, track, settings, limit=_ALL)
        entry = _Entry(
            key,
            tuple(
                (u.opportunity.id, u.evaluation.id, u.uncertainty, dict(u.components)) for u in full
            ),
        )
        _cache[track] = entry
        if limit >= len(full):
            return full
        return full[:limit]
    head = entry.order[:limit]
    if not head:
        return []
    opps = {
        o.id: o
        for o in (
            await session.execute(
                select(Opportunity).where(Opportunity.id.in_([h[0] for h in head]))
            )
        ).scalars()
    }
    evs = {
        e.id: e
        for e in (
            await session.execute(select(Evaluation).where(Evaluation.id.in_([h[1] for h in head])))
        ).scalars()
    }
    out: list[UncertainItem] = []
    for oid, eid, u, parts in head:
        if oid not in opps or eid not in evs:  # deleted under us: rank afresh
            _cache.pop(track, None)
            return await ranked(session, track, settings, limit=limit)
        out.append(UncertainItem(opps[oid], evs[eid], u, dict(parts)))
    return out
