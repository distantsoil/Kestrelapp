"""Why a role has no score, told truthfully (hard rule 4: a role is never hidden or rejected to
explain it).

queries.scoring_states sorts an unscored role by its scoring task. Four of those states could
read "Waiting, it scores on its own" when nothing would score it:

- its last scoring task died after its tries (a model that could not be reached, a model not
  installed, a refused key, a crash): no task is left, so it read "not_queued";
- Kestrel is paused (the tray, or Stop on Runs): nothing is claimed until Resume;
- its task is held because the online model is out of credit;
- it waits in the background lane, which only ever scores on local models, while scoring is
  online.

`apply_holds` re-reads those rows and gives them their true state, which fit.SCORING_CHIPS words
the same everywhere (the Pipeline row and panel, the role page, the Runs summary). It only reads:
it never queues, sends, rejects or hides anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Task

# The scoring task types whose death leaves a role unscored (embed never scores).
_SCORING = ("t1", "t2")

# States whose line is the stored cause (a dead task's last error, a quarantine error).
_STOPPED = ("dead", "dead_final", "failed")
# States apply_holds may replace: a role a task or Tier 0 would score on its own.
_WAITING = ("queued", "not_queued", "background")
# The tier each scoring task type runs on, for the engine preflight's record (D-WORKER-6).
_ENGINE_TIER = {"t1": "tier1", "t2": "tier2"}

# A dead task's last error in plain words, checked in order (the first match wins). A model
# missing on this computer (Ollama asks to pull it) is told apart from a model an online
# service does not offer (a 404, "No endpoints found").
_STOPPED_WORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("try pulling", "ollama pull", "pull it first", "pull the model"),
     "The model is not installed on this computer."),
    (("no endpoints found", "model_not_found", "does not exist", "not a valid model"),
     "The online service does not offer this model."),
    (("401", "403", "unauthorized", "forbidden", "api key", "invalid key"),
     "The online service did not accept the key."),
    (("not found", "404", "no such model"),
     "The model could not be found."),
    (("connect", "unreachable", "refused", "timed out", "timeout", "name resolution"),
     "Kestrel could not reach the model."),
)


def stopped_words(error: str | None) -> str:
    """Why a scoring task gave up, in one sentence."""
    from kestrel.web.fit import failure_words

    low = (error or "").lower()
    for needles, words in _STOPPED_WORDS:
        if any(n in low for n in needles):
            return words
    return failure_words(error).capitalize() + "."


def _swept(opp: Any) -> bool:
    """Whether the worker moves this role to Needs another try (pipeline.deadletter, each cycle
    and the moment a task dies): any active role, whatever its stage (D-WORKER-8). A hidden role
    is left alone, so nothing will try it again."""
    return opp.archived_at is None


def _pause_state() -> str | None:
    """"maintenance" while a restore or reset holds the worker (it ends on its own), "app_paused"
    while the operator paused it, else None. Maintenance wins: Resume cannot end it."""
    from kestrel.web.app import maintenance_held, user_paused

    if maintenance_held():
        return "maintenance"
    return "app_paused" if user_paused() else None


def _settings_now() -> tuple[Any, bool]:
    """(settings, whether scoring runs on an online model now). The worker's current run mode
    wins over the saved one, as Today reads it."""
    from kestrel.config import get_settings
    from kestrel.pipeline.lane import scores_on_hosted
    from kestrel.web.app import current_run_mode

    settings = get_settings()
    return settings, current_run_mode() == "hosted" or scores_on_hosted(settings)


async def apply_holds(session: AsyncSession, rows: list[Any]) -> None:
    """Correct the waiting states of `rows` (queries.OppRow, after scoring_states) to the
    reason nothing scores them, when there is one. Sets `scoring`, and `failed_reason` for a
    stopped one."""
    waiting = [r for r in rows if getattr(r, "scoring", None) in _WAITING]
    if not waiting:
        return
    from kestrel.queue.queue import (
        DEFER_REASON_KEY,
        DEFERRED_KEY,
        REASON_CREDIT,
        WAIT_CAUSE_KEY,
    )

    ids = [r.opp.id for r in waiting]
    oid = Task.opportunity_id
    # Newest scoring task per role: a dead one with nothing newer means scoring gave up.
    newest = (
        select(func.max(Task.id)).where(Task.type.in_(_SCORING), oid.in_(ids)).group_by(oid)
    )
    last = {
        int(o): (status, err, payload or {}, typ)
        for o, status, err, payload, typ in (
            await session.execute(
                select(oid, Task.status, Task.last_error, Task.payload, Task.type).where(
                    Task.id.in_(newest)
                )
            )
        ).all()
        if o is not None
    }
    settings, online = _settings_now()
    from kestrel.pipeline.worker import held_types

    credit_types = set(held_types(settings))
    paused = _pause_state()
    from kestrel.pipeline.worker import engine_wait

    engine = engine_wait()
    for r in waiting:
        status, err, payload, typ = last.get(r.opp.id, (None, None, {}, None))
        if r.scoring == "not_queued" and status == "dead":
            # The worker quarantines an active role's dead scoring task (pipeline.deadletter):
            # it moves to Needs another try. A hidden role is left alone (D-WORKER-8).
            r.scoring = "dead" if _swept(r.opp) else "dead_final"
            r.failed_reason = stopped_words(err)
        elif paused:
            r.scoring = paused
        elif (
            status == "pending"
            and typ in _ENGINE_TIER
            and _ENGINE_TIER[typ] in engine
            and not (payload.get("model_override") and (typ == "t2" or payload.get("rescore")))
        ):
            # The worker's preflight found the local engine down or the tier's model missing
            # (D-WORKER-6): nothing is leased until it is back. A Score again on another model
            # is not judged here (it may run elsewhere).
            r.scoring = engine[_ENGINE_TIER[typ]]
        elif (status == "pending" and typ in _ENGINE_TIER
              and payload.get(WAIT_CAUSE_KEY) == "engine"):
            # Its own call could not reach the local engine, written on its row (D-WORKER-6
            # amended, round 11): it waits for the engine, its try given back, whatever the
            # preflight saw last (an engine that answers the probe and drops each call).
            r.scoring = "engine_down"
        elif status == "pending" and (
            (payload.get(DEFERRED_KEY) and payload.get(DEFER_REASON_KEY) == REASON_CREDIT)
            or typ in credit_types
        ):
            r.scoring = "credit"
        elif r.scoring == "background" and online:
            r.scoring = "local_only"


# The action that moves a role forward, per state: (label, href). "{id}" is the role's id.
ACTIONS: dict[str, tuple[str, str] | None] = {
    "dead": ("Score again", "/rescore/choose?opp_ids={id}&page=1&next=/opportunities"),
    "dead_final": ("Score again", "/rescore/choose?opp_ids={id}&page=1&next=/opportunities"),
    "stuck": ("Score again", "/rescore/choose?opp_ids={id}&page=1&next=/opportunities"),
    "failed": ("Score again", "/rescore/choose?opp_ids={id}&page=1&next=/opportunities"),
    "app_paused": ("Resume on Runs", "/runs"),
    "maintenance": None,
    "credit": ("Open Models", "/models"),
    "local_only": ("Open Models", "/models"),
    "engine_down": ("Open Models", "/models"),
    "model_missing": ("Open Models", "/models"),
    "held": ("Turn the track on in Profile", "/profile"),
    "empty": ("Paste the description", "/opportunities/{id}"),
    "hidden": ("Open the role", "/opportunities/{id}"),
    "removed": ("Undo in Needs another try", "/quarantine"),
    "approval": ("Confirm", "/rescore/choose?opp_ids={id}&page=1&next=/opportunities"),
}


def action_for(state: str | None, opp_id: int) -> dict[str, str] | None:
    """The one action for a role in `state`, or None when it scores on its own."""
    found = ACTIONS.get(state or "")  # None: nothing for the person to do
    if found is None:
        return None
    return {"label": found[0], "href": found[1].replace("{id}", str(int(opp_id)))}


# The short line a Pipeline row prints after the company and place, per state. A role that
# scores on its own (queued, not reached yet, being read now) needs none: its chip says so.
ROW_LINES: dict[str, str] = {
    "app_paused": "Kestrel is paused, so nothing is scored.",
    "maintenance": "Kestrel is restoring or resetting your data. Scoring starts again when it "
                   "finishes.",
    "credit": "Your online model is out of credit. Nothing was sent.",
    "engine_down": "Kestrel cannot reach the AI engine on this computer, so scoring waits.",
    "model_missing": "The model for this step is not installed, so scoring waits.",
    "local_only": "Background roles score only on a local model, and scoring is online now.",
    "held": "Its track is switched off.",
    "background": "It scores in the background when Kestrel is idle.",
    "approval": "Nothing was sent. It waits for your OK.",
}


def row_line(state: str | None) -> str:
    """The row's why clause for an unscored role in `state` ("" when none is needed)."""
    return ROW_LINES.get(state or "", "")


