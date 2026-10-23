"""Debrief capture (SPEC-MODULES 3.4). After a real interview the operator records what was asked,
what went well and badly, who was in the room, next steps, and their read on the role. This writes
`interaction` rows (one per person in the room), updates the `pursuit`, adds any newly discovered
contacts, and reports the predicted-vs-asked question hit rate — a direct measure of whether Block F
is any good. Outcome data proper (offer/reject) feeds the learning loop when the pursuit closes
(§5.6); the debrief itself is the interaction/contact/hit-rate capture, not an outcome label."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Contact, Interaction, Opportunity, Pursuit
from kestrel.interview.questions import expected_questions

_STOPWORDS = frozenset(
    "a an the of to in on for with about your you tell me how what when why did do does "
    "have has was were is are that this it they them their our we us can could would should".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Overlap coefficient (shared content words / smaller question), not Jaccard: a real interviewer's
# phrasing of a predicted question is usually longer or shorter, and Jaccard penalizes that length
# gap so heavily that true paraphrases miss. 0.5 means "half the shorter question's content words
# recur" — enough to call it the same question without matching unrelated ones.
_MATCH_OVERLAP = 0.5
_MIN_SAMPLE = 5  # constraint 6: a hit rate from fewer predicted questions is labeled insufficient


@dataclass
class DebriefResult:
    interactions_written: int = 0
    contacts_added: int = 0
    predicted: int = 0
    asked: int = 0
    matched: list[str] = field(default_factory=list)
    hit_rate: float | None = None
    sufficient: bool = False
    note: str = ""


def _tokens(q: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((q or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def _hits(predicted: list[str], asked: list[str]) -> list[str]:
    """A predicted question counts as hit if some asked question shares enough significant tokens
    (overlap coefficient over content words). Interview questions are paraphrased and vary in
    length, so exact match is useless and Jaccard over-penalizes the length gap."""
    asked_tokens = [_tokens(a) for a in asked]
    matched: list[str] = []
    for p in predicted:
        pt = _tokens(p)
        if not pt:
            continue
        for at in asked_tokens:
            smaller = min(len(pt), len(at))
            if smaller and len(pt & at) / smaller >= _MATCH_OVERLAP:
                matched.append(p)
                break
    return matched


async def _find_or_create_contact(
    session: AsyncSession, org_id: int | None, person: dict
) -> tuple[Contact, bool]:
    name = (person.get("name") or "").strip()
    existing = None
    if name and org_id is not None:
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.org_id == org_id, func.lower(Contact.name) == name.lower()
                )
            )
        ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    contact = Contact(
        org_id=org_id,
        name=name or "(unnamed)",
        title=person.get("title"),
        contact_type=person.get("contact_type") or "peer",
        source="interview_debrief",
        relationship="known",
    )
    session.add(contact)
    await session.flush()
    return contact, True


async def capture_debrief(
    session: AsyncSession,
    opportunity_id: int,
    *,
    questions_asked: list[str],
    predicted_questions: list[str] | None = None,
    panel: list[dict] | None = None,
    went_well: str | None = None,
    went_poorly: str | None = None,
    next_steps: str | None = None,
    role_read: str | None = None,
) -> DebriefResult:
    """Persist the debrief and return the hit-rate report. Panel members become interactions (and
    new contacts if not already known); the pursuit gets its next action and a dated note."""
    opportunity = await session.get(Opportunity, opportunity_id)
    org_id = opportunity.org_id if opportunity else None
    now = utcnow_iso()
    result = DebriefResult()

    summary = " | ".join(
        p for p in (f"went well: {went_well}" if went_well else None,
                    f"went poorly: {went_poorly}" if went_poorly else None,
                    f"read: {role_read}" if role_read else None) if p
    ) or "interview debrief"

    for person in panel or []:
        contact, created = await _find_or_create_contact(session, org_id, person)
        result.contacts_added += int(created)
        session.add(
            Interaction(
                contact_id=contact.id,
                opportunity_id=opportunity_id,
                kind="interview_debrief",
                direction="met",
                occurred_at=now,
                summary=summary,
                outcome=next_steps,
            )
        )
        result.interactions_written += 1

    pursuit = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    ).scalar_one_or_none()
    if pursuit is not None:
        pursuit.last_activity_at = now
        if next_steps:
            pursuit.next_action = next_steps
        note = f"[{now[:10]}] Debrief: {summary}"
        pursuit.notes_md = (pursuit.notes_md + "\n\n" + note) if pursuit.notes_md else note

    if predicted_questions is None:
        # Score against the role's own predicted questions: the P14 set once it exists, else
        # Tier 2 Block F (D-INTERVIEW-1).
        predicted_questions = await expected_questions(session, opportunity_id)
    predicted = [p for p in (predicted_questions or []) if p]
    asked = [q for q in (questions_asked or []) if q]
    result.predicted = len(predicted)
    result.asked = len(asked)
    if predicted:
        result.matched = _hits(predicted, asked)
        result.hit_rate = round(len(result.matched) / len(predicted), 2)
        result.sufficient = len(predicted) >= _MIN_SAMPLE
        if result.sufficient:
            result.note = f"{len(result.matched)}/{len(predicted)} predicted questions were asked."
        else:
            result.note = (
                f"{len(result.matched)}/{len(predicted)} predicted questions were asked, but with "
                f"fewer than {_MIN_SAMPLE} predictions this hit rate is not yet a reliable signal."
            )
    else:
        result.note = "No predicted questions to compare against."

    await session.commit()
    return result
