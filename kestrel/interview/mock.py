"""Mock interview (P9, SPEC-MODULES 3.3). A conversational loop, not a document: the model asks one
question at a time, the operator answers in text, and the critique scores the answer against an
explicit rubric AND performs the critical check — every substantive claim must appear in the
operator's own proof points or canonical stories. Any claim that does not is flagged, because a
claim that drifts in practice will drift in a real interview.

The system never speaks for the operator: it critiques and asks the next question, and every turn is
persisted before the next is posed (constraint 5), so a killed session resumes at exactly the
question awaiting an answer."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import MockSession, Quarantine, Story
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline.profile import load_profile
from kestrel.writing_check import operator_spans

MODES = ("behavioral", "technical_leadership", "executive_panel", "consulting_discovery")
_MODE_OPENERS = {
    "behavioral": "Tell me about a time you led a team through a difficult change.",
    "technical_leadership": "Walk me through a technical decision you owned and still defend.",
    "executive_panel": "Why this role, and why now, in your own words?",
    "consulting_discovery": "A prospective client asks what you would do in your first 30 days.",
}
_PROOF_MARKER = '{for each proof point: "- {claim} ({evidence})"}'
_STORY_MARKER = '{for each story: "- {title}: {one-line}"}'


def _pp(p: Any, attr: str) -> Any:
    return p.get(attr) if isinstance(p, dict) else getattr(p, attr, None)


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    """Preserve a failed P9 critique with its raw response (hard rule 3), never a silent no-op."""

    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=None,
                tier=9,
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error or "")[:4000],
                attempts=2,
            )
        )

    return hook


def build_mock_context(proof_points: list, stories: list[Story]) -> tuple[str, str]:
    """The two substantiation blocks P9 checks answers against: proof points and canonical stories.
    These are the ONLY claims the operator can stand behind, so they are what the model is told."""
    proof_block = "\n".join(
        f"- {_pp(p, 'claim')} ({_pp(p, 'evidence')})" for p in proof_points
    ) or "(none provided)"
    story_block = "\n".join(
        f"- {s.title}: {(s.result or s.reflection or '').strip()}" for s in stories
    ) or "(none yet)"
    return proof_block, story_block


def _render_user(
    template: str, *, proof_block: str, story_block: str, question: str, answer: str
) -> str:
    text = template.replace(_PROOF_MARKER, proof_block).replace(_STORY_MARKER, story_block)
    text = text.replace("{question}", question).replace("{answer}", answer)
    return text


async def _canonical_stories(session: AsyncSession) -> list[Story]:
    return list(
        (await session.execute(select(Story).where(Story.is_canonical.is_(True)))).scalars().all()
    )


async def start_session(
    session: AsyncSession,
    opportunity_id: int,
    mode: str,
    *,
    first_question: str | None = None,
) -> MockSession:
    """Open a mock session and pose the first question. The pending question is persisted as a
    transcript turn with a null answer, so the session is resumable before any answer is given."""
    if mode not in MODES:
        raise ValueError(f"unknown mock mode: {mode!r}")
    if first_question is None:
        # The role's own likely questions (P14, once it is marked Applied) open the session; a
        # role without a set gets the mode's stock opener.
        from kestrel.interview.questions import opener_for

        first_question = await opener_for(session, opportunity_id, mode)
    question = (first_question or _MODE_OPENERS[mode]).strip()
    ms = MockSession(
        opportunity_id=opportunity_id,
        mode=mode,
        transcript=[_pending_turn(question)],
        question_count=0,
        mean_scores={},
    )
    session.add(ms)
    await session.commit()
    await session.refresh(ms)
    return ms


def _pending_turn(question: str) -> dict:
    return {
        "question": question,
        "answer": None,
        "critique": None,
        "scores": None,
        "unsupported_claims": None,
    }


_RUBRIC = ("structure", "specificity", "relevance", "length", "ownership")


async def submit_answer(
    session: AsyncSession,
    client: Any,
    mock_session_id: int,
    answer: str,
    *,
    proof_points: list | None = None,
    stories: list[Story] | None = None,
    ran_on: str | None = None,
) -> dict | None:
    """Critique the answer to the pending question (P9), record the turn, and pose the next
    question. Returns the filled transcript turn, or None if there was no pending question or the
    call failed. The unsupported_claims list is preserved on the turn — it is the whole point."""
    ms = await session.get(MockSession, mock_session_id)
    if ms is None:
        return None
    transcript = list(ms.transcript or [])
    pending = next((t for t in transcript if t.get("answer") is None), None)
    if pending is None:
        return None

    if proof_points is None:
        profile = load_profile("employment")
        proof_points = list(getattr(profile, "proof_points", None) or []) if profile else []
    if stories is None:
        stories = await _canonical_stories(session)
    proof_block, story_block = build_mock_context(proof_points, stories)

    spec = load_prompt("mock_critique/v2")
    user = _render_user(
        spec.user,
        proof_block=proof_block,
        story_block=story_block,
        question=pending["question"],
        answer=answer,
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    # Through the Hosted mode plan (D-PRIV-1): on an online tier the confidential employer is
    # redacted before the send, or the send is refused when nothing is marked.
    from kestrel.pipeline._shared import run_prompt_hosted

    hook = _quarantine_hook(session)

    async def run(msgs: list[dict], reask: bool = False) -> Any:
        return await run_prompt_hosted(
            session, client, spec, msgs, quarantine_tier=2,
            on_quarantine=None if reask else hook,  # a failed re-ask leaves no quarantine row
        )

    result = await run(messages)
    if not result.ok or result.value is None:
        await session.commit()  # persist the quarantine row the hook staged (hard rule 3)
        return None

    # The person's own answer is quoted back in the critique (in revised_opening too): every
    # sentence and clause of it is skipped by span, never judged.
    plain = await enforce(
        spec, result.value, messages=messages, run=run, exempt=operator_spans(answer)
    )
    v = plain.value
    scores = v.scores.model_dump()
    pending["answer"] = answer
    pending["critique"] = {"worked_well": v.worked_well, "improvements": list(v.improvements)}
    pending["scores"] = scores
    pending["unsupported_claims"] = list(v.unsupported_claims)
    if plain.flags:
        pending["style_flags"] = plain.flags
    if ran_on:
        pending["ran_on"] = ran_on  # "computer" or "online", shown on the feedback

    transcript.append(_pending_turn(v.next_question.strip()))
    ms.transcript = transcript
    ms.question_count = (ms.question_count or 0) + 1
    ms.mean_scores = _mean_scores(transcript)
    await session.commit()
    return pending


def _mean_scores(transcript: list[dict]) -> dict:
    answered = [t["scores"] for t in transcript if t.get("scores")]
    if not answered:
        return {}
    return {
        dim: round(sum(s.get(dim, 0) for s in answered) / len(answered), 2) for dim in _RUBRIC
    }


async def end_session(session: AsyncSession, mock_session_id: int) -> MockSession | None:
    """Close the session: drop the trailing unanswered question and stamp ended_at."""
    ms = await session.get(MockSession, mock_session_id)
    if ms is None:
        return None
    transcript = [t for t in (ms.transcript or []) if t.get("answer") is not None]
    ms.transcript = transcript
    ms.mean_scores = _mean_scores(transcript)
    ms.ended_at = utcnow_iso()
    await session.commit()
    await session.refresh(ms)
    return ms


async def skip_question(
    session: AsyncSession, mock_session_id: int, candidates: list[str]
) -> str | None:
    """Swap the pending question for another of the role's likely questions that this session has
    not asked. Returns the new question, or None when there is nothing else to ask (the pending
    one stays). Skipping records nothing about the operator: no answer, no score."""
    ms = await session.get(MockSession, mock_session_id)
    if ms is None or ms.ended_at is not None:
        return None
    # copies: a JSON column is not change-tracked
    transcript = [dict(t) for t in ms.transcript or []]
    pending = next((t for t in transcript if t.get("answer") is None), None)
    if pending is None:
        return None
    asked = {str(t.get("question") or "").strip().casefold() for t in transcript}
    pick = next((q for q in candidates if q.strip().casefold() not in asked), None)
    if pick is None:
        return None
    pending["question"] = pick.strip()
    ms.transcript = transcript
    await session.commit()
    return pending["question"]


async def acknowledge_claim(
    session: AsyncSession, mock_session_id: int, turn_index: int, claim_index: int
) -> bool:
    """"Got it" on a flagged claim: remember the operator has seen it, so it stays dismissed after
    a reload. The claim and the turn are left as they were (the flag is still on the record)."""
    ms = await session.get(MockSession, mock_session_id)
    if ms is None:
        return False
    # copies: a JSON column is not change-tracked
    transcript = [dict(t) for t in ms.transcript or []]
    if not 0 <= turn_index < len(transcript):
        return False
    turn = transcript[turn_index]
    claims = turn.get("unsupported_claims") or []
    if not 0 <= claim_index < len(claims):
        return False
    seen = sorted({*(turn.get("claims_seen") or []), claim_index})
    turn["claims_seen"] = seen
    ms.transcript = transcript
    await session.commit()
    return True