# The states the role page explains in its own words (the rest already have a sentence there:
# rule set aside, no description, hidden as applied, held for approval, being scored again).
ROLE_PAGE_STATES = ("dead", "dead_final", "stuck", "app_paused", "maintenance", "credit",
                    "engine_down", "model_missing", "local_only", "held", "background",
                    "removed", "failed")


async def role_state(session: AsyncSession, opp_id: int) -> dict[str, Any] | None:
    """Where an unscored role stands, for the role page: the same probe the Pipeline panel
    builds (pages.pipeline.panel_context), so the two never disagree. None when the role is
    gone. Read only."""
    from kestrel.db.models import Opportunity
    from kestrel.web.fit import fit_for, scoring_chip
    from kestrel.web.pages.pipeline import _flags
    from kestrel.web.queries import OppRow, latest_evaluation, scoring_states

    opp = await session.get(Opportunity, opp_id)
    if opp is None:
        return None
    live, removed, _saved, queued = await _flags(session, opp_id)
    ev = await latest_evaluation(session, opp_id)
    probe = OppRow(
        opp=opp, ev=ev, org_name=None, source_name=None, removed=removed,
        fit=fit_for(ev, opp.stage, live=live, stage_reason=opp.stage_reason, removed=removed,
                    hidden_applied=opp.archive_reason == "applied", queued=queued),
    )
    await scoring_states(session, [probe])
    state = probe.scoring
    if state not in ROLE_PAGE_STATES:
        return {"state": state}
    chip = scoring_chip(state)
    reason = probe.failed_reason if state in _STOPPED else None
    return {
        "state": state,
        "label": chip["label"],
        "text": f"{reason} {chip['tip']}" if reason else chip["tip"],
        "action": action_for(state, opp_id),
    }


