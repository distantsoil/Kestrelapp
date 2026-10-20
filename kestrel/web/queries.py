"""Read/write helpers behind the review UI and the REST API (both consume these, so the UI is not a
privileged path). Server-side filtering, sorting, and pagination over the pipeline, plus the
override write that is this phase's primary label-capture surface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from kestrel.db.base import iso, utcnow_iso
from kestrel.db.enums import APPLIED_OR_LATER
from kestrel.db.models import (
    Contact,
    DocMaster,
    DocRender,
    Evaluation,
    FrontierTransmission,
    Label,
    Opportunity,
    Org,
    OrgResearch,
    Pursuit,
    Quarantine,
    Run,
    ScrapeRun,
    Source,
    StatusEvent,
    Task,
    VerdictOverride,
)
from kestrel.services import run_model
from kestrel.web.fit import (
    GROUP_APPLIED,
    GROUP_FAILED,
    GROUP_MAYBE,
    GROUP_NEW,
    GROUP_NOT_FIT,
    GROUP_WORTH,
    Fit,
    failure_words,
    fit_for,
    fit_word_for_verdict,
    operator_group,
)

_SORTS = {
    "score": (Evaluation.score, True),
    "date": (Opportunity.date_posted, True),
    "first_seen": (Opportunity.first_seen_at, True),
    "pre_score": (Opportunity.pre_score, True),
    "ghost": (Opportunity.ghost_score, True),
}


@dataclass
class OppFilters:
    track: str | None = None  # employment | consulting | None(both)
    stage: str | None = None
    verdict: str | None = None
    score_min: float | None = None
    score_max: float | None = None
    org: str | None = None
    source: str | None = None
    needs_review: bool = False
    ghost_min: float | None = None
    q: str | None = None
    sort: str = "score"
    page: int = 1
    page_size: int = 50
    # Hidden (archived_at set) opportunities are out of the list by default, which is what the Hide
    # action promises. The escape hatch matters: hiding must hide, never destroy. `?archived=1`
    # (the REST API's parameter, unchanged since the API shipped) INCLUDES hidden rows beside the
    # active ones; `?hidden=1` (the Hidden chip) lists ONLY the hidden ones, with their reason,
    # and Unhide puts one back. Nothing here deletes a row.
    include_archived: bool = False
    hidden_only: bool = False
    # The triage chips (docs/UI_PROPOSAL.html Screen 2). Shortlisted is "has a pursuit row"; new
    # today is "first seen since 00:00 UTC". Both are additive to the older filters above.
    shortlisted: bool = False
    new_today: bool = False
    # The Pipeline page's fit groups (D-UI-45, D-UI-51): rows ordered Worth a look, Maybe, Being
    # scored, Not a fit, then by `sort` inside each group, with a count per group. The JSON API
    # keeps the flat list (grouped stays False there).
    grouped: bool = False
    # Grouped list only (D-UI-62): "main" pages the visible groups and leaves Not a fit out;
    # "not_fit" pages Not a fit alone, for its own section below the others.
    section: str = "main"


@dataclass
class OppRow:
    opp: Opportunity
    ev: Evaluation | None
    org_name: str | None
    source_name: str | None
    # The one line of the model's own summary (Tier 2 block A), already cut to its first
    # sentence; None when the opportunity has no evaluation. Derived from the Evaluation row the
    # list query already joins, so it costs no extra statement.
    summary: str | None = None
    pursuit_id: int | None = None
    # The pursuit's status (grouped list only), so a row can say "applied" instead of "on my
    # shortlist" (D-UI-51 amended).
    pursuit_status: str | None = None
    # The operator's word for this row (kestrel.web.fit), from fit_for over the latest
    # evaluation; `mine` is the fit word of the operator's latest label, if any.
    fit: Fit | None = None
    mine: str | None = None
    # Grouped list, unscored rows only: where the role is in the scoring queue (D-UI-51, D-UI-81):
    # "running" (a task is leased), "queued" (a task is pending), "background" (its task waits in
    # the background lane), "held" (its track is switched off), "not_queued" (no task yet),
    # "failed" (parked in Scoring failed) or "stuck" (re-queued once at startup and still
    # unscored with nothing queued, D-UI-56). None for a scored row.
    scoring: str | None = None
    # Scoring failed rows only: the latest quarantine error in plain words (fit.failure_words).
    failed_reason: str | None = None


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def first_sentence(text: str | None, *, limit: int = 220) -> str | None:
    """The first sentence of a block of prose, for the one summary line a triage row carries.
    Splits on sentence punctuation followed by a capital, so "acquired carrier mid-integration;
    reports to the CFO." stays whole and "Inc. is" does not cut mid-name. Clipped at `limit`."""
    if not text:
        return None
    flat = " ".join(str(text).split())
    if not flat:
        return None
    head = _SENTENCE_END.split(flat, maxsplit=1)[0]
    if len(head) > limit:
        head = head[: limit - 1].rstrip() + "…"
    return head


def row_summary(ev: Evaluation | None) -> str | None:
    """The summary line for a pipeline row: the first sentence of the Tier 2 "what this actually
    is" block, or the consulting situation block. None when there is no evaluation or the
    evaluation carries neither block (a Tier 1 row, a quarantined one)."""
    if ev is None:
        return None
    from kestrel.analytics.today import summary_line

    return first_sentence(summary_line(ev.blocks))


