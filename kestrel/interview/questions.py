"""Reading the likely interview questions for a role (P14, D-INTERVIEW-1).

When a role is marked Applied, a queued task writes an `interview_question_set` for it
(kestrel.interview.question_task). Once that set exists it SUPERSEDES the Tier 2 Block F questions
for the prep pack, the mock interview and the debrief; Block F stays the source for a role that is
not Applied and for an Applied role whose set has not been written yet (or was quarantined).
This module is the read side only, so prep, mock and debrief can use it without importing the
model-calling code."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, InterviewQuestionSet

# Which categories open a mock interview in each mode, most fitting first.
_OPENER_CATEGORIES: dict[str, tuple[str, ...]] = {
    "behavioral": ("behavioral", "gap_probe"),
    "technical_leadership": ("technical", "role_specific"),
    "executive_panel": ("motivation", "culture", "role_specific"),
    "consulting_discovery": ("role_specific", "technical"),
}


async def active_set(session: AsyncSession, opportunity_id: int) -> InterviewQuestionSet | None:
    """The role's current question set (the one not superseded), newest first, or None."""
    return (
        await session.execute(
            select(InterviewQuestionSet)
            .where(
                InterviewQuestionSet.opportunity_id == opportunity_id,
                InterviewQuestionSet.superseded_at.is_(None),
            )
            .order_by(InterviewQuestionSet.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def set_questions(qset: InterviewQuestionSet | None) -> list[dict]:
    """The set's well-formed questions (a dict with question text), else []."""
    if qset is None:
        return []
    return [
        q for q in (qset.questions or [])
        if isinstance(q, dict) and str(q.get("question") or "").strip()
    ]


async def block_f_questions(session: AsyncSession, opportunity_id: int) -> list[str]:
    """The Tier 2 Block F `likely_question` texts from the newest Tier 2 evaluation."""
    ev = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opportunity_id, Evaluation.tier == 2)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    blocks = ev.blocks if ev is not None and isinstance(ev.blocks, dict) else {}
    out: list[str] = []
    for item in blocks.get("f_interview") or []:
        text = str(item.get("likely_question") or "").strip() if isinstance(item, dict) else ""
        if text:
            out.append(text)
    return out


async def expected_questions(session: AsyncSession, opportunity_id: int) -> list[str]:
    """The questions the debrief scores the real interview against: the P14 set when present,
    else Block F. Empty when neither exists."""
    from_set = [str(q["question"]).strip() for q in set_questions(await active_set(
        session, opportunity_id))]
    if from_set:
        return from_set
    return await block_f_questions(session, opportunity_id)


async def opener_for(session: AsyncSession, opportunity_id: int, mode: str) -> str | None:
    """A first mock-interview question from the P14 set that fits the mode, else its first
    question, else None (the caller falls back to the mode's stock opener)."""
    questions = set_questions(await active_set(session, opportunity_id))
    if not questions:
        return None
    for category in _OPENER_CATEGORIES.get(mode, ()):
        for q in questions:
            if q.get("category") == category:
                return str(q["question"]).strip()
    return str(questions[0]["question"]).strip()