# The order the Runs summary lists reasons in: what needs the person first, then what waits.
SUMMARY_ORDER = ("dead", "dead_final", "stuck", "failed", "empty", "engine_down",
                 "model_missing", "credit", "app_paused", "maintenance", "local_only",
                 "approval", "held", "removed", "hidden", "background", "queued", "not_queued",
                 "running")
# States whose action is about one role (its own Score again or paste), so the Runs card links
# the role, not a button for the whole group.
_PER_ROLE = ("dead", "dead_final", "stuck", "failed", "empty", "hidden", "approval")
# What a set-aside role reads (a rule or the quick screen judged it before any full score).
SET_ASIDE = {"t0_reject": "Set aside by your rules", "t1_reject": "Set aside by the quick screen"}


# The stages a role with no score can be at (db.enums.STAGES less the rejected and done ones).
_UNSCORED_STAGES = ("new", "t0_pass", "t1_pass", "error", "quarantined")


async def unscored_rows(session: AsyncSession) -> list[Any]:
    """Every listed role with no score, each with its reading (scoring_states, then
    apply_holds): the active roles Pipeline lists, plus the roles hidden as applied before a
    model read them. Signals are not roles. Filtered in SQL to the unscored stages, and only the
    columns the reading needs are loaded (never a description). Each row also carries
    `source_kind` and `source_site`. Read only."""
    from sqlalchemy import or_
    from sqlalchemy.orm import load_only

    from kestrel.db.models import Evaluation, Opportunity, Source
    from kestrel.web.fit import GROUP_FAILED, GROUP_NEW, GROUP_UNSCORED
    from kestrel.web.queries import (
        OppRow,
        _latest_eval_subq,
        _retry_removed,
        listed_clause,
        live_scoring_exists,
        row_fit,
        scoring_states,
        scoring_task_exists,
    )

    latest = _latest_eval_subq()
    stmt = (
        select(Opportunity, Evaluation, live_scoring_exists(), _retry_removed(Opportunity.id),
               scoring_task_exists(), Source.kind,
               func.json_extract(Opportunity.raw, "$.site"))
        .select_from(Opportunity)
        .join(latest, latest.c.opportunity_id == Opportunity.id, isouter=True)
        .join(Evaluation, Evaluation.id == latest.c.eid, isouter=True)
        .join(Source, Source.id == Opportunity.source_id, isouter=True)
        .options(
            load_only(Opportunity.id, Opportunity.title, Opportunity.url, Opportunity.stage,
                      Opportunity.stage_reason, Opportunity.archive_reason,
                      Opportunity.archived_at, Opportunity.track, Opportunity.opp_type,
                      raiseload=True),
            load_only(Evaluation.id, Evaluation.tier, Evaluation.verdict, Evaluation.score,
                      Evaluation.weighted_score, Evaluation.needs_review, raiseload=True),
        )
        .where(
            Opportunity.stage.in_(_UNSCORED_STAGES),
            or_(Opportunity.archived_at.is_(None),
                and_(Opportunity.archive_reason == "applied", Opportunity.stage == "new")),
            listed_clause(),
            Opportunity.opp_type != "trigger_event",
        )
    )
    rows = []
    for opp, ev, live, removed, queued, kind, site in (await session.execute(stmt)).all():
        fit = row_fit(opp, ev, live=bool(live), removed=bool(removed), queued=bool(queued))
        if fit.group not in (GROUP_NEW, GROUP_UNSCORED, GROUP_FAILED):
            continue
        row = OppRow(opp=opp, ev=ev, org_name=None, source_name=None, removed=bool(removed),
                     fit=fit)
        row.source_kind, row.source_site = kind, site
        rows.append(row)
    await scoring_states(session, rows)
    return rows


