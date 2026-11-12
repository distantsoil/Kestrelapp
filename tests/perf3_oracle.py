# ruff: noqa
"""The implementations D-PERF-3 replaced, kept verbatim as the oracle for the parity tests: the
Pipeline list (whole rows, one query per group), the Analytics loader (every opportunity as an ORM
row) and the funnel (the same). They read the module-level helpers of the code under test."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import and_, func, or_, select

from kestrel.analytics import funnel as F
from kestrel.analytics import report as R
from kestrel.analytics.funnel import _STAGE_RANK, STEPS, Funnel, FunnelStep, _furthest_step
from kestrel.db.models import (
    Evaluation, Opportunity, Org, Outcome, Pursuit, Quarantine, Source, StatusEvent, Task,
    VerdictOverride,
)
from kestrel.web import queries as Q

globals().update({k: v for k, v in vars(Q).items() if not k.startswith("__")})


async def old_list_opportunities(session: AsyncSession, f: OppFilters) -> OppPage:
    col, desc = _SORTS.get(f.sort, _SORTS["score"])
    if f.grouped and f.sort == "score":
        # "Best fit" sorts by the one fit number a row shows (fit.Fit.score), which falls back
        # to the weighted score when the holistic one is missing.
        col = func.coalesce(Evaluation.score, Evaluation.weighted_score)
    order = col.desc() if desc else col.asc()
    page_size = max(1, min(f.page_size, 200))
    page = max(1, f.page)
    if not f.grouped:
        stmt = _base_stmt(f).order_by(order, Opportunity.id.desc())
        rows = (
            await session.execute(stmt.limit(page_size).offset((page - 1) * page_size))
        ).all()
        # total over the same filter set (count of the filtered rows)
        count_stmt = select(func.count()).select_from(_base_stmt(f).subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        return OppPage(
            rows=[
                OppRow(opp=o, ev=e, org_name=on, source_name=sn, summary=row_summary(e),
                       pursuit_id=pid, fit=row_fit(o, e))
                for (o, e, on, sn, pid) in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    # Grouped: the same joins plus the operator's latest label. The visible groups each give
    # their first `limits[group]` rows (25 by default, Show 25 more raises one group's number),
    # in `order` inside the group; Not a fit is its own section, paged alone (D-UI-62). The
    # per-group counts below are over the whole filter set.
    grp = _group_expr()
    not_fit = _G[GROUP_NOT_FIT]
    cols = (
        Opportunity, Evaluation, Org.name, Source.name, Pursuit.id, Pursuit.closed_at,
        Pursuit.status, VerdictOverride.operator_verdict, VerdictOverride.operator_score,
        # A removed role is neither scored nor failed: it sits in the unscored group unless a
        # task is scoring it, as _group_expr does (D-UI-133, issue #90).
        live_scoring_exists().label("live"),
        scoring_task_exists().label("queued"),
        _retry_removed(Opportunity.id).label("removed"),
        saved_clause().label("saved"),
    )

    def _group_stmt(key: str):
        return (
            _with_operator(_base_stmt(f, *cols))
            .where(grp == _G[key])
            .order_by(order, Opportunity.id.desc())
        )

    rows: list = []
    if f.section == "not_fit":
        stmt = _group_stmt(GROUP_NOT_FIT)
        rows = (await session.execute(stmt.limit(page_size).offset((page - 1) * page_size))).all()
    else:
        for key in GROUP_ORDER:
            if key == GROUP_NOT_FIT:
                continue
            take = max(1, int(f.limits.get(key) or GROUP_ROWS))
            rows.extend((await session.execute(_group_stmt(key).limit(take))).all())
    counts_stmt = _with_operator(
        _base_stmt(
            f, grp.label("g"), (Opportunity.stage == "t0_reject").label("rule"), func.count()
        )
    ).group_by("g", "rule")
    group_counts = dict.fromkeys(GROUP_ORDER, 0)
    rule_rejects = 0
    for g, rule, n in (await session.execute(counts_stmt)).all():
        key = GROUP_ORDER[int(g)]
        group_counts[key] += n
        if key == GROUP_NOT_FIT and rule:
            rule_rejects += n
    out = [
        OppRow(
            opp=o, ev=e, org_name=on, source_name=sn, summary=row_summary(e),
            pursuit_id=pid,
            pursuit_status=pstatus if pid is not None and closed is None else None,
            fit=row_fit(o, e, pursued=pid is not None and closed is None,
                        pursuit_status=pstatus, operator_verdict=ov, live=bool(lv),
                        removed=bool(rm), queued=bool(qd)),
            mine=fit_word_for_verdict(ov, os_),
            removed=bool(rm), saved=bool(sv),
        )
        for (o, e, on, sn, pid, closed, pstatus, ov, os_, lv, qd, rm, sv) in rows
    ]
    await scoring_states(session, out)
    await attach_rescoring(session, out)
    await attach_twins(session, out)
    failed_ids: list[int] = []
    if group_counts.get(GROUP_FAILED):
        failed_ids = [
            int(i)
            for i in (
                await session.execute(
                    _with_operator(_base_stmt(f, Opportunity.id))
                    .where(failed_scoring_clause())
                    .order_by(Opportunity.id.desc())
                    .limit(_FAILED_IDS_CAP)
                )
            ).scalars()
        ]
    from kestrel.web.fit import rule_word

    by_rule: dict[str, int] = {}
    if rule_rejects:
        reasons = (
            await session.execute(
                _with_operator(
                    _base_stmt(f, Opportunity.stage_reason, func.count())
                ).where(grp == not_fit, Opportunity.stage == "t0_reject")
                .group_by(Opportunity.stage_reason)
            )
        ).all()
        for reason, n in reasons:
            word = rule_word([reason] if reason else [])
            by_rule[word] = by_rule.get(word, 0) + int(n)
    paged = (
        group_counts[GROUP_NOT_FIT] if f.section == "not_fit"
        else sum(n for k, n in group_counts.items() if k != GROUP_NOT_FIT)
    )
    return OppPage(
        rows=out,
        total=paged,
        page=page,
        page_size=page_size,
        group_counts=group_counts,
        rule_rejects=rule_rejects,
        not_fit_rules=sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0])),
        failed_ids=failed_ids,
    )


async def old_load(session: AsyncSession, track: str, now: datetime) -> _Data:
    opps = [
        (o, org, src)
        for o, org, src in (
            await session.execute(
                select(Opportunity, Org, Source)
                .join(Org, Org.id == Opportunity.org_id, isouter=True)
                .join(Source, Source.id == Opportunity.source_id, isouter=True)
                .where(Opportunity.track == track)
            )
        ).all()
    ]
    read_ids = set(
        (
            await session.execute(
                select(Evaluation.opportunity_id)
                .where(Evaluation.tier == 2, Evaluation.track == track)
                .distinct()
            )
        ).scalars()
    )
    scores: dict[int, float] = {}
    for opp_id, score in (
        await session.execute(
            select(Evaluation.opportunity_id, Evaluation.score)
            .where(Evaluation.tier == 2, Evaluation.track == track)
            .order_by(Evaluation.id)
        )
    ).all():
        if score is not None:
            scores[opp_id] = score  # the latest evaluation wins
    first_event = {
        pid: R._dt(at)
        for pid, at in (
            await session.execute(
                select(StatusEvent.pursuit_id, func.min(StatusEvent.occurred_at)).group_by(
                    StatusEvent.pursuit_id
                )
            )
        ).all()
    }
    opp_by_id = {o.id: (o, org, src) for o, org, src in opps}
    outcomes: dict[int, Outcome] = {}
    for oc in (await session.execute(select(Outcome).order_by(Outcome.id))).scalars():
        outcomes[oc.pursuit_id] = oc  # the latest outcome per pursuit
    pursuits: list[_P] = []
    for p in (await session.execute(select(Pursuit).where(Pursuit.track == track))).scalars():
        found = opp_by_id.get(p.opportunity_id)
        if found is None:
            continue
        opp, org, src = found
        oc = outcomes.get(p.id)
        responded = oc is not None and oc.days_to_first_response is not None
        reached = R._furthest_step(p.status, oc.furthest_stage if oc else None, responded, track)
        sent_at = R._dt(p.applied_at)
        shortlisted_at = first_event.get(p.id)
        when = (
            sent_at or shortlisted_at or R._dt(p.last_activity_at) or R._dt(opp.first_seen_at) or now
        )
        pursuits.append(
            R._P(
                id=p.id, opp=opp, org=org, src=src, status=p.status, when=when, sent_at=sent_at,
                shortlisted_at=shortlisted_at, reached=reached, replied=reached >= R._REPLY,
                reply_days=oc.days_to_first_response if oc else None,
                score=scores.get(opp.id),
            )
        )
    return R._Data(track=track, now=now, opps=opps, read_ids=read_ids, pursuits=pursuits)


async def old_funnel(session: AsyncSession, track: str) -> Funnel:
    """Compute the funnel for one track. Opportunity steps come from stage/evaluation; pursuit steps
    from the furthest step each pursuit reached. Each conversion is count / previous count.

    The pursuit steps (shortlisted onward) are sourced independently of the opportunity steps, so a
    manually-shortlisted pursuit (one with no tier-2 evaluation) can make shortlisted exceed
    tier2_evaluated, giving a conversion above 1.0. That is real and left visible, not clamped: it
    means items entered a later step without being recorded at the earlier one."""
    opps = (
        (await session.execute(select(Opportunity).where(Opportunity.track == track)))
        .scalars()
        .all()
    )
    counts = [0] * len(STEPS)
    counts[0] = len(opps)
    for o in opps:
        rank = _STAGE_RANK.get(o.stage or "new", 0)
        if rank >= 1:
            counts[1] += 1
        if rank >= 2:
            counts[2] += 1

    counts[3] = (
        await session.execute(
            select(func.count(func.distinct(Evaluation.opportunity_id))).where(
                Evaluation.tier == 2, Evaluation.track == track
            )
        )
    ).scalar_one()

    pursuits = (
        (
            await session.execute(
                select(Pursuit, Outcome)
                .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
                .outerjoin(Outcome, Outcome.pursuit_id == Pursuit.id)
                .where(Opportunity.track == track)
            )
        )
        .all()
    )
    for pursuit, outcome in pursuits:
        responded = outcome is not None and outcome.days_to_first_response is not None
        furthest_stage = outcome.furthest_stage if outcome is not None else None
        reached = _furthest_step(pursuit.status, furthest_stage, responded, track)
        for step in range(4, reached + 1):
            counts[step] += 1

    steps: list[FunnelStep] = []
    for i, name in enumerate(STEPS):
        prev = counts[i - 1] if i > 0 else counts[0]
        conv = (counts[i] / prev) if (i > 0 and prev > 0) else (1.0 if i == 0 else None)
        steps.append(FunnelStep(name=name, count=counts[i],
                                sample_size=(prev if i > 0 else counts[0]), conversion=conv))
    return Funnel(track=track, steps=steps)



def old_live_scoring_exists():
    """EXISTS an in-flight task that would score this Opportunity again (correlated to the
    enclosing statement's Opportunity). The line between Being scored and Scoring failed
    (D-UI-56), shared by _group_expr, the grouped list and scoring_failed_count. A task the
    track switch holds is not in flight, so it never hides a failure (D-UI-70)."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    return (
        select(Task.id)
        .where(Task.type.in_(_RESCORING_TASKS), _in_flight_now(), oid == Opportunity.id)
        .correlate(Opportunity)
        .exists()
    )


