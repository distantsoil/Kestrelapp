"""Tier 2 — deep evaluation (SPEC.md 6.3, 7). P2 for employment, P2C for consulting. Workhorse
model, few-shot from the operator's own confirmed labels, full description truncated to
max_jd_tokens, and the comp corpus and ghost history passed as explicit input.

After the model responds, the SYSTEM (never the model) post-processes: weighted score, needs_review
divergence flag, blocker and legitimacy overrides, threshold-derived verdict stored beside the
model's own (SPEC.md 7.3). For consulting, the top-level `legitimacy` is authoritative and
`blocks.g_risk` is narrative detail, and every situation fact and identified buyer is written back
to the org as accumulated signals so the next opportunity there starts warmer (PROMPTS.md P2C)."""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Evaluation, Label, Opportunity, OpportunityEmbedding, Org, Source
from kestrel.llm.client import LLMClient
from kestrel.llm.embeddings import current_model, from_bytes
from kestrel.llm.fewshot import FewShotExample, select_few_shot
from kestrel.llm.guard import run_prompt
from kestrel.pipeline import scoring
from kestrel.pipeline._shared import (
    HandlerOutcome,
    encode_send,
    jd_budget,
    make_quarantine_hook,
    plan_hosted_send,
    prompt_spec,
    record_refusal,
    rescore_failure_prefix,
)
from kestrel.pipeline.context import (
    build_tier2_values,
    comp_intel_summary,
    org_signals_lines,
    render_user,
)

log = structlog.get_logger()

_VERSION = {"employment": "evaluate/v2", "consulting": "evaluate_consulting/v2"}


async def _gather_few_shot(
    session: AsyncSession, opp: Opportunity, settings: Settings
) -> tuple[list[FewShotExample], list[int]]:
    """Build few-shot candidates from the operator's own labels for this track, ranked+diversified
    by select_few_shot and gated at settings.learning.min_labels_for_few_shot (zero-shot below).
    Returns ([], []) when the query opportunity has no embedding to rank against. Only vectors
    from the current embed model are compared (D-EMBED-4): another model's are another space."""
    model = current_model(settings)
    query = await session.get(OpportunityEmbedding, opp.id)
    if query is None or query.model != model:
        return [], []
    rows = (
        await session.execute(
            select(Label, OpportunityEmbedding)
            .join(OpportunityEmbedding, OpportunityEmbedding.opportunity_id == Label.opportunity_id)
            .where(
                Label.track == opp.track,
                Label.opportunity_id != opp.id,
                OpportunityEmbedding.model == model,
            )
        )
    ).all()
    examples = [
        FewShotExample(
            id=label.opportunity_id,
            embedding=from_bytes(emb.vector),
            verdict=label.verdict or "unknown",
            holistic=label.score if label.score is not None else 0.0,
            summary=(label.rationale or "")[:400],
            rationale=(label.rationale or "")[:200],
        )
        for label, emb in rows
    ]
    chosen = select_few_shot(
        from_bytes(query.vector),
        examples,
        k=settings.learning.few_shot_k,
        # Switched off in Settings > Scoring and learning: no floor is ever reached.
        min_labels=(
            settings.learning.min_labels_for_few_shot
            if settings.learning.use_examples else 10**9
        ),
    )
    return chosen, [ex.id for ex in chosen]


def _dimension_scores(dimensions) -> dict[str, float]:
    return {name: getattr(dimensions, name).score for name in type(dimensions).model_fields}


def _writeback_org_signals(org: Org | None, value, opp: Opportunity) -> None:
    """P2C: append each stated situation fact and identified buyer to org.signals, DEDUPED against
    what is already there so re-evaluating the same organization does not inflate the signal count
    (which feeds the consulting Tier 0 `multi_signal` bonus). Reassigns the list so the JSON column
    is marked dirty (the mapping is not a MutableList)."""
    if org is None:
        return
    now = utcnow_iso()
    existing = org.signals or []
    seen = {
        (s.get("category"), s.get("statement"))
        for s in existing
        if isinstance(s, dict)
    }
    candidates: list[tuple[str, str]] = []
    stated = (value.blocks.a_situation.stated or "").strip()
    if stated:
        candidates.append(("situation", stated))
    buyers = value.blocks.c_buyers
    for name in filter(None, [buyers.economic_buyer, *buyers.influencers]):
        candidates.append(("buyer", name))

    new = [
        {"category": cat, "statement": stmt, "observed_at": now,
         "source": "p2c_eval", "opportunity_id": opp.id}
        for cat, stmt in candidates
        if (cat, stmt) not in seen
    ]
    if new:
        org.signals = existing + new