async def set_aside_counts(session: AsyncSession) -> dict[str, int]:
    """How many listed active roles a rule or the quick screen set aside (no full score)."""
    from kestrel.db.models import Opportunity
    from kestrel.web.queries import listed_clause

    got = (
        await session.execute(
            select(Opportunity.stage, func.count())
            .where(Opportunity.archived_at.is_(None), listed_clause(),
                   Opportunity.stage.in_(tuple(SET_ASIDE)))
            .group_by(Opportunity.stage)
        )
    ).all()
    return {str(stage): int(n) for stage, n in got}


async def reason_counts(session: AsyncSession) -> dict[str, int]:
    """Counts by reason key: the diagnostics bundle's numbers. Keys only, never role text."""
    out: dict[str, int] = {}
    for r in await unscored_rows(session):
        key = r.scoring or "not_queued"
        out[key] = out.get(key, 0) + 1
    for stage, n in (await set_aside_counts(session)).items():
        out[stage] = n
    return dict(sorted(out.items()))


async def summary(session: AsyncSession, *, per: int = 5) -> dict[str, Any]:
    """The Runs page's Why roles have no score card: one entry per reason with its count, its
    sentence, its action and the first `per` roles (title and board only), plus the set-aside
    counts. Reasons are in SUMMARY_ORDER."""
    from kestrel.web.fit import scoring_chip
    from kestrel.web.pipeline_view import source_word

    by: dict[str, list[Any]] = {}
    for r in await unscored_rows(session):
        by.setdefault(r.scoring or "not_queued", []).append(r)
    reasons = []
    for key in sorted(by, key=lambda k: SUMMARY_ORDER.index(k) if k in SUMMARY_ORDER else 99):
        rows = sorted(by[key], key=lambda r: r.opp.id, reverse=True)
        chip = scoring_chip(key)
        # A group-wide action only: per-role ones (Score again, the paste) live on each role.
        act = None if key in _PER_ROLE else action_for(key, 0)
        if key == "failed":  # Runs' own word for these (never "failed" on Runs)
            from kestrel.web.fit import FAILED_NOTE

            chip = {"label": "Needs another try", "tip": FAILED_NOTE}
        reasons.append({
            "key": key,
            "label": chip["label"],
            "tip": chip["tip"],
            "count": len(rows),
            "roles": [
                {"id": r.opp.id, "title": r.opp.title or "(untitled)",
                 "source": source_word(r.source_kind, r.source_site),
                 "why": r.failed_reason if key in _STOPPED else None}
                for r in rows[:per]
            ],
            "more": max(0, len(rows) - per),
            "action": act["label"] if act else None,
            "href": act["href"] if act else None,
        })
    aside = await set_aside_counts(session)
    no_vector = await vector_failures(session)
    return {
        "reasons": reasons,
        "total": sum(x["count"] for x in reasons),
        # Scored without a vector (their embed task died): said here, never silent.
        "no_vector": await _no_vector_block(session, no_vector, per),
        "set_aside": [
            {"label": SET_ASIDE[k], "count": aside[k]} for k in SET_ASIDE if aside.get(k)
        ],
    }


