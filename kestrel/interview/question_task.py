"""Likely interview questions as a queued task (P14, D-INTERVIEW-1, hard rule 5).

Marking a role Applied enqueues one `interview_questions` task for it (the applied branch of
`move_to_status`, kestrel.services.pursuit_board), never a bulk import. The serve worker drains
the tasks right after the story build, on the same Tier 2 model, so the tier-drain rule holds. The
model writes questions only: each may point at one of the operator's real stories and proof points
by id, and both fields are enums of the ids that exist (the P3 and P13 pattern), so an invented id
is structurally impossible on a constrained backend and a validation failure anywhere else. A reply
that fails validation is repaired once, then quarantined with the raw reply kept; it never crashes
a run and never silently falls back to a default.

Resumable: the lease is renewed while a call runs, a killed worker's task is reclaimed after the
lease expires, and a re-leased task that finds its set already written does not write a second."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, create_model
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import (
    Evaluation,
    InterviewQuestionSet,
    Opportunity,
    Org,
    Pursuit,
    Quarantine,
    Story,
)
from kestrel.interview.questions import active_set
from kestrel.llm.schemas import (
    MAX_QUESTIONS,
    P14InterviewQuestions,
    P14QuestionCore,
    _cap,
    _max_items,
)
from kestrel.pipeline.tasks import enqueue_once

log = structlog.get_logger()

TASK_TYPE = "interview_questions"
PROMPT = "interview_questions/v1"
QUARANTINE_TIER = 14  # the quarantine row's `tier` names the prompt, as P8, P9 and P13 do
MAX_ATTEMPTS = 3
_LIVE = ("pending", "leased")


# ---------------------------------------------------------------------------------- enqueue
async def request_interview_questions(
    session: AsyncSession, opportunity_id: int, pursuit_id: int, *, regenerate: bool = False
) -> bool:
    """Queue one question task for this role unless one is already waiting or running (dedup on
    the payload's opportunity_id). Without `regenerate`, a role that already has an active set
    queues nothing. Flushes, never commits: the caller commits with the status change. True when
    a task was added."""
    if not regenerate and await active_set(session, opportunity_id) is not None:
        return False
    added = await enqueue_once(
        session, TASK_TYPE, opportunity_id,
        extra={"pursuit_id": pursuit_id, "regenerate": regenerate, "requested_at": utcnow_iso()},
    )
    await session.flush()
    return added


async def pending(session: AsyncSession, opportunity_id: int) -> bool:
    """Whether a question task for this role is queued or running."""
    from kestrel.db.models import Task

    n = (
        await session.execute(
            select(func.count()).select_from(Task).where(
                Task.type == TASK_TYPE,
                Task.status.in_(_LIVE),
                func.json_extract(Task.payload, "$.opportunity_id") == opportunity_id,
            )
        )
    ).scalar_one()
    return bool(n)


# ------------------------------------------------------------------------- the per-call schema
def question_model_for(story_ids: list[int], proof_keys: list[str]) -> type[BaseModel]:
    """The P14 model with `story_id` an enum of this operator's real story ids and
    `proof_point_key` an enum of their real proof point keys, each also allowed to be null. A
    field with nothing to point at is left out of this call's schema altogether."""
    fields: dict[str, Any] = {}
    if story_ids:
        story_type: Any = Literal[tuple(story_ids)]  # type: ignore[valid-type]
        fields["story_id"] = (story_type | None, None)
    if proof_keys:
        key_type: Any = Literal[tuple(proof_keys)]  # type: ignore[valid-type]
        fields["proof_point_key"] = (key_type | None, None)
    question = create_model("P14QuestionForRole", __base__=P14QuestionCore, **fields)
    return create_model(
        "P14InterviewQuestionsForRole", __base__=P14InterviewQuestions,
        questions=(
            Annotated[list[question], _cap(MAX_QUESTIONS), _max_items(MAX_QUESTIONS)], ...
        ),
    )


def question_schema(model_cls: type[BaseModel]) -> dict:
    from kestrel.docs.assess import assess_schema

    return assess_schema(model_cls)


# ---------------------------------------------------------------------------------- the inputs
@dataclass
class _Inputs:
    user: str
    story_ids: list[int]
    proof_keys: list[str]


def _pp(p: Any, attr: str) -> Any:
    return p.get(attr) if isinstance(p, dict) else getattr(p, attr, None)


async def _inputs(
    session: AsyncSession, settings: Settings, opp: Opportunity, template: str, tier: str
) -> _Inputs:
    from kestrel.ingest.tokens import truncate_middle
    from kestrel.interview.prep import _latest_research
    from kestrel.pipeline._shared import jd_budget
    from kestrel.pipeline.context import normalize_description, profile_full
    from kestrel.pipeline.profile import load_profile

    org = await session.get(Org, opp.org_id) if opp.org_id else None
    profile = load_profile(opp.track)
    proof_keys = [
        str(k) for p in (getattr(profile, "proof_points", None) or [])
        if (k := _pp(p, "key"))
    ]
    stories = list(
        (
            await session.execute(
                select(Story).where(Story.is_canonical.is_(True)).order_by(Story.id)
            )
        ).scalars().all()
    )
    ev = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opp.id, Evaluation.tier == 2)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    blocks = ev.blocks if ev is not None and isinstance(ev.blocks, dict) else {}
    a = blocks.get("a_role_summary")
    summary = (a.get("lead_with") if isinstance(a, dict) else None) or "(no summary yet)"
    gaps = [
        f"- [{g.get('severity')}] {g.get('requirement')}"
        for g in ((blocks.get("b_fit") or {}).get("gaps") or [])
        if isinstance(g, dict) and g.get("requirement")
    ]
    seeds = [
        f"- {i.get('likely_question')}"
        for i in (blocks.get("f_interview") or [])
        if isinstance(i, dict) and i.get("likely_question")
    ]
    research = await _latest_research(session, org.id if org else None)
    unknowns = [f"- {u}" for u in (research.unknowns if research else []) if isinstance(u, str)]
    desc = truncate_middle(
        normalize_description(opp.description_md),
        jd_budget(settings, tier, settings.pipeline.max_jd_tokens),
    )
    values = {
        "{job.title}": opp.title or "(untitled)",
        "{job.company}": (org.name if org else None) or opp.org_name or "(unknown)",
        "{ROLE_SUMMARY}": summary,
        "{description}": desc or "(no description)",
        "{GAPS}": "\n".join(gaps) or "(none recorded)",
        "{SEED_QUESTIONS}": "\n".join(seeds) or "(none)",
        "{UNKNOWNS}": "\n".join(unknowns) or "(none recorded)",
        "{PROFILE_FULL}": profile_full(profile),
        "{STORIES}": "\n".join(f"{s.id}: {s.title}" for s in stories) or "(none yet)",
        "{PROOF_KEYS}": ", ".join(proof_keys) or "(none)",
    }
    user = template
    for marker, text in values.items():
        user = user.replace(marker, text)
    return _Inputs(user=user, story_ids=[s.id for s in stories], proof_keys=proof_keys)


