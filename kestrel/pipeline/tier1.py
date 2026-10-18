"""Tier 1 — fast triage (SPEC.md 6.2). P1 for employment, P1C for consulting. Small model, keep/kill
with confidence. Two non-negotiables (hard rule 4, uncertainty escalates):

  - Confidence below settings.pipeline.tier1_min_confidence is FORCED to "keep", and the evaluation
    records that the escalation was forced (forced_by).
  - A quarantined Tier 1 item is PROMOTED to Tier 2, never dropped.

The handler owns its session and commits the Evaluation, the stage change, and the next tier's task
together, so a crash between commit and task-completion resumes cleanly (the re-leased task sees the
advanced stage and is completed without a second model call)."""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.config import Settings
from kestrel.db.models import Evaluation, Opportunity, Org, Source
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_prompt
from kestrel.pipeline._shared import (
    HandlerOutcome,
    encode_send,
    jd_budget,
    make_quarantine_hook,
    plan_hosted_send,
    prompt_spec,
    record_refusal,
)
from kestrel.pipeline.context import build_tier1_values, render_user
from kestrel.pipeline.tasks import enqueue_once

log = structlog.get_logger()

_TIER1_HEAD_TOKENS = 1200  # SPEC.md 6.2: Tier 1 sees the first 1200 tokens of the description
_VERSION = {"employment": "triage/v1", "consulting": "triage_consulting/v1"}