@dataclass
class OppPage:
    rows: list[OppRow] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
    # Grouped list only: rows per fit group over the whole filter set (not just this page), and
    # how many of the Not a fit rows a Tier 0 rule rejected before any model ran.
    group_counts: dict[str, int] = field(default_factory=dict)
    rule_rejects: int = 0
    # Grouped list only: the Not a fit rows a rule set aside, by rule word over the WHOLE filter
    # set ([("location", 31), ("title", 18)]), for the section's note (D-UI-62).
    not_fit_rules: list[tuple[str, int]] = field(default_factory=list)
    # Grouped list only: every Scoring failed role under the filters (all pages, capped), for
    # the group's own Score again (D-UI-56).
    failed_ids: list[int] = field(default_factory=list)

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


def _latest_eval_subq():
    """The evaluation that speaks for each opportunity: the HIGHEST TIER, then the newest id
    (SPEC.md 4.4). Highest id alone let a Tier 1 forced-keep row written after the Tier 2 read
    (a re-screen of a changed posting) stand in for it, so the row showed no score at all."""
    ranked = select(
        Evaluation.opportunity_id,
        Evaluation.id.label("eid"),
        func.row_number()
        .over(
            partition_by=Evaluation.opportunity_id,
            order_by=(Evaluation.tier.desc(), Evaluation.id.desc()),
        )
        .label("rn"),
    ).subquery()
    return (
        select(ranked.c.opportunity_id, ranked.c.eid).where(ranked.c.rn == 1).subquery()
    )