# A role whose embed task died (D-WORKER-6 amended, round 4). Scoring never waits for the vector
# (it only helps sort: the quick-screen gate and the scoring examples work without it), so the
# role is scored without it and this says so, on Runs and on the role page, never silently.
_VECTOR_NOTE = {
    True: ("Kestrel could not prepare this role for sorting. {why} It was still scored, though "
           "its place in your lists may be less accurate. Kestrel tries again on its own."),
    False: ("Kestrel could not prepare this role for sorting. {why} Scoring goes ahead without "
            "it, though its place in your lists may be less accurate. Kestrel tries again on its "
            "own."),
}


async def vector_failures(
    session: AsyncSession, opp_ids: list[int] | None = None
) -> dict[int, str]:
    """{role id: the reason in a sentence} for active roles whose newest embed task is dead and
    that hold no vector from the current embed model. One reading for Runs and the role page.
    Read only."""
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased

    from kestrel.config import get_settings
    from kestrel.db.models import Opportunity, OpportunityEmbedding
    from kestrel.llm.embeddings import current_model

    model = current_model(get_settings())
    other = aliased(Task)
    newer = exists().where(other.opportunity_id == Task.opportunity_id,
                           other.type == "embed", other.id > Task.id)
    has_vector = exists().where(OpportunityEmbedding.opportunity_id == Task.opportunity_id,
                                OpportunityEmbedding.model == model)
    active = exists().where(Opportunity.id == Task.opportunity_id,
                            Opportunity.archived_at.is_(None))
    stmt = select(Task.opportunity_id, Task.last_error).where(
        Task.type == "embed", Task.status == "dead", Task.opportunity_id.is_not(None),
        ~newer, ~has_vector, active,
    )
    if opp_ids is not None:
        stmt = stmt.where(Task.opportunity_id.in_(opp_ids))
    return {int(o): stopped_words(err) for o, err in (await session.execute(stmt)).all()}


def vector_note(why: str, *, scored: bool) -> str:
    """The role page's sentence, chosen from whether the role has a score now (§8a check 3:
    the claim and the state it describes come from one decision)."""
    return _VECTOR_NOTE[bool(scored)].format(why=why)


async def _titles(session: AsyncSession, ids: list[int]) -> dict[int, str]:
    from kestrel.db.models import Opportunity

    if not ids:
        return {}
    rows = await session.execute(select(Opportunity.id, Opportunity.title)
                                 .where(Opportunity.id.in_(ids)))
    return {int(i): t or "(untitled)" for i, t in rows.all()}


async def _no_vector_block(
    session: AsyncSession, found: dict[int, str], per: int
) -> dict[str, Any] | None:
    """The Runs card's line for roles that could not be prepared for sorting, or None. The
    roles shown are chosen, ordered and titled as one set (newest first, `per` of them), so the
    titles always belong to the roles listed (round 5, finding 2)."""
    if not found:
        return None
    ids = sorted(found, reverse=True)[:per]
    titles = await _titles(session, ids)
    return {
        "count": len(found),
        "roles": [{"id": i, "title": titles.get(i, "(untitled)"), "why": found[i]} for i in ids],
        "more": max(0, len(found) - per),
    }