async def handle_t1(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: LLMClient,
    settings: Settings,
    *,
    opportunity_id: int,
    profiles: dict,
    model_override: dict | None = None,
    rescore: bool = False,
    requested_at: str | None = None,
    tier0_override: bool = False,
    t2_model_override: dict | None = None,
) -> HandlerOutcome:
    """`model_override` is set on a score-again task run on another model this time only
    (D-UI-50): `settings` already carry it for Tier 1, and the Tier 2 task enqueued here carries
    it on so the role's full read runs on the same model. Such a task skips the reranker gate:
    the operator asked for that model's reading, not the reranker's.

    `t2_model_override` is a run's one-run model (Run now, Scan now; D-UI-72 amended): Tier 1
    runs on the configured Tier 1 model as usual (reranker gate included) and only the Tier 2
    task enqueued here carries the pick.

    `rescore` marks a score-again task for a role Tier 1 set aside: score again leaves the stage
    at `t1_reject` until this task runs (a task that dies never strands the role in a waiting
    stage), so such a task runs from `t1_reject` too. A Tier 1 evaluation made since
    `requested_at` means a re-leased task already did the work (hard rule 5), so it skips.

    `tier0_override` is Score this role anyway (D-UI-74): the operator asked for a role a Tier 0
    rule set aside, so with `rescore` such a task runs from `t0_reject` the same way, skips the
    reranker gate (the operator asked for the model's reading), and records the override in the
    same commit as its Tier 1 result (kestrel.services.tier0_override)."""
    # A score-again (`rescore`) hands Tier 2 the same flag. A run that picked its own model
    # (`t2_model_override`, D-UI-72) ran Tier 1 on the configured model and hands on only the
    # override, so its Tier 2 task is an ordinary first score on the pick.
    if model_override:
        t2_extra: dict | None = {"model_override": model_override}
    elif t2_model_override:
        t2_extra = {"model_override": t2_model_override}
    else:
        t2_extra = None
    if rescore:
        # The Tier 2 step of an operator's Score again is requested too (finding 10).
        #
        # `rescore` goes HERE, not inside the model_override branch above (review). It used
        # to be set there only, so a plain Score again -- no model override, the common case --
        # enqueued a Tier 2 task carrying `requested` and `requested_at` but NOT `rescore`. The
        # worker forwards the timestamp only `if payload.get("rescore")` and tier2.py runs its
        # replay guard only `if requested_at:`, so BOTH gates failed and the guard never ran: a
        # crash between the evaluation commit and the task completion scored the role twice. The
        # commit that introduced `requested_at` therefore fixed only the override case and left
        # the common path inert. The comment above already claimed "a score-again hands Tier 2
        # the same flag"; now it does.
        t2_extra = {**(t2_extra or {}), "requested": True, "rescore": True}
        # `requested_at` is the other half: "an evaluation made since then means a re-leased task
        # already did the work, so skip" (tier2.py). Both must be present for the guard to run.
        if requested_at:
            t2_extra["requested_at"] = requested_at
    async with sessionmaker() as session:
        opp = await session.get(Opportunity, opportunity_id)
        if opp is None:
            return HandlerOutcome("skip", 1, error="opportunity missing")
        if rescore and (
            opp.stage == "t1_reject" or (opp.stage == "t0_reject" and tier0_override)
        ):
            if requested_at:
                done_since = (
                    await session.execute(
                        select(func.count())
                        .select_from(Evaluation)
                        .where(
                            Evaluation.opportunity_id == opp.id,
                            Evaluation.tier == 1,
                            Evaluation.created_at >= requested_at,
                        )
                    )
                ).scalar_one()
                if done_since:
                    return HandlerOutcome("skip", 1)
        elif opp.stage != "t0_pass":  # already tiered (idempotent resume) or not ready
            return HandlerOutcome("skip", 1)
        if tier0_override and rescore and opp.stage == "t0_reject":
            # The operator's Score this role anyway is recorded HERE, in the session whose commit
            # also moves the stage and stores this Tier 1 result (D-UI-74 amended). A task that
            # errors (no commit) or dies dead never records it, so the role never says "You
            # asked to score this" for a score that did not happen, and Tier 0 keeps its rule.
            from kestrel.services import tier0_override as t0o

            await t0o.record(session, opp.id, opp.stage_reason)

        org = await session.get(Org, opp.org_id) if opp.org_id else None
        source = await session.get(Source, opp.source_id) if opp.source_id else None

        # A promoted reranker replaces the LLM here (SPEC-MODULES 5.4). A confident keep/kill
        # returns immediately; an uncertain-band item falls through to the LLM below.
        from kestrel.pipeline.reranker_gate import try_reranker_tier1

        gated = None
        if not model_override and not tier0_override:
            gated = await try_reranker_tier1(
                session, settings, opp, org, source, t2_extra=t2_extra
            )
        if gated is not None:
            return gated

        profile = profiles.get(opp.track)
        spec = prompt_spec(_VERSION[opp.track])
        # Hosted mode (SPEC.md 6.4): a metered tier sees a smaller slice, the confidential
        # employer is redacted before the send, and a missing redaction map refuses the send.
        plan = await plan_hosted_send(session, settings, spec.tier)

        values = build_tier1_values(
            opp=opp,
            org=org,
            profile=profile,
            source_name=source.name if source else "",
            n_tokens=jd_budget(settings, spec.tier, _TIER1_HEAD_TOKENS),
        )
        trigger_event = bool(opp.trigger_data or opp.event_date)
        posted = bool(opp.description_md)
        user = render_user(
            spec.user, values, trigger_event=trigger_event, posted=posted or not trigger_event
        )
        messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]

        if plan.refuse:
            # Hard rule 4: a refused hosted send keeps the item. Promote to Tier 2 exactly like a
            # quarantined Tier 1 call; nothing was transmitted.
            model_name = settings.llm.tiers[spec.tier].model
            record_refusal(
                session, tier=1, opportunity_id=opp.id, model=model_name,
                reason=plan.refuse, messages=messages,
            )
            session.add(
                Evaluation(
                    opportunity_id=opp.id, tier=1, track=opp.track,
                    content_version=opp.version, model=model_name,
                    prompt_version=spec.version, verdict="keep",
                    forced_by="t1_hosted_refused",
                )
            )
            opp.stage = "t1_pass"
            await enqueue_once(
                session, "t2", opp.id, priority=int(opp.pre_score * 100), extra=t2_extra
            )
            await session.commit()
            return HandlerOutcome(
                "promoted", 1, verdict="keep", forced=True, quarantined=True, error=plan.refuse
            )
        messages, book = encode_send(messages, plan.mapping)

        hook = make_quarantine_hook(session, tier=1, opportunity_id=opp.id)
        result = await run_prompt(client, spec, messages, on_quarantine=hook,
                                  reply_filter=book)

        if not result.ok and not result.quarantined:
            # Transport/request error, not a structural failure: let the worker retry the task.
            return HandlerOutcome("error", 1, error=result.error)

        ev = Evaluation(
            opportunity_id=opp.id,
            tier=1,
            track=opp.track,
            content_version=opp.version,
            model=settings.llm.tiers[spec.tier].model,
            prompt_version=spec.version,
            raw_response=result.raw,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
        )

        if result.quarantined or result.value is None:
            # Hard rule 4: promote, never drop. The quarantine row was added by the hook.
            opp.stage = "t1_pass"
            ev.verdict = "keep"
            ev.forced_by = "t1_quarantine_promoted"
            session.add(ev)
            await enqueue_once(
                session, "t2", opp.id, priority=int(opp.pre_score * 100), extra=t2_extra
            )
            await session.commit()
            return HandlerOutcome("promoted", 1, verdict="keep", forced=True, quarantined=True)

        value = result.value
        model_verdict = value.verdict
        forced = value.confidence < settings.pipeline.tier1_min_confidence
        verdict = "keep" if forced else model_verdict
        ev.verdict = verdict
        ev.verdict_model = model_verdict
        ev.forced_by = "low_confidence_forced_keep" if forced else None
        ev.blocks = {
            "reason": value.reason,
            "signals": list(value.signals),
            "confidence": value.confidence,
        }
        session.add(ev)

        if verdict == "keep":
            opp.stage = "t1_pass"
            await enqueue_once(
                session, "t2", opp.id, priority=int(opp.pre_score * 100), extra=t2_extra
            )
            action = "t1_pass"
        else:
            opp.stage = "t1_reject"
            opp.stage_reason = f"t1 kill: {value.reason}"
            action = "t1_reject"
        await session.commit()
        return HandlerOutcome(action, 1, verdict=verdict, forced=forced)