async def latest_evaluation(session: AsyncSession, opp_id: int) -> Evaluation | None:
    """The one evaluation the pages, the API and the override label use for an opportunity:
    highest tier, then newest, the same rule as _latest_eval_subq."""
    return (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opp_id)
            .order_by(Evaluation.tier.desc(), Evaluation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _latest_override_subq():
    return (
        select(VerdictOverride.opportunity_id, func.max(VerdictOverride.id).label("vid"))
        .group_by(VerdictOverride.opportunity_id)
        .subquery()
    )


_T2_VERDICTS = ("apply", "pursue", "consider", "pass")
# Stages where scoring stopped without a score: Scoring failed, or Being scored again while a
# scoring task is live; never a model verdict.
_PARKED_STAGES = ("quarantined", "error")
# Group order on the Pipeline page, as kestrel.web.fit names them. Being scored and Scoring
# failed sit ahead of Not a fit (hard rule 4): a quarantined row or a Tier 1 keep awaiting Tier 2
# must never be pages deep behind hundreds of Tier 0 rejects. Applied follows Worth a look and
# Maybe: roles the operator already acted on, off the review queue (D-UI-51 amended). The index is
# the CASE value _group_expr returns.
GROUP_ORDER = (GROUP_WORTH, GROUP_MAYBE, GROUP_APPLIED, GROUP_NEW, GROUP_FAILED, GROUP_NOT_FIT)
_G = {key: i for i, key in enumerate(GROUP_ORDER)}
# Task types that score a role, and the stages a task can be in while it is live.
_SCORING_TASKS = ("embed", "t1", "t2", "t3", "rescore")
# The ones that can move a parked role out of Scoring failed (an embed only refreshes a vector).
_RESCORING_TASKS = ("t1", "t2", "t3", "rescore")
_FAILED_IDS_CAP = 500


def _in_flight_now():
    """queue.in_flight at this moment: claimable now (never a task the track switch holds) or
    under a live lease. The one "is something running" predicate (D-UI-68)."""
    from kestrel.queue.queue import in_flight

    return in_flight(utcnow_iso())


def live_scoring_exists():
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


def failed_scoring_clause():
    """An active role in Scoring failed: parked (quarantined or error) with no live scoring task
    (D-UI-56). _group_expr puts these first, ahead of every operator label."""
    return and_(
        func.coalesce(Opportunity.stage, "").in_(_PARKED_STAGES), ~live_scoring_exists()
    )


# Quarantine tiers that score a role (Tier 1 screen, Tier 2 review, Tier 3 read). Every other
# quarantine (research, interview, negotiation, the summary, SEC triggers) is never superseded by
# an evaluation, so it stays open until the operator deals with it.
SCORING_QUARANTINE_TIERS = (1, 2, 3)
# Evaluations a quarantine itself writes (Tier 1 promotes instead of dropping, hard rule 4): they
# record the failure and never resolve it.
_FORCED_BY_FAILURE = ("t1_quarantine_promoted", "t1_hosted_refused")
# The task types whose evaluation would resolve a Tier 1 or a Tier 2 failure (a `rescore` and a
# score-again `t2` both write Tier 2). Only these rows can read "Queued to score again".
_RESOLVING_TASKS = {1: ("t1", "t2", "t3", "rescore"), 2: ("t2", "t3", "rescore")}


def quarantine_superseded():
    """EXISTS a later successful evaluation of the same role at the same or a higher tier,
    correlated to the enclosing statement's Quarantine row (D-UI-70). Such a row is resolved:
    it stays as history (hard rule 3 keeps the raw reply) but no count or list calls it failed.
    "Later" is created_at on or after the quarantine row's, the run summary's own rule (R6)."""
    ev = aliased(Evaluation)
    return and_(
        Quarantine.opportunity_id.is_not(None),
        Quarantine.tier.in_(SCORING_QUARANTINE_TIERS),
        select(ev.id)
        .where(
            ev.opportunity_id == Quarantine.opportunity_id,
            ev.tier >= Quarantine.tier,
            ev.created_at >= Quarantine.created_at,
            or_(ev.forced_by.is_(None), ev.forced_by.not_in(_FORCED_BY_FAILURE)),
        )
        .correlate(Quarantine)
        .exists(),
    )


def open_quarantine_clause():
    """A quarantine row nothing has resolved yet (D-UI-70): every row the Failed scoring page
    lists in its main tables."""
    return ~quarantine_superseded()


def quarantine_queued_clause():
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


def _quarantine_role_hidden():
    return (
        select(Opportunity.id)
        .where(Opportunity.id == Quarantine.opportunity_id, Opportunity.archived_at.is_not(None))
        .correlate(Quarantine)
        .exists()
    )


def quarantine_needs_you_clause():
    """An open quarantine row the operator still has to act on (D-UI-70): open, its role not
    hidden, and not queued to score again (quarantine_queued_clause)."""
    return and_(open_quarantine_clause(), ~_quarantine_role_hidden(), ~quarantine_queued_clause())


@dataclass
class FailureSummary:
    """What needs the operator, counted once per role (plus each row with no role): `scoring`
    (parked in Scoring failed, or a failed screen or read), `rescore` (a failed score-again of a
    role already scored, whose earlier score stands), `other` (research, interview and the like,
    or a row with no role). `total` is the one number every surface shows (D-UI-70)."""

    scoring: int = 0
    rescore: int = 0
    other: int = 0

    @property
    def total(self) -> int:
        return self.scoring + self.rescore + self.other


async def failure_summary(session: AsyncSession, track: str | None = None) -> FailureSummary:
    """The one definition of "failed and needs you" (D-UI-70): each role with an open,
    unqueued failure on a role not hidden, plus every role parked in Scoring failed
    (failed_scoring_clause, so the Pipeline group always agrees), plus each orphan row. Today's
    Fix now and its line, the rail, the Now line, the Runs pages, the tray and the Failed scoring
    banner all read this."""
    from kestrel.pipeline._shared import parse_rescore_failure

    rank = {"scoring": 3, "rescore": 2, "other": 1}
    kinds: dict[tuple[str, int], str] = {}

    def put(key: tuple[str, int], kind: str) -> None:
        if rank[kind] > rank.get(kinds.get(key, ""), 0):
            kinds[key] = kind

    stmt = select(Quarantine.id, Quarantine.opportunity_id, Quarantine.tier, Quarantine.error)
    stmt = stmt.where(quarantine_needs_you_clause())
    if track in ("employment", "consulting"):
        stmt = stmt.join(Opportunity, Opportunity.id == Quarantine.opportunity_id).where(
            Opportunity.track == track
        )
    for qid, oid, tier, error in (await session.execute(stmt)).all():
        if oid is None:
            put(("q", int(qid)), "other")
        elif parse_rescore_failure(error)[0]:
            put(("o", int(oid)), "rescore")
        elif tier in SCORING_QUARANTINE_TIERS:
            put(("o", int(oid)), "scoring")
        else:
            put(("o", int(oid)), "other")
    parked = select(Opportunity.id).where(
        failed_scoring_clause(), Opportunity.archived_at.is_(None)
    )
    if track in ("employment", "consulting"):
        parked = parked.where(Opportunity.track == track)
    for (oid,) in (await session.execute(parked)).all():
        put(("o", int(oid)), "scoring")
    out = FailureSummary()
    for kind in kinds.values():
        setattr(out, kind, getattr(out, kind) + 1)
    return out


async def open_quarantine_count(session: AsyncSession, track: str | None = None) -> int:
    """failure_summary's total: the one "N need you" number (D-UI-70)."""
    return (await failure_summary(session, track)).total


async def open_quarantine_errors(session: AsyncSession) -> list[str | None]:
    """The stored error of each open failure that needs the operator, for Today's "why" line."""
    return list(
        (
            await session.execute(select(Quarantine.error).where(quarantine_needs_you_clause()))
        ).scalars()
    )


async def scoring_failed_count(session: AsyncSession, track: str | None = None) -> int:
    """How many active (not hidden) roles sit in the Pipeline's Scoring failed group right now
    (its header count). Every "N need you" count is failure_summary, which includes these."""
    stmt = (
        select(func.count())
        .select_from(Opportunity)
        .where(failed_scoring_clause(), Opportunity.archived_at.is_(None))
    )
    if track in ("employment", "consulting"):
        stmt = stmt.where(Opportunity.track == track)
    return int((await session.execute(stmt)).scalar_one() or 0)


def _group_expr():
    """fit_for's grouping plus the operator's decision (fit.operator_group) as SQL, so the page
    can order and count by group before paging. In this order: a parked role (quarantined or
    errored) with no live scoring task is Scoring failed (D-UI-56); a role not scored yet
    (parked but queued again, new, or waiting on a tier) is Being scored whatever anyone said, so a
    role passed and then re-scored into quarantine is never hidden in Not a fit (hard rule 4);
    then a role with an open pursuit (on the shortlist, not closed) is Applied once its status
    is applied or later and Worth a look before that; then the
    operator's latest label (apply/pursue, consider, pass); then the model (D-UI-51). The
    statement must outer-join Pursuit and the latest VerdictOverride (`_with_operator`). Must
    agree with row_fit row for row (tests/test_pipeline_view.py checks)."""
    verdict = func.coalesce(Evaluation.verdict, "")
    scored = and_(verdict.in_(_T2_VERDICTS), func.coalesce(Evaluation.tier, 0) != 1)
    stage = func.coalesce(Opportunity.stage, "")
    being_scored = or_(
        stage.in_(_PARKED_STAGES),
        and_(stage.not_in(("t0_reject", "t1_reject")), verdict != "kill", ~scored),
    )
    mine = VerdictOverride.operator_verdict
    return case(
        (failed_scoring_clause(), _G[GROUP_FAILED]),
        (being_scored, _G[GROUP_NEW]),
        (
            and_(
                Pursuit.id.is_not(None),
                Pursuit.closed_at.is_(None),
                Pursuit.status.in_(sorted(APPLIED_OR_LATER)),
            ),
            _G[GROUP_APPLIED],
        ),
        (and_(Pursuit.id.is_not(None), Pursuit.closed_at.is_(None)), _G[GROUP_WORTH]),
        (mine.in_(("apply", "pursue")), _G[GROUP_WORTH]),
        (mine == "consider", _G[GROUP_MAYBE]),
        (mine == "pass", _G[GROUP_NOT_FIT]),
        (Opportunity.stage == "t0_reject", _G[GROUP_NOT_FIT]),
        (or_(Evaluation.verdict == "kill", Opportunity.stage == "t1_reject"), _G[GROUP_NOT_FIT]),
        (and_(scored, Evaluation.needs_review.is_(True)), _G[GROUP_WORTH]),
        (and_(scored, Evaluation.verdict.in_(("apply", "pursue"))), _G[GROUP_WORTH]),
        (and_(scored, Evaluation.verdict == "consider"), _G[GROUP_MAYBE]),
        (and_(scored, Evaluation.verdict == "pass"), _G[GROUP_NOT_FIT]),
        else_=_G[GROUP_NEW],
    )


def _with_operator(stmt):
    """Outer-join the operator's latest label, which _group_expr reads (one row per
    opportunity: the newest verdict_override id)."""
    lov = _latest_override_subq()
    return stmt.join(lov, lov.c.opportunity_id == Opportunity.id, isouter=True).join(
        VerdictOverride, VerdictOverride.id == lov.c.vid, isouter=True
    )


def t0_rules(opp: Opportunity) -> str | None:
    """The Tier 0 rule text fit_for names, without tier0's "blocker:" prefix (the stored
    stage_reason is "blocker:<rule id>: <reason>")."""
    if opp.stage != "t0_reject" or not opp.stage_reason:
        return None
    return opp.stage_reason.removeprefix("blocker:")


def row_fit(
    opp: Opportunity,
    ev: Evaluation | None,
    *,
    pursued: bool = False,
    pursuit_status: str | None = None,
    operator_verdict: str | None = None,
    live: bool = False,
) -> Fit:
    """The row's fit word from the model (fit_for), in the group the operator's own decision
    puts it in when there is one (fit.operator_group, D-UI-51), except that a role not scored
    yet stays in Being scored or Scoring failed. `live` is live_scoring_exists for the row.
    _group_expr is the SQL twin."""
    fit = fit_for(ev, opp.stage, t0_rules=t0_rules(opp), live=live)
    if fit.group in (GROUP_NEW, GROUP_FAILED):  # outranks the operator's label (_group_expr)
        return fit
    group = operator_group(operator_verdict, pursued=pursued, pursuit_status=pursuit_status)
    return replace(fit, group=group) if group and group != fit.group else fit


async def scoring_states(session: AsyncSession, rows: list[OppRow]) -> None:
    """Fill `scoring` on the unscored rows in a few statements. Hard rule 4: an unscored row
    always says where it is and why, never a blank or a dash.

    - "failed" (with `failed_reason`): parked in Scoring failed.
    - "running": one of its scoring tasks is leased.
    - "held": its track is switched off, so its task waits unclaimed (D-UI-68).
    - "background": its task sits in the background lane (a company-board role the smart filter
      did not keep, D-SRC-8) and is not claimed until the lane releases it; on an online model it
      waits for Score everything now (D-SRC-8 amended).
    - "queued": a task is pending and the worker will claim it.
    - "stuck": the startup orphan sweep already re-queued it once and nothing is queued
      (pipeline.orphans, D-UI-56).
    - "not_queued": no task yet (it awaits its first pass through the pipeline)."""
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
        # A live task wins over a parked stage: a quarantined role queued to score again stays
        # quarantined until its task runs (queue.rescore), and is queued, not failed.
        if r.opp.id in state:
            r.scoring = state[r.opp.id]
        elif r.opp.id in stuck:
            r.scoring = "stuck"
        else:
            r.scoring = "not_queued"


async def failure_reasons(session: AsyncSession, opp_ids: list[int]) -> dict[int, str]:
    """The newest quarantine error per role, in plain words (fit.failure_words)."""
    if not opp_ids:
        return {}
    newest = (
        select(func.max(Quarantine.id))
        .where(Quarantine.opportunity_id.in_(opp_ids))
        .group_by(Quarantine.opportunity_id)
    )
    rows = (
        await session.execute(
            select(Quarantine.opportunity_id, Quarantine.error).where(Quarantine.id.in_(newest))
        )
    ).all()
    return {int(o): failure_words(err) for o, err in rows if o is not None}


def _apply_filters(stmt, f: OppFilters):
    if f.hidden_only:
        stmt = stmt.where(Opportunity.archived_at.is_not(None))
    elif not f.include_archived:
        stmt = stmt.where(Opportunity.archived_at.is_(None))
    if f.track in ("employment", "consulting"):
        stmt = stmt.where(Opportunity.track == f.track)
    if f.stage:
        stmt = stmt.where(Opportunity.stage == f.stage)
    if f.verdict:
        stmt = stmt.where(Evaluation.verdict == f.verdict)
    if f.score_min is not None:
        stmt = stmt.where(Evaluation.score >= f.score_min)
    if f.score_max is not None:
        stmt = stmt.where(Evaluation.score <= f.score_max)
    if f.needs_review:
        stmt = stmt.where(Evaluation.needs_review.is_(True))
    if f.ghost_min is not None:
        stmt = stmt.where(Opportunity.ghost_score >= f.ghost_min)
    if f.org:
        stmt = stmt.where(Org.name.ilike(f"%{f.org}%"))
    if f.source:
        like = f"%{f.source}%"
        stmt = stmt.where(Source.name.ilike(like) | Source.kind.ilike(like))
    if f.q:
        # Titles and org names, as the search box promises ("Search titles and orgs").
        like = f"%{f.q}%"
        stmt = stmt.where(
            Opportunity.title.ilike(like) | Org.name.ilike(like) | Opportunity.org_name.ilike(like)
        )
    if f.shortlisted:
        stmt = stmt.where(Pursuit.id.is_not(None))
    if f.new_today:
        stmt = stmt.where(Opportunity.first_seen_at >= local_day_start_iso())
    return stmt


def local_day_start_iso(now: datetime | None = None) -> str:
    """Midnight today on the operator's clock, as the UTC ISO text `first_seen_at` is stored in.
    "New today" follows the local day like every other "today" in the app (D-UI-9); a UTC cut
    reset the chip at 7pm in a US time zone and emptied it of everything seen earlier that day."""
    local = (now or datetime.now(UTC)).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return iso(start.astimezone(UTC))


def _base_stmt(f: OppFilters, *cols):
    latest = _latest_eval_subq()
    stmt = (
        select(*(cols or (Opportunity, Evaluation, Org.name, Source.name, Pursuit.id)))
        .select_from(Opportunity)
        .join(latest, latest.c.opportunity_id == Opportunity.id, isouter=True)
        .join(Evaluation, Evaluation.id == latest.c.eid, isouter=True)
        .join(Org, Org.id == Opportunity.org_id, isouter=True)
        .join(Source, Source.id == Opportunity.source_id, isouter=True)
        # pursuit.opportunity_id is unique, so this join never multiplies rows.
        .join(Pursuit, Pursuit.opportunity_id == Opportunity.id, isouter=True)
    )
    return _apply_filters(stmt, f)


async def quarantined_count(session: AsyncSession, track: str | None = None) -> int:
    """How many active opportunities sit in the quarantined stage, for the chip's count."""
    stmt = (
        select(func.count())
        .select_from(Opportunity)
        .where(Opportunity.stage == "quarantined", Opportunity.archived_at.is_(None))
    )
    if track in ("employment", "consulting"):
        stmt = stmt.where(Opportunity.track == track)
    return (await session.execute(stmt)).scalar_one()


async def any_opportunities(session: AsyncSession, track: str | None = None) -> bool:
    """Whether the pipeline has ever ingested anything (archived included), so an empty list can
    say "nothing ingested yet" rather than "nothing matches" when that is the truth."""
    stmt = select(func.count()).select_from(Opportunity)
    if track in ("employment", "consulting"):
        stmt = stmt.where(Opportunity.track == track)
    return (await session.execute(stmt)).scalar_one() > 0


async def list_opportunities(session: AsyncSession, f: OppFilters) -> OppPage:
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

    # Grouped: the same joins plus the operator's latest label, ordered by fit group first.
    # Two statements, as the flat list: the rows, and the per-group counts (whose sum is the
    # total).
    grp = _group_expr()
    not_fit = _G[GROUP_NOT_FIT]
    stmt = _with_operator(
        _base_stmt(
            f, Opportunity, Evaluation, Org.name, Source.name, Pursuit.id, Pursuit.closed_at,
            Pursuit.status, VerdictOverride.operator_verdict, VerdictOverride.operator_score,
            live_scoring_exists().label("live"),
        )
    ).order_by(grp, order, Opportunity.id.desc())
    # Not a fit leaves the grouped paging (D-UI-62): the visible groups page alone, and Not a
    # fit is its own section with its own paging, so a page is never all hidden rows.
    stmt = stmt.where(grp == not_fit) if f.section == "not_fit" else stmt.where(grp != not_fit)
    rows = (await session.execute(stmt.limit(page_size).offset((page - 1) * page_size))).all()
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
                        pursuit_status=pstatus, operator_verdict=ov, live=bool(lv)),
            mine=fit_word_for_verdict(ov, os_),
        )
        for (o, e, on, sn, pid, closed, pstatus, ov, os_, lv) in rows
    ]
    await scoring_states(session, out)
    failed_ids: list[int] = []
    if group_counts.get(GROUP_FAILED):
        failed_ids = [
            int(i)
            for i in (
                await session.execute(
                    _base_stmt(f, Opportunity.id)
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


async def get_detail(session: AsyncSession, opp_id: int) -> dict[str, Any] | None:
    opp = await session.get(Opportunity, opp_id)
    if opp is None:
        return None
    ev = await latest_evaluation(session, opp_id)
    org = await session.get(Org, opp.org_id) if opp.org_id else None
    source = await session.get(Source, opp.source_id) if opp.source_id else None
    dedup_members: list[Opportunity] = []
    if opp.dedup_group_id:
        dedup_members = list(
            (
                await session.execute(
                    select(Opportunity).where(
                        Opportunity.dedup_group_id == opp.dedup_group_id,
                        Opportunity.id != opp.id,
                    )
                )
            ).scalars()
        )
    override = (
        await session.execute(
            select(VerdictOverride)
            .where(VerdictOverride.opportunity_id == opp_id)
            .order_by(VerdictOverride.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    from kestrel.pipeline.context import comp_intel_summary
    from kestrel.web.redact import confidential, redact_json

    comp_intel = await comp_intel_summary(session, opp)  # None -> "insufficient data (n<5)"
    # A display copy of the evaluation's JSON fields with confidential-mode redaction applied.
    # The Tier 2 prose (blocks, dimension rationale, blocker detail, legitimacy) names the
    # employer routinely, and every reader of this dict (row expansion, detail, the JSON API) was
    # rendering it raw while redacting only `org`. A separate copy rather than an in-place edit,
    # so the ORM row is never mutated inside a session that might later commit.
    conf, mapping = await confidential(session)
    ev_json = {
        "blocks": redact_json(ev.blocks, mapping, enabled=conf) if ev else None,
        "dimensions": redact_json(ev.dimensions, mapping, enabled=conf) if ev else None,
        "blockers": redact_json(ev.blockers, mapping, enabled=conf) if ev else None,
        "legitimacy": redact_json(ev.legitimacy, mapping, enabled=conf) if ev else None,
    }
    return {
        "opp": opp,
        "ev": ev,
        "ev_json": ev_json,
        "org": org,
        "source": source,
        "dedup_members": dedup_members,
        "override": override,
        "comp_intel": comp_intel,
    }


def _said(verdict: str | None, score: float | None) -> str:
    """A stored verdict as the operator's word (kestrel.web.fit) for the History tab."""
    if verdict == "keep":
        return "kept for a full read"
    if verdict == "kill":
        return "screened out"
    return fit_word_for_verdict(verdict, score) or verdict or "no verdict"


async def opportunity_history(session: AsyncSession, opp_id: int) -> list[dict[str, Any]]:
    """The History tab of the opportunity detail: every evaluation by tier, every label the
    operator gave (verdict overrides and label rows), the pursuit's status changes, and every Tier
    3 transmission with what it cost. Newest first. Plain dicts, so the route can redact the free
    text (reasons and notes name the employer) with redact_json before rendering.

    Each item: {"at": iso, "kind": evaluation|label|status|transmission, "label": short heading,
    "detail": one line, "cost_usd": float|None}."""
    items: list[dict[str, Any]] = []

    evals = (
        await session.execute(
            select(Evaluation)
            .where(Evaluation.opportunity_id == opp_id)
            .order_by(Evaluation.id.desc())
        )
    ).scalars()
    for ev in evals:
        score = f" {ev.score:.2f}" if ev.score is not None else ""
        model = ev.model or "unknown model"
        prompt = f", prompt {ev.prompt_version}" if ev.prompt_version else ""
        detail = f"{_said(ev.verdict, ev.score)}{score} by {model}{prompt}"
        if ev.verdict_model and ev.verdict_model != ev.verdict:
            detail += f"; the model said {_said(ev.verdict_model, ev.score)}"
        if ev.forced_by:
            detail += f"; forced by {ev.forced_by}"
        items.append(
            {
                "at": ev.created_at or "", "kind": "evaluation",
                "label": f"Tier {ev.tier} evaluation", "detail": detail, "cost_usd": None,
            }
        )

    overrides = (
        await session.execute(
            select(VerdictOverride)
            .where(VerdictOverride.opportunity_id == opp_id)
            .order_by(VerdictOverride.id.desc())
        )
    ).scalars()
    for ov in overrides:
        agreed = ov.operator_verdict == ov.system_verdict and ov.operator_score == ov.system_score
        head = "Agreed with the system" if agreed else "Your call"
        score = f" ({ov.operator_score:.1f})" if ov.operator_score is not None else ""
        detail = f"{_said(ov.operator_verdict, ov.operator_score)}{score}"
        if ov.system_verdict and not agreed:
            sys_score = f" {ov.system_score:.2f}" if ov.system_score is not None else ""
            detail += f"; the system said {_said(ov.system_verdict, ov.system_score)}{sys_score}"
        if ov.reason:
            detail += f". {ov.reason}"
        items.append(
            {"at": ov.created_at or "", "kind": "label", "label": head, "detail": detail,
             "cost_usd": None}
        )

    labels = (
        await session.execute(
            select(Label).where(Label.opportunity_id == opp_id).order_by(Label.id.desc())
        )
    ).scalars()
    for lb in labels:
        score = f" ({lb.score:.1f})" if lb.score is not None else ""
        detail = f"{lb.verdict or 'no verdict'}{score}"
        if lb.rationale:
            detail += f". {lb.rationale}"
        items.append(
            {"at": lb.created_at or "", "kind": "label", "label": f"Label ({lb.source})",
             "detail": detail, "cost_usd": None}
        )

    pursuit = (
        await session.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
    ).scalar_one_or_none()
    if pursuit is not None:
        events = (
            await session.execute(
                select(StatusEvent)
                .where(StatusEvent.pursuit_id == pursuit.id)
                .order_by(StatusEvent.id.desc())
            )
        ).scalars()
        n_events = 0
        for e in events:
            n_events += 1
            detail = f"{e.from_status or 'start'} to {e.to_status}"
            if e.note:
                detail += f". {e.note}"
            items.append(
                {"at": e.occurred_at or "", "kind": "status", "label": "Pursuit status",
                 "detail": detail, "cost_usd": None}
            )
        if n_events == 0:
            # A pursuit created by Start pursuit has no status event yet; still show it exists.
            items.append(
                {"at": pursuit.last_activity_at or pursuit.applied_at or "", "kind": "status",
                 "label": "Pursuit started", "detail": f"status {pursuit.status}",
                 "cost_usd": None}
            )
        if pursuit.outcome:
            detail = pursuit.outcome
            if pursuit.outcome_reason:
                detail += f". {pursuit.outcome_reason}"
            items.append(
                {"at": pursuit.closed_at or "", "kind": "status", "label": "Outcome",
                 "detail": detail, "cost_usd": None}
            )

    sends = (
        await session.execute(
            select(FrontierTransmission)
            .where(FrontierTransmission.opportunity_id == opp_id)
            .order_by(FrontierTransmission.id.desc())
        )
    ).scalars()
    for t in sends:
        detail = f"{t.status} to {t.provider} ({t.model}), about {t.prompt_tokens_estimate} tokens"
        if t.prompt_tokens is not None:
            detail += f"; billed {t.prompt_tokens} in"
            if t.completion_tokens is not None:
                detail += f", {t.completion_tokens} out"
        detail += "; employer redacted" if t.confidential else "; employer not redacted"
        if t.error:
            detail += f". {t.error}"
        items.append(
            {"at": t.created_at or "", "kind": "transmission",
             "label": f"Deeper read sent to {provider_display(t.base_url, t.provider)}",
             "detail": detail, "cost_usd": t.cost_usd}
        )

    items.sort(key=lambda i: i["at"], reverse=True)
    return items


_PROVIDER_HOSTS = (
    ("openrouter", "OpenRouter"), ("anthropic", "Anthropic"), ("openai", "OpenAI"),
    ("openmodel", "OpenModel"),
)


def provider_display(base_url: str | None, backend: str | None) -> str:
    """The provider as the operator knows it (OpenRouter, Anthropic...), read from where the
    send went; the host itself for any other OpenAI-compatible service. Never the internal
    backend name ("frontier") unless nothing else is known."""
    from urllib.parse import urlsplit

    host = (urlsplit(base_url or "").hostname or "").lower()
    for needle, label in _PROVIDER_HOSTS:
        if needle in host:
            return label
    return host or (backend or "the online model")


async def opportunity_renders(session: AsyncSession, opp_id: int) -> list[dict[str, Any]]:
    """Documents rendered for this opportunity (the Documents tab and card), newest first, each
    with its master so the row can say "Executive resume" rather than a master id."""
    rows = (
        await session.execute(
            select(DocRender, DocMaster)
            .join(DocMaster, DocMaster.id == DocRender.doc_master_id, isouter=True)
            .where(DocRender.opportunity_id == opp_id)
            .order_by(DocRender.id.desc())
        )
    ).all()
    return [{"render": r, "master": m} for (r, m) in rows]


async def org_context(session: AsyncSession, org_id: int | None) -> dict[str, Any]:
    """The Organization tab: the latest research pass and the contacts on file for the org. Both
    are returned as plain JSON-shaped values so the route can redact them (the research summary
    and contact notes name the employer in free text)."""
    if org_id is None:
        return {"research": None, "contacts": []}
    research = (
        await session.execute(
            select(OrgResearch)
            .where(OrgResearch.org_id == org_id)
            .order_by(OrgResearch.researched_at.desc(), OrgResearch.id.desc())
            .limit(1)
        )
    ).scalars().first()
    contacts = (
        await session.execute(
            select(Contact).where(Contact.org_id == org_id).order_by(Contact.name)
        )
    ).scalars().all()
    research_json = None
    if research is not None:
        research_json = {
            "summary_md": research.summary_md,
            "signals": research.signals or [],
            "unknowns": research.unknowns or [],
            "angle": research.angle,
            "researched_at": research.researched_at,
            "ttl_days": research.ttl_days,
            "insufficient_sources": bool(research.insufficient_sources),
        }
    contacts_json = [
        {
            "id": c.id, "name": c.name, "title": c.title, "contact_type": c.contact_type,
            "linkedin_url": c.linkedin_url, "email": c.email, "confidence": c.confidence,
            "relationship": c.relationship,
        }
        for c in contacts
    ]
    return {"research": research_json, "contacts": contacts_json}


async def record_override(
    session: AsyncSession,
    *,
    opp_id: int,
    mode: str,  # agree | adjust | override
    operator_verdict: str | None = None,
    operator_score: float | None = None,
    reason: str | None = None,
    dimension_corrections: dict | None = None,
    commit: bool = True,
) -> VerdictOverride | None:
    """Write a verdict_override row (SPEC-INTERFACES 2.1). This is the label the learning loop
    consumes. 'agree' records the system verdict/score as the operator's; 'adjust' keeps the verdict
    but sets the operator's score; 'override' sets the operator's verdict and requires a reason.

    `commit=False` only flushes, so a caller can write the label in the same transaction as the
    change it describes (Add to my shortlist: the pursuit and its label land together or not at
    all)."""
    opp = await session.get(Opportunity, opp_id)
    if opp is None:
        return None
    ev = await latest_evaluation(session, opp_id)
    sys_verdict = ev.verdict if ev else None
    sys_score = ev.score if ev else None

    if mode == "agree":
        op_verdict, op_score = sys_verdict, sys_score
    elif mode == "adjust":
        op_verdict, op_score = sys_verdict, operator_score
    else:  # override
        op_verdict, op_score = operator_verdict, operator_score

    row = VerdictOverride(
        opportunity_id=opp_id,
        evaluation_id=ev.id if ev else None,
        system_verdict=sys_verdict,
        system_score=sys_score,
        operator_verdict=op_verdict,
        operator_score=op_score,
        reason=reason,
        dimension_corrections=dimension_corrections,
        created_at=utcnow_iso(),
    )
    session.add(row)
    if not commit:
        await session.flush()
        return row
    await session.commit()
    await session.refresh(row)
    return row


async def latest_override(session: AsyncSession, opp_id: int) -> VerdictOverride | None:
    return (
        await session.execute(
            select(VerdictOverride)
            .where(VerdictOverride.opportunity_id == opp_id)
            .order_by(VerdictOverride.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def live_activity(session: AsyncSession) -> dict[str, Any]:
    """A live picture of pipeline work from the task queue and the latest run (SPEC-INTERFACES 2.1
    /runs). Task counts are live even mid-run, before a Run row's summary counts are written."""
    from kestrel.db.base import utcnow_iso
    from kestrel.queue.queue import in_flight

    # Pending and leased counts are only work the worker can run: the queue's in-flight predicate
    # (review finding 1), so a switched-off track's held tasks never make the scan look running.
    # Finished counts (done, dead, cancelled) are kept for the Runs page table.
    task_rows = (
        await session.execute(
            select(Task.type, Task.status, func.count())
            .where(
                Task.type.in_(("t1", "t2", "embed", "scrape")),
                or_(Task.status.not_in(("pending", "leased")), in_flight(utcnow_iso())),
            )
            .group_by(Task.type, Task.status)
        )
    ).all()
    tasks: dict[str, dict[str, int]] = {}
    for ttype, status, count in task_rows:
        tasks.setdefault(ttype, {})[status] = count
    latest = (
        await session.execute(select(Run).order_by(Run.id.desc()).limit(1))
    ).scalar_one_or_none()
    # Failures that still need the operator, never every row ever written (D-UI-70).
    quarantined = await open_quarantine_count(session)
    return {"tasks": tasks, "latest_run": latest, "quarantined": quarantined}


async def recent_scans(session: AsyncSession, limit: int = 10) -> list[dict[str, Any]]:
    """The latest scrape runs with their search's name, for the Runs page (D-UI-38): a scan is
    pipeline work too, and it used to be visible only on Sources."""
    rows = (
        await session.execute(
            select(ScrapeRun, Source.name)
            .join(Source, Source.id == ScrapeRun.source_id, isouter=True)
            .order_by(ScrapeRun.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": r.id,
            "source": name or f"source {r.source_id}",
            "status": r.status,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "new": int((r.counts or {}).get("new", 0) or 0),
            "error": (r.error or "")[:160] or None,
            "model": run_model.label_of(r.counts),  # D-UI-72: "<model> (this run only)"
        }
        for r, name in rows
    ]


async def list_runs(session: AsyncSession, limit: int = 25) -> list[Run]:
    return list(
        (
            await session.execute(select(Run).order_by(Run.id.desc()).limit(limit))
        ).scalars()
    )


async def lookup_by_urls(session: AsyncSession, urls: list[str]) -> dict[str, dict]:
    """POST /api/opportunities/lookup: per known URL its id/score/verdict/ghost. Powers the
    extension's seen-badge (SPEC-INTERFACES 3)."""
    out: dict[str, dict] = {}
    if not urls:
        return out
    # One query, not one per matched opportunity: the extension calls this on every DOM mutation
    # batch (about every 800ms on a busy board), so an N+1 here was a steady load on the loop.
    latest = _latest_eval_subq()
    rows = (
        await session.execute(
            select(Opportunity, Evaluation)
            .join(latest, latest.c.opportunity_id == Opportunity.id, isouter=True)
            .join(Evaluation, Evaluation.id == latest.c.eid, isouter=True)
            .where(Opportunity.url.in_(urls))
        )
    ).all()
    for opp, ev in rows:
        out[opp.url] = {
            "id": opp.id,
            "score": ev.score if ev else None,
            "verdict": ev.verdict if ev else None,
            "stage": opp.stage,
            "ghost_score": opp.ghost_score,
        }
    return out
