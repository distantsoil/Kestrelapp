"""Label/outcome/selection-override capture (SPEC-MODULES 5.1). verdict_override is written by the
Phase 6 review UI; this module records the other three signals. Every one is cheap to capture at the
moment the operator is already acting; labeling is a one-click side effect of reviewing, never a
separate chore.

`labeled_examples` unifies the operator's judgments into (opportunity, operator_score, verdict,
dimensions) rows, drawing from BOTH explicit `label` rows and `verdict_override` rows (an override
with an operator_score is a label too). This is the training signal calibration and the reranker
consume."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Evaluation,
    Label,
    Opportunity,
    Outcome,
    SelectionOverride,
    VerdictOverride,
)


@dataclass
class LabeledExample:
    opportunity_id: int
    track: str
    operator_score: float
    verdict: str | None
    dimensions: dict | None  # the tier-2 dimension scores this judgment was made against
    source: str  # "label" | "verdict_override"
    created_at: str = ""  # so the caller can take the most-recent N (drift window)


async def record_label(
    session: AsyncSession,
    *,
    opportunity_id: int,
    track: str,
    score: float | None,
    verdict: str | None = None,
    dimensions: dict | None = None,
    rationale: str | None = None,
    source: str = "active_learning",
) -> Label:
    row = Label(
        opportunity_id=opportunity_id,
        track=track,
        score=score,
        verdict=verdict,
        dimensions=dimensions,
        rationale=rationale,
        source=source,
        created_at=utcnow_iso(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def record_outcome(
    session: AsyncSession,
    *,
    pursuit_id: int,
    opportunity_id: int,
    final_status: str | None = None,
    furthest_stage: str | None = None,
    days_to_first_response: int | None = None,
    rejection_reason: str | None = None,
    operator_retrospective: str | None = None,
) -> Outcome:
    row = Outcome(
        pursuit_id=pursuit_id,
        opportunity_id=opportunity_id,
        final_status=final_status,
        furthest_stage=furthest_stage,
        days_to_first_response=days_to_first_response,
        rejection_reason=rejection_reason,
        operator_retrospective=operator_retrospective,
        recorded_at=utcnow_iso(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def record_selection_override(
    session: AsyncSession,
    *,
    doc_render_id: int,
    slot_key: str,
    model_choice: str | None,
    operator_choice: str | None,
    opportunity_id: int | None = None,
) -> SelectionOverride:
    """A resume/capability slot changed after model selection (consumed in Phase 8)."""
    row = SelectionOverride(
        doc_render_id=doc_render_id,
        opportunity_id=opportunity_id,
        slot_key=slot_key,
        model_choice=model_choice,
        operator_choice=operator_choice,
        created_at=utcnow_iso(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _latest_eval_map(session: AsyncSession, opp_ids: list[int]) -> dict[int, Evaluation]:
    if not opp_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Evaluation)
                .where(Evaluation.opportunity_id.in_(opp_ids), Evaluation.tier == 2)
                .order_by(Evaluation.opportunity_id, Evaluation.id)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, Evaluation] = {}
    for ev in rows:  # ascending id -> last write per opp wins
        latest[ev.opportunity_id] = ev
    return latest


async def _eval_by_id(session: AsyncSession, eval_ids: list[int]) -> dict[int, Evaluation]:
    ids = [e for e in eval_ids if e is not None]
    if not ids:
        return {}
    rows = (
        (await session.execute(select(Evaluation).where(Evaluation.id.in_(ids)))).scalars().all()
    )
    return {ev.id: ev for ev in rows}


async def labeled_examples(session: AsyncSession, track: str) -> list[LabeledExample]:
    """The operator's judgments for a track: explicit `label` rows plus `verdict_override` rows that
    carry an operator_score. One example per opportunity — the MOST RECENT judgment wins across both
    kinds (a later correction supersedes an earlier label). Dimensions come from the evaluation the
    operator actually judged (the override's evaluation_id, or the label's stored dimensions), else
    the latest Tier 2 evaluation. Returned oldest-first so the caller can take the most-recent N
    (the drift window)."""
    label_rows = (
        (await session.execute(select(Label).where(Label.track == track))).scalars().all()
    )
    ov_rows = (
        await session.execute(
            select(VerdictOverride, Opportunity.track)
            .join(Opportunity, Opportunity.id == VerdictOverride.opportunity_id)
            .where(Opportunity.track == track, VerdictOverride.operator_score.is_not(None))
        )
    ).all()

    ov_eval = await _eval_by_id(session, [ov.evaluation_id for ov, _ in ov_rows])
    candidates: dict[int, LabeledExample] = {}

    def _consider(ex: LabeledExample) -> None:
        prev = candidates.get(ex.opportunity_id)
        if prev is None or (ex.created_at or "") >= (prev.created_at or ""):
            candidates[ex.opportunity_id] = ex

    for ov, opp_track in ov_rows:
        ev = ov_eval.get(ov.evaluation_id)
        _consider(
            LabeledExample(
                opportunity_id=ov.opportunity_id, track=opp_track,
                operator_score=ov.operator_score, verdict=ov.operator_verdict,
                dimensions=(ev.dimensions if ev else None), source="verdict_override",
                created_at=ov.created_at or "",
            )
        )
    for lb in label_rows:
        if lb.score is None:
            continue
        _consider(
            LabeledExample(
                opportunity_id=lb.opportunity_id, track=lb.track, operator_score=lb.score,
                verdict=lb.verdict, dimensions=lb.dimensions, source="label",
                created_at=lb.created_at or "",
            )
        )

    missing = [oid for oid, ex in candidates.items() if ex.dimensions is None]
    if missing:
        eval_map = await _latest_eval_map(session, missing)
        for oid in missing:
            if oid in eval_map:
                candidates[oid].dimensions = eval_map[oid].dimensions

    return sorted(candidates.values(), key=lambda e: e.created_at)