# --------------------------------------------------------------------------------- generating
@dataclass
class GenerateOutcome:
    status: str  # stored | skipped | empty | quarantined | error
    set_id: int | None = None
    error: str | None = None


async def generate_question_set(
    session: AsyncSession,
    client: Any,
    settings: Settings,
    *,
    opportunity_id: int,
    pursuit_id: int,
    regenerate: bool = False,
    requested_at: str | None = None,
) -> GenerateOutcome:
    """Ask for the role's questions and store the set. The session is committed here. A set
    written since the task was requested means a re-leased task already did the work (hard
    rule 5), so it skips. A regenerated set supersedes the old one only once it has validated."""
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import make_quarantine_hook, run_prompt_hosted

    opp = await session.get(Opportunity, opportunity_id)
    pursuit = await session.get(Pursuit, pursuit_id)
    if opp is None or pursuit is None:
        return GenerateOutcome("skipped", error="the role or its pursuit no longer exists")
    current = await active_set(session, opportunity_id)
    if current is not None and (
        not regenerate or (requested_at is not None and current.created_at >= requested_at)
    ):
        return GenerateOutcome("skipped", set_id=current.id)

    spec = load_prompt(PROMPT)
    inputs = await _inputs(session, settings, opp, spec.user, spec.tier)
    model_cls = question_model_for(inputs.story_ids, inputs.proof_keys)
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": inputs.user},
    ]
    result = await run_prompt_hosted(
        session, client, replace(spec, model_cls=model_cls), messages,
        quarantine_tier=QUARANTINE_TIER, opportunity_id=opp.id,
        on_quarantine=make_quarantine_hook(session, tier=QUARANTINE_TIER, opportunity_id=opp.id),
        schema=question_schema(model_cls),
    )
    if result.quarantined:
        await session.commit()  # the quarantine row (raw reply kept) staged by the hook
        log.warning("interview_questions_quarantined", opportunity_id=opp.id, error=result.error)
        return GenerateOutcome("quarantined", error=result.error)
    if not result.ok or result.value is None:
        await session.rollback()
        return GenerateOutcome("error", error=result.error or "no reply")

    questions = [
        {
            "question": q.question,
            "category": q.category,
            "why_asked": q.why_asked,
            "proof_point_key": getattr(q, "proof_point_key", None),
            "story_id": getattr(q, "story_id", None),
        }
        for q in result.value.questions
        if q.question.strip()
    ]
    if not questions:
        await session.rollback()
        log.warning("interview_questions_empty", opportunity_id=opp.id)
        return GenerateOutcome("empty", error="the model returned no questions")
    now = utcnow_iso()
    if current is not None:
        current.superseded_at = now
        await session.flush()  # free the one-active-set-per-pursuit slot before the insert
    row = InterviewQuestionSet(
        opportunity_id=opp.id, pursuit_id=pursuit.id, prompt_version=spec.version,
        questions=questions, created_at=now,
    )
    session.add(row)
    await session.commit()
    return GenerateOutcome("stored", set_id=row.id)