def old_scoring_task_exists():
    """EXISTS any pending or leased task that would score this Opportunity, held or not (correlated
    to the enclosing statement's Opportunity). The one "a task is queued" test for the unscored
    group (issue #90): _group_expr, the grouped list, the panel and queries.hidden_unscored all
    use it, so a Score anyway task a switched-off track holds keeps the role in Being scored,
    reading Paused, instead of leaving it among the roles nothing will score."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    return (
        select(Task.id)
        .where(Task.type.in_(_RESCORING_TASKS), Task.status.in_(("pending", "leased")),
               oid == Opportunity.id)
        .correlate(Opportunity)
        .exists()
    )


def old_quarantine_queued_clause():
    """A Tier 1 or Tier 2 failure whose role has an in-flight task of a tier that would resolve
    it (D-UI-70): Being scored again, not failed. A held task, a Tier 3 or research row, or an
    unrelated task never makes a row queued."""
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    resolving = or_(
        *(and_(Quarantine.tier == tier, Task.type.in_(types))
          for tier, types in _RESOLVING_TASKS.items())
    )
    return and_(
        Quarantine.tier.in_(tuple(_RESOLVING_TASKS)),
        select(Task.id)
        .where(resolving, _in_flight_now(), oid == Quarantine.opportunity_id)
        .correlate(Quarantine)
        .exists(),
    )


async def old_scoring_states(session: AsyncSession, rows: list[OppRow]) -> None:
    """Fill `scoring` on the unscored rows in a few statements. Hard rule 4: an unscored row
    always says where it is and why, never a blank or a dash.

    - "failed" (with `failed_reason`): parked in Scoring failed.
    - "running": one of its scoring tasks is leased.
    - "held": its track is switched off, so its task waits unclaimed (D-UI-68).
    - "background": its task sits in the background lane (a company-board role the smart filter
      did not keep, D-SRC-8) and is not claimed until the lane releases it; on an online model it
      waits for local scoring settings (D-SRC-13).
    - "queued": a task is pending and the worker will claim it.
    - "stuck": the startup orphan sweep already re-queued it once and nothing is queued
      (pipeline.orphans, D-UI-56).
    - "not_queued": no task yet (it awaits its first pass through the pipeline).

    A row in the unscored group (nothing scores it unless the operator acts) reads "empty" (no
    description), "hidden" (hidden as applied) or "removed" (off Needs another try)."""
    failed = [r for r in rows if r.fit and r.fit.group == GROUP_FAILED]
    if failed:
        reasons = await failure_reasons(session, [r.opp.id for r in failed])
        for r in failed:
            r.scoring = "failed"
            r.failed_reason = reasons.get(r.opp.id) or failure_words(None)
    todo = [r for r in rows if r.fit and r.fit.word == "new" and r.fit.group != GROUP_FAILED]
    if not todo:
        return
    from kestrel.pipeline.orphans import stuck_ids
    from kestrel.queue.queue import requested_by_operator
    from kestrel.services.tracks import switched_off_tracks

    stuck = await stuck_ids(session)
    hidden = await hidden_unscored(session, [r.opp for r in todo])
    off = set(switched_off_tracks())
    track_of = {r.opp.id: r.opp.track for r in todo}
    oid = func.json_extract(Task.payload, "$.opportunity_id")
    lane_flag = func.coalesce(func.json_extract(Task.payload, "$.lane"), 0)
    live = (
        await session.execute(
            select(oid, Task.status, lane_flag, requested_by_operator()).where(
                Task.type.in_(_SCORING_TASKS),
                Task.status.in_(("pending", "leased")),
                oid.in_([r.opp.id for r in todo]),
            )
        )
    ).all()
    # Per role, the most explanatory state among its live tasks.
    rank = {"running": 0, "held": 1, "background": 2, "queued": 3}
    state: dict[int, str] = {}
    for o, status, lane_on, asked in live:
        if o is None:
            continue
        o = int(o)
        if status == "leased":
            this = "running"
        elif track_of.get(o) in off and not asked:
            this = "held"
        elif int(lane_on or 0) == 1 and not asked:
            this = "background"
        else:
            this = "queued"
        if o not in state or rank[this] < rank[state[o]]:
            state[o] = this
    for r in todo:
        if r.fit.group == GROUP_UNSCORED:
            # Nothing will score it unless the operator acts (issue #90): the reason leads, and
            # a task that only refreshes a vector (a re-post's embed) never changes it.
            r.scoring, r.hidden_why = _unscored_reason(r)
            if r.scoring == "empty" and r.opp.id in hidden:
                r.hidden_why = hidden[r.opp.id]  # says so plainly after a requested score
            continue
        if r.opp.id in hidden:
            # Hidden as applied and nothing queued to score it (D-UI-225); checked first because
            # a re-post's embed task is live but never leads to a score.
            r.scoring = "hidden"
            r.hidden_why = hidden[r.opp.id]
            continue
        # A live task wins over a parked stage: a quarantined role queued to score again stays
        # quarantined until its task runs (queue.rescore), and is queued, not failed.
        if r.opp.id in state:
            r.scoring = state[r.opp.id]
        elif r.removed:
            # Taken off Needs another try (D-UI-133): not scored, and not failed either.
            r.scoring = "removed"
        elif r.opp.id in stuck:
            r.scoring = "stuck"
        else:
            r.scoring = "not_queued"


# The four task-by-role statements as they read before 0030 (payload JSON, no index).
OLD_PATCH = {
    "live_scoring_exists": old_live_scoring_exists,
    "scoring_task_exists": old_scoring_task_exists,
    "quarantine_queued_clause": old_quarantine_queued_clause,
    "scoring_states": old_scoring_states,
}