async def handle_t2(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: LLMClient,
    settings: Settings,
    *,
    opportunity_id: int,
    profiles: dict,
    changed: bool = False,
    rescore: bool = False,
    requested_at: str | None = None,
    task_id: int | None = None,
) -> HandlerOutcome:
    """`rescore` marks a score-again task (D-UI-50): the role is evaluated again whatever its
    stage, a NEW Evaluation row is written beside the old ones, and a role already scored keeps
    its stage (and its earlier evaluation) when the new attempt quarantines. `requested_at` is
    when it was asked for: an evaluation made since then means a re-leased task already did the
    work (hard rule 5), so it skips. `task_id` is the score-again task's id: a failure that keeps
    the stage marks its quarantine row with it (_shared.rescore_failure_prefix), so Failed scoring
    shows it as "Score again failed" and Try again re-queues that same task's payload."""
    async with sessionmaker() as session:
        opp = await session.get(Opportunity, opportunity_id)
        if opp is None:
            return HandlerOutcome("skip", 2, error="opportunity missing")
        already_scored = opp.stage in ("t2_done", "t3_done")
        # t1_pass is the normal entry. A material re-scrape enqueues a `changed` task to re-evaluate
        # the new content. Both are idempotent (hard rule 5): the normal entry is guarded by stage,
        # the re-eval by content_version, so a re-leased task after a crash (or a duplicate task)
        # that finds a Tier 2 evaluation already made against this exact version skips instead of
        # writing a second evaluation.
        if opp.stage == "t1_pass":
            pass
        elif rescore:
            if requested_at:
                done_since = (
                    await session.execute(
                        select(func.count())
                        .select_from(Evaluation)
                        .where(
                            Evaluation.opportunity_id == opp.id,
                            Evaluation.tier == 2,
                            Evaluation.created_at >= requested_at,
                        )
                    )
                ).scalar_one()
                if done_since:
                    return HandlerOutcome("skip", 2)
        elif changed:
            already = (
                await session.execute(
                    select(func.count())
                    .select_from(Evaluation)
                    .where(
                        Evaluation.opportunity_id == opp.id,
                        Evaluation.tier == 2,
                        Evaluation.content_version == opp.version,
                    )
                )
            ).scalar_one()
            if already:
                return HandlerOutcome("skip", 2)
        else:
            return HandlerOutcome("skip", 2)

        org = await session.get(Org, opp.org_id) if opp.org_id else None
        source = await session.get(Source, opp.source_id) if opp.source_id else None
        profile = profiles.get(opp.track)
        spec = prompt_spec(_VERSION[opp.track])

        few_shot_examples, few_shot_ids = await _gather_few_shot(session, opp, settings)
        from kestrel.pipeline.context import few_shot_block

        # Hosted mode (SPEC.md 6.4): smaller slice when metered, redact before send, refuse when
        # confidential mode has nothing to redact with.
        plan = await plan_hosted_send(session, settings, spec.tier)
        comp_intel = await comp_intel_summary(session, opp)
        values = build_tier2_values(
            opp=opp,
            org=org,
            profile=profile,
            source_name=source.name if source else "",
            max_jd_tokens=jd_budget(settings, spec.tier, settings.pipeline.max_jd_tokens),
            few_shot=few_shot_block(few_shot_examples),
            comp_intel=comp_intel,
        )
        trigger_event = bool(opp.trigger_data or opp.event_date)
        posted = bool(opp.description_md)
        user = render_user(
            spec.user,
            values,
            trigger_event=trigger_event,
            posted=posted or not trigger_event,
            org_signals=org_signals_lines(org),
        )
        messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]

        keeps_stage = rescore and already_scored
        mark = rescore_failure_prefix(task_id) if keeps_stage else ""
        if plan.refuse:
            # Hard rule 4: refused, not rejected. The item sits in quarantine with the reason
            # until the operator sets a redaction map or turns confidential mode off.
            record_refusal(
                session, tier=2, opportunity_id=opp.id, model=settings.llm.tiers[spec.tier].model,
                reason=plan.refuse, messages=messages, error_prefix=mark,
            )
            if not keeps_stage:
                opp.stage = "quarantined"
                opp.stage_reason = f"t2 {plan.refuse}"[:500]
            await session.commit()
            return HandlerOutcome("quarantined", 2, quarantined=True, error=plan.refuse)
        messages, book = encode_send(messages, plan.mapping)

        hook = make_quarantine_hook(session, tier=2, opportunity_id=opp.id, error_prefix=mark)
        result = await run_prompt(client, spec, messages, on_quarantine=hook,
                                  reply_filter=book)

        if not result.ok and not result.quarantined:
            return HandlerOutcome(
                "error", 2, error=result.error, out_of_credit=result.out_of_credit
            )

        if result.quarantined or result.value is None:
            # Tier 2 is the deepest evaluation: a structural failure quarantines (SPEC.md P2/P2C
            # failure handling). The quarantine row was added by the hook.
            # A score-again of a role already scored keeps its stage: the earlier evaluation
            # still stands and the failed attempt is in quarantine as history.
            if not keeps_stage:
                opp.stage = "quarantined"
                opp.stage_reason = f"t2 quarantine: {result.error}"
            await session.commit()
            return HandlerOutcome("quarantined", 2, quarantined=True, error=result.error)

        value = result.value
        dim_scores = _dimension_scores(value.dimensions)
        blockers = [{"kind": b.kind, "detail": b.detail} for b in value.blockers]
        legitimacy_status = value.legitimacy.status  # top-level is authoritative on both tracks
        work_auth = getattr(value, "work_auth_signal", None)
        requires_sponsorship = bool(getattr(profile, "requires_sponsorship", False))

        post = scoring.post_process(
            track=opp.track,
            dimension_scores=dim_scores,
            holistic=value.holistic,
            model_verdict=value.verdict,
            blockers=blockers,
            legitimacy_status=legitimacy_status,
            work_auth_signal=work_auth,
            requires_sponsorship=requires_sponsorship,
            settings=settings,
        )
        if post.verdict_disagreement:
            log.info(
                "verdict_disagreement",
                opportunity_id=opp.id,
                system=post.verdict,
                model=post.verdict_model,
            )

        session.add(
            Evaluation(
                opportunity_id=opp.id,
                tier=2,
                track=opp.track,
                content_version=opp.version,
                model=settings.llm.tiers[spec.tier].model,
                prompt_version=spec.version,
                verdict=post.verdict,
                verdict_model=post.verdict_model,
                score=post.displayed_score,
                weighted_score=post.weighted,
                needs_review=post.needs_review,
                forced_by=post.forced_by,
                dimensions={
                    k: {
                        "score": getattr(value.dimensions, k).score,
                        "why": getattr(value.dimensions, k).why,
                    }
                    for k in dim_scores
                },
                blocks=value.blocks.model_dump(),
                blockers=post.blockers,
                legitimacy=value.legitimacy.model_dump(),
                few_shot_ids=few_shot_ids,
                raw_response=result.raw,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
            )
        )
        if opp.track == "consulting":
            _writeback_org_signals(org, value, opp)

        if not (rescore and opp.stage == "t3_done"):  # a score-again keeps the Tier 3 stage
            opp.stage = "t2_done"
        opp.stage_reason = (
            f"t2 {post.verdict} (score {post.displayed_score:.2f}, model {post.verdict_model})"
        )
        await session.commit()
        return HandlerOutcome(
            "t2_done",
            2,
            verdict=post.verdict,
            needs_review=post.needs_review,
            forced=bool(post.forced_by),
        )