# ------------------------------------------------------------------------------------ draining
async def drain_interview_questions(
    sm: async_sessionmaker[AsyncSession], client: Any, settings: Settings, *, stop: Any = None
) -> int:
    """Run every queued question task, one at a time. A transport error or an unexpected failure
    goes back to the queue while attempts remain; a quarantined reply completes the task (the raw
    reply is kept on the Quarantine page and retrying a model that cannot meet the schema would
    loop). Never raises into the caller's worker cycle."""
    from kestrel.interview.story_task import LEASE_S, _renew
    from kestrel.queue.queue import TaskQueue

    queue = TaskQueue(sm)
    wid = "pipeline-questions"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        payload = task.payload or {}
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            async with sm() as s:
                outcome = await generate_question_set(
                    s, client, settings,
                    opportunity_id=int(payload.get("opportunity_id") or 0),
                    pursuit_id=int(payload.get("pursuit_id") or 0),
                    regenerate=bool(payload.get("regenerate")),
                    requested_at=payload.get("requested_at"),
                )
        except Exception as exc:  # a failed task is retried, never fatal to the cycle
            beat.cancel()
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("interview_questions_failed", task=task.id, error=reason)
            await queue.fail(task.id, reason, worker_id=wid, retry=True)
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        if outcome.status == "error":
            await queue.fail(task.id, outcome.error or "error", worker_id=wid, retry=True)
            continue
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done


# ------------------------------------------------------------------------------- page support
async def question_view(session: AsyncSession, opportunity_id: int) -> dict:
    """What the opportunity page says about the role's questions: the active set with each
    question's story title, whether a task is queued or running, and whether the newest attempt
    was quarantined (so the page can say the earlier Tier 2 questions still apply)."""
    qset = await active_set(session, opportunity_id)
    titles: dict[int, str] = {}
    if qset is not None:
        ids = {q.get("story_id") for q in (qset.questions or []) if isinstance(q, dict)}
        ids.discard(None)
        if ids:
            rows = (await session.execute(select(Story).where(Story.id.in_(ids)))).scalars()
            titles = {s.id: s.title for s in rows}
    questions = [
        {**q, "story_title": titles.get(q.get("story_id"))}
        for q in (qset.questions if qset is not None else [])
        if isinstance(q, dict) and q.get("question")
    ]
    newest_bad = (
        await session.execute(
            select(func.max(Quarantine.created_at)).where(
                Quarantine.opportunity_id == opportunity_id,
                Quarantine.tier == QUARANTINE_TIER,
            )
        )
    ).scalar_one_or_none()
    failed = bool(newest_bad and (qset is None or newest_bad > qset.created_at))
    return {
        "questions": questions,
        "created_at": qset.created_at if qset is not None else None,
        "pending": await pending(session, opportunity_id),
        "failed": failed,
    }
