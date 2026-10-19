"""Tier 0 — deterministic rules over config/filters.yml, per track (SPEC.md 6.1). No LLM.

For one opportunity: the first hard blocker whose `when:` is true rejects it, recording which rule
fired; if none fire, the soft-signal weights sum to a `pre_score`. Survivors sort by `pre_score`
descending so the best opportunities get expensive model time first. Bias is toward false positives
throughout (hard rule 4): an unknown title, a missing date, or an unparseable location passes.

The batch sweep advances `stage` new -> t0_pass / t0_reject, commits per opportunity (resumable),
and enqueues one `t1` task per survivor. The expression evaluator and its whitelist live in
`kestrel.rules.helpers`; the config-load guard (`check_filters`) that rejects a dead-on-arrival
filters.yml runs at preflight, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.models import Opportunity, Org, Source
from kestrel.ingest import board_discovery, smart_filter
from kestrel.pipeline import lane
from kestrel.pipeline.profile import load_profile
from kestrel.pipeline.tasks import enqueue_once
from kestrel.rules import helpers
from kestrel.services import run_model, tier0_override
from kestrel.services.run_model import T2_ONLY_KEY

log = structlog.get_logger()


@dataclass
class Tier0Result:
    passed: bool
    pre_score: float = 0.0
    fired_rule: str | None = None
    reason: str | None = None
    soft_hits: list[str] = field(default_factory=list)
    geo_uncertain: bool = False


def evaluate_tier0(
    *,
    opp: Any,
    org: Any,
    profile: Any,
    source: Any,
    settings: Settings,
    filters: dict,
    track: str,
    skip_blockers: bool = False,
) -> Tier0Result:
    """Evaluate one opportunity against a track's rules. The evaluator is built per opportunity
    (its `names` bind opp/org/profile/source); helper functions are null-safe so no rule raises.
    `skip_blockers` is a role the operator chose to score anyway (D-UI-74): no hard blocker
    rejects it, and its soft signals still set its pre_score."""
    groups = filters.get(track) or {}
    ev = helpers.build_evaluator(
        opp=opp, org=org, profile=profile, source=source, settings=settings
    )

    for rule in ([] if skip_blockers else groups.get("hard_blockers", []) or []):
        try:
            if ev.eval(rule["when"]):
                return Tier0Result(
                    passed=False, fired_rule=rule.get("id"), reason=rule.get("reason")
                )
        except Exception as exc:  # a null-safe helper should never raise; keep, don't reject
            log.warning("tier0_rule_error", rule=rule.get("id"), track=track, error=str(exc))

    pre_score = 0.0
    hits: list[str] = []
    for rule in groups.get("soft_signals", []) or []:
        try:
            if ev.eval(rule["when"]):
                pre_score += float(rule.get("weight", 0.0))
                hits.append(rule.get("id", "?"))
        except Exception as exc:
            log.warning("tier0_soft_error", rule=rule.get("id"), track=track, error=str(exc))

    geo_rules = getattr(profile, "geo_rules", None) if profile is not None else None
    _, geo_uncertain = helpers.geo_evaluate(opp, geo_rules)
    return Tier0Result(
        passed=True,
        pre_score=round(pre_score, 4),
        soft_hits=hits,
        geo_uncertain=geo_uncertain,
    )


def _profile_cache() -> dict[str, SimpleNamespace | None]:
    return {t: load_profile(t) for t in ("employment", "consulting")}


async def run_tier0_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    track: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Sweep stage='new' opportunities through Tier 0, committing each one's stage change and (for
    survivors) its t1 task in a single transaction. Idempotent: only 'new' rows are considered, so a
    restart never re-runs an already-tiered opportunity."""
    filters = helpers.load_filters(str(paths.config_file("filters.yml")))
    profiles = _profile_cache()
    counts = {"seen": 0, "pass": 0, "reject": 0, "geo_uncertain": 0, "lane": 0}
    filter_ctx: dict[str, smart_filter.FilterContext] = {}  # per run, never global

    async with sessionmaker() as session:
        # Archived items are out of the active pipeline, which is exactly what the Archive action
        # promises the operator. Without this they keep flowing through Tier 0 and on into the
        # tiers, burning LLM calls on work the operator has explicitly set aside.
        stmt = select(Opportunity).where(
            Opportunity.stage == "new", Opportunity.archived_at.is_(None)
        )
        if track:
            stmt = stmt.where(Opportunity.track == track)
        stmt = stmt.order_by(Opportunity.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        opp_ids = [o.id for o in (await session.execute(stmt)).scalars().all()]
        # Roles the operator chose to score despite a rule (D-UI-74): never set aside again.
        anyway = await tier0_override.overridden_ids(session) if opp_ids else set()

    for opp_id in opp_ids:
        async with sessionmaker() as session:
            opp = await session.get(Opportunity, opp_id)
            if opp is None or opp.stage != "new":
                continue
            org = await session.get(Org, opp.org_id) if opp.org_id else None
            source = await session.get(Source, opp.source_id) if opp.source_id else None
            profile = profiles.get(opp.track)
            result = evaluate_tier0(
                opp=opp,
                org=org,
                profile=profile,
                source=source,
                settings=settings,
                filters=filters,
                track=opp.track,
                skip_blockers=opp.id in anyway,
            )
            counts["seen"] += 1
            # Consumed on every first pass, pass or reject, in this commit (run_model.take).
            chosen = await run_model.take(session, opp.id)
            if result.passed:
                opp.stage = "t0_pass"
                opp.pre_score = result.pre_score
                reason = f"soft: {', '.join(result.soft_hits) or 'none'}"
                if result.geo_uncertain:
                    reason += "; geo_uncertain"
                    counts["geo_uncertain"] += 1
                if opp.id in anyway:
                    reason += "; tier0_override"
                opp.stage_reason = reason
                # A role the current run stored on a one-run model (D-UI-72) carries the pick on
                # its Tier 1 task for Tier 2 only; Tier 1 itself runs on the configured model.
                # Any later pass (no marker left) uses the configured models.
                extra: dict = {T2_ONLY_KEY: chosen} if chosen else {}
                priority = int(result.pre_score * 100)
                # A role from a company board found in a search, which the smart filter does not
                # keep, waits in the background lane (D-SRC-8): stored, scored when idle, never
                # rejected. A role the operator chose to score anyway is never held back.
                if (
                    source is not None
                    and source.origin == board_discovery.DISCOVERED
                    and opp.id not in anyway
                ):
                    if opp.track not in filter_ctx:
                        filter_ctx[opp.track] = await smart_filter.build_context(
                            session, opp.track, profile
                        )
                    if not smart_filter.keep(opp.title, filter_ctx[opp.track]):
                        extra.update(lane.lane_extra(result.pre_score))
                        priority = lane.LANE_PRIORITY
                        opp.stage_reason += f"; {lane.LANE_REASON}"
                        counts["lane"] += 1
                await enqueue_once(
                    session, "t1", opp.id, priority=priority, extra=extra or None,
                )
                counts["pass"] += 1
            else:
                opp.stage = "t0_reject"
                opp.pre_score = 0.0
                opp.stage_reason = f"blocker:{result.fired_rule}: {result.reason}"
                counts["reject"] += 1
            await session.commit()

    return counts
