"""Has the person done something with this role? The one place that question is answered
(issue #250, D-SCORE-919 as amended).

A recheck that judges stored roles by today's filters (the legacy lane's, kestrel.pipeline.lane)
must never restage a role the person acted on. Marks of the person's are kept in many places: a
column on the role, rows in tables that point at it, and settings rows whose key ends in the
role's id. A list of marks written next to each recheck kept missing some, so every such place is
classified here, once, as the person's or the machine's, and tests/test_person_touched.py fails
when a table that points at a role, or a settings key prefix, is in neither list.

Not here, on purpose: whether a TASK was asked for by the person (that is on the task's payload,
kestrel.pipeline.lane.asked_by_person) and how far scoring got (kestrel.queue.rescore.past_tier1).
"""

from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import (
    CompObservation,
    DocRender,
    Evaluation,
    FrontierTransmission,
    Interaction,
    InterviewQuestionSet,
    Label,
    Letter,
    MockSession,
    Notification,
    Opportunity,
    OpportunityEmbedding,
    Outcome,
    OutreachDraft,
    PrepPack,
    Pursuit,
    Quarantine,
    RequirementCorrection,
    SelectionOverride,
    Setting,
    StoryUsage,
    Task,
    VerdictOverride,
)

# Tables whose row for a role exists only because the person did something with that role.
PERSON_TABLES = (
    Pursuit,  # shortlisted, applied, interviewing, closed: any state
    Outcome,  # what came of a pursuit
    Label,  # their own verdict on the role
    VerdictOverride,  # they changed the verdict
    SelectionOverride,  # they changed which resume text is used for it
    RequirementCorrection,  # they corrected what the posting asks for
    DocRender,  # a resume or statement made for it
    Letter,  # a cover letter for it
    OutreachDraft,  # a message drafted about it
    Interaction,  # a contact logged against it
    StoryUsage,  # a story they used for it
    PrepPack,  # interview preparation they asked for
    InterviewQuestionSet,  # interview questions they asked for
    MockSession,  # a practice interview
    FrontierTransmission,  # a deep read they confirmed sending
)
# Tables Kestrel fills by itself for a role: never a sign the person acted.
MACHINE_TABLES = (
    OpportunityEmbedding,
    Evaluation,  # how far scoring got is rescore.past_tier1's question
    Quarantine,
    Task,  # a requested task is lane.asked_by_person's question
    Notification,
    CompObservation,
)

# Settings rows keyed `<prefix><role id>` that the person wrote for that role.
PERSON_SETTING_PREFIXES = (
    "saved_role.",  # Save for later (kestrel.services.saved_roles)
    "tier0_override.",  # Score this role anyway (kestrel.services.tier0_override)
    "prep_notes.",  # their interview notes (kestrel.interview.notes)
    "prep_story.",  # the story they chose per question (kestrel.interview.prep_state)
    "prep_ask.",  # questions they added or ticked (kestrel.interview.prep_state)
    "prep_people.",  # people they listed for the interview (kestrel.interview.prep_state)
    "retry_removed.",  # Remove on Needs another try (kestrel.services.retry_removal)
)
# Settings key prefixes that are not a person's mark on a role, each with the reason.
OTHER_SETTING_PREFIXES = (
    "run_model.pending.",  # keyed by role, but written by a run for the roles it stores
    "tier0_recheck.",  # keyed by track
    "extension.",  # keyed by feature
    "retry_group_dismissed.",  # keyed by a group of failures, not a role
    "stories.dismiss_undo.group.",  # keyed by an undo token
    "undo.pursuit.",  # keyed by an undo token, kept for a day
    "undo.correction.",  # keyed by an undo token, kept for a day
    "scoring.weights.",  # keyed by track and dimension
    "llm.hosted_mode.",  # a settings override
    "models.undo.",  # keyed by an undo token
    "models.base.",  # keyed by model
)


def setting_keys(opportunity_id: int) -> tuple[str, ...]:
    return tuple(f"{prefix}{int(opportunity_id)}" for prefix in PERSON_SETTING_PREFIXES)


async def person_touched(session: AsyncSession, opp: Opportunity) -> bool:
    """True when the person hid the role, or any table or settings row above holds something of
    theirs for it. Read from the stored rows for this role, now."""
    if opp.archived_at is not None:
        return True
    marks = [exists().where(model.opportunity_id == opp.id) for model in PERSON_TABLES]
    marks.append(exists().where(Setting.key.in_(setting_keys(opp.id))))
    return bool(await session.scalar(select(or_(*marks))))
