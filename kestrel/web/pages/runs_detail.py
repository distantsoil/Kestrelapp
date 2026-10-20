"""Run detail (SPEC-INTERFACES 2.1, `GET /runs/{run_id}`) and the quarantine surface (hard rule 3:
structured output or failure, never silently dropped). Three routes:

  - `GET /runs/{run_id}`        one pipeline run's counts, plus a best-effort picture of the scrape
                                 runs and evaluations that fall inside its time window.
  - `GET /quarantine`           every quarantined LLM response, newest first, with the raw text
                                 preserved and escaped (never `|safe`, since it is untrusted model
                                 output).
  - `POST /quarantine/{id}/retry`  re-queues the opportunity for the tier that failed. The page's
                                 Retry on a scored tier (1 or 2, or a failed score-again) opens the
                                 Score again dialog instead, preselected to the model that failed
                                 (D-UI-71); this route stays for Tier 3 and rows without a role,
                                 which only explain where to go. This route
                                 must never call an LLM itself; it only writes a stage change and
                                 a Task row, so the background worker does the actual re-evaluation
                                 later (hard rule 4: uncertainty escalates, it never rejects).

`Run` carries no foreign key to `ScrapeRun` or `Opportunity`/`Evaluation` (checked kestrel/db/models
directly: no `run_id` column anywhere outside `Run.id` itself), so "affected" rows are matched by
timestamp overlap with [run.started_at, run.finished_at or now]. That is disclosed on the page as a
best-effort match, not presented as an authoritative join."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity, Quarantine, Run, ScrapeRun, Source, Task
from kestrel.pipeline._shared import parse_rescore_failure
from kestrel.pipeline.tasks import enqueue_once
from kestrel.queue.rescore import requeue_rescore_task
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.rescore import outcome_for
from kestrel.web.queries import (
    failure_summary,
    live_activity,
    open_quarantine_clause,
    quarantine_needs_you_clause,
    quarantine_queued_clause,
    quarantine_superseded,
)

log = structlog.get_logger()

router = APIRouter()

# Retry must never move an opportunity BACKWARD into a position where it can be rejected.
#
# Hard rule 4 (uncertainty escalates, it never rejects) is already applied at quarantine time, and
# that changes what "retry" may safely mean per tier:
#
#   Tier 1: handle_t1 (tier1.py) responds to a quarantine by PROMOTING to "t1_pass" with
#     forced_by="t1_quarantine_promoted" and queuing t2. The item is not stuck, it is already saved.
#     Rolling it back to "t0_pass" would re-run Tier 1, which can return a kill and land it in
#     "t1_reject", so the Retry button would be able to reject an item the escalation rule had
#     rescued. Retry therefore only re-queues the t2 the promotion already implies, self-healing a
#     lost task without ever un-promoting.
#   Tier 2: handle_t2 (tier2.py) sets stage="quarantined" on a structural failure, which IS stuck
#     and has no queued successor. Resetting to "t1_pass" plus a t2 task is the real repair, and it
#     is the case this button exists for.
#   Tier 3: there is no worker task kind at all; escalation is operator-confirmed only and must
#     never acquire an automatic path (hard rule 1). Resetting to "new" would re-run Tier 0, which
#     CAN reject an item that already cleared Tiers 0, 1 and 2, discarding "t2_done" and burning two
#     more evaluations. Retry does not touch the stage; it points the operator at Escalate.
_RETRY_TASK = {1: "t2", 2: "t2"}
_RETRY_STAGE = {2: "t1_pass"}  # tier 1 keeps whatever stage its promotion already earned


async def _scrape_runs_during(session: AsyncSession, run: Run, limit: int = 50) -> list[dict]:
    stmt = (
        select(ScrapeRun, Source.name)
        .join(Source, Source.id == ScrapeRun.source_id, isouter=True)
        .where(ScrapeRun.started_at >= run.started_at)
    )
    if run.finished_at:
        stmt = stmt.where(ScrapeRun.started_at <= run.finished_at)
    stmt = stmt.order_by(ScrapeRun.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [{"scrape": sr, "source_name": name} for sr, name in rows]


async def _evaluations_during(session: AsyncSession, run: Run, limit: int = 100) -> list[dict]:
    stmt = (
        select(Evaluation, Opportunity)
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .where(Evaluation.created_at >= run.started_at)
    )
    if run.finished_at:
        stmt = stmt.where(Evaluation.created_at <= run.finished_at)
    stmt = stmt.order_by(Evaluation.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [{"ev": ev, "opp": opp} for ev, opp in rows]


async def _run_context(request: Request, session: AsyncSession, run: Run) -> dict:
    activity = await live_activity(session)  # reuse: quarantined count, no new query for it
    return {
        "title": f"Run {run.id}",
        "query": dict(request.query_params),
        "run": run,
        "scrape_runs": await _scrape_runs_during(session, run),
        "evaluations": await _evaluations_during(session, run),
        "quarantined_count": activity["quarantined"],
    }


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def page(request: Request, run_id: int):
    async with sessionmaker()() as s:
        try:
            run = await s.get(Run, run_id)
        except Exception as exc:  # a lookup failure must render, never 500
            log.warning("run_lookup_failed", run_id=run_id, error_type=type(exc).__name__,
                        error=str(exc))
            return templates().TemplateResponse(
                request,
                "run_detail.html",
                {"title": "Run", "query": dict(request.query_params), "run": None,
                 "load_error": plain_error(exc)},
                status_code=404,
            )
        if run is None:
            return templates().TemplateResponse(
                request,
                "run_detail.html",
                {"title": "Run", "query": dict(request.query_params), "run": None},
                status_code=404,
            )
        ctx = await _run_context(request, s, run)
    return templates().TemplateResponse(request, "run_detail.html", ctx)


# Open failures are listed in full (hard rule 4: a failure Today counts must be on this page);
# the cap only guards against a runaway table and the page says how many it left out. Resolved
# rows are history and keep their own, smaller cap (D-UI-70).
OPEN_CAP = 5000
RESOLVED_CAP = 200


async def _quarantine_rows(
    session: AsyncSession, *, open_cap: int = OPEN_CAP, resolved_cap: int = RESOLVED_CAP
) -> list[dict]:
    """Every open quarantine row (newest first, up to `open_cap`), then the newest resolved ones
    (up to `resolved_cap`). Each is marked `resolved` when a later score of the same role at the
    same or a higher tier superseded it (web.queries.quarantine_superseded), `queued` when a
    task that would resolve it is in flight (quarantine_queued_clause), `hidden` when its role is
    hidden, and `needs_you` when it counts toward the one "needs you" number (D-UI-70)."""
    cols = (
        Quarantine, Opportunity, quarantine_queued_clause().label("queued"),
        quarantine_needs_you_clause().label("needs_you"),
    )
    base = (
        select(*cols)
        .join(Opportunity, Opportunity.id == Quarantine.opportunity_id, isouter=True)
        .order_by(Quarantine.id.desc())
    )
    open_rows = (await session.execute(base.where(open_quarantine_clause()).limit(open_cap))).all()
    done_rows = (
        await session.execute(base.where(quarantine_superseded()).limit(resolved_cap))
    ).all()
    out = []
    for resolved, rows in ((False, open_rows), (True, done_rows)):
        for q, opp, queued, needs_you in rows:
            rescore, task_id, error = parse_rescore_failure(q.error)
            hidden = opp is not None and opp.archived_at is not None
            out.append(
                {"q": q, "opp": opp, "rescore": rescore, "task_id": task_id, "error": error,
                 "resolved": resolved, "hidden": hidden and not resolved,
                 "queued": bool(queued) and not resolved and not hidden,
                 "needs_you": bool(needs_you) and not resolved}
            )
    return out


async def _open_total(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Quarantine).where(open_quarantine_clause())
            )
        ).scalar_one()
        or 0
    )


async def _quarantine_context(request: Request) -> dict:
    """Three lists: a failure that parked the role (its stage is quarantined, or Tier 1 promoted
    it); a failed score-again of a role already scored, whose earlier score stands and whose
    stage was never touched, shown apart so it never reads as a parked role; and, as history, the
    failures a later score already resolved (D-UI-70), which no count calls failed. The banner
    reads failure_summary, the number Today, the rail and the tray show."""
    async with sessionmaker()() as s:
        rows = await _quarantine_rows(s)
        needs_you = (await failure_summary(s)).total
        open_total = await _open_total(s)
    open_rows = [r for r in rows if not r["resolved"]]
    return {
        "query": dict(request.query_params),
        "rows": [r for r in open_rows if not r["rescore"]],
        "rescore_rows": [r for r in open_rows if r["rescore"]],
        "resolved_rows": [r for r in rows if r["resolved"]],
        "needs_you": needs_you,
        "open_more": max(0, open_total - len(open_rows)),
        "outcome": outcome_for(request.query_params),
        "retry_error": None,
    }


@router.get("/quarantine", response_class=HTMLResponse)
async def quarantine_list(request: Request):
    ctx = await _quarantine_context(request)
    ctx["title"] = "Failed scoring"
    is_hx = request.headers.get("HX-Request")
    tmpl = "partials/quarantine_list.html" if is_hx else "quarantine.html"
    return templates().TemplateResponse(request, tmpl, ctx)


async def _retry_quarantine(session: AsyncSession, quarantine_id: int) -> str | None:
    """Re-queue the opportunity behind one quarantine row. Returns an error string on a handled
    failure (unknown id, orphaned row), or None on success. Never calls the LLM or any network
    client: this only writes a stage change and a Task row (kestrel.pipeline.tasks.enqueue_once),
    the same durable-queue mechanism the pipeline itself uses, so the worker performs the actual
    re-evaluation on its own cycle. `raw_response` is never touched; it is evidence."""
    q = await session.get(Quarantine, quarantine_id)
    if q is None:
        return f"No quarantine record #{quarantine_id}."

    q.attempts = (q.attempts or 0) + 1

    if q.opportunity_id is None:
        await session.commit()
        return "Retried (no opportunity was attached to this record; attempt count updated only)."

    opp = await session.get(Opportunity, q.opportunity_id)
    if opp is None:
        await session.commit()
        return f"Retried, but opportunity #{q.opportunity_id} no longer exists."

    is_rescore, task_id, _ = parse_rescore_failure(q.error)
    if is_rescore:
        # A failed score-again of a role already scored: its stage (and earlier score) stands.
        # Re-queue the same score-again, model override included; never touch the stage.
        earlier = await session.get(Task, task_id) if task_id else None
        payload = dict(earlier.payload or {}) if earlier is not None else {}
        added = await requeue_rescore_task(
            session, opp.id, payload, earlier.type if earlier is not None else "t2"
        )
        await session.commit()
        if not added:
            return "This role is already waiting to be scored again."
        return None

    task_type = _RETRY_TASK.get(q.tier)
    if task_type is None:
        # Tier 3 and anything else with no worker task kind. Do NOT reset the stage: see the note
        # on _RETRY_TASK above. Escalation stays operator-confirmed, which is hard rule 1.
        await session.commit()
        return (
            f"Tier {q.tier} has no automatic retry: escalation is operator-confirmed only. "
            f"Attempt count updated. Use Escalate on the opportunity to send it again."
        )

    stage = _RETRY_STAGE.get(q.tier)
    if stage is not None:
        opp.stage = stage
        opp.stage_reason = f"quarantine #{q.id} retry: re-queued for tier {q.tier}"
    else:
        opp.stage_reason = (
            f"quarantine #{q.id} retry: re-queued tier 2 (tier 1 promotion left intact)"
        )
    # The operator clicked Retry: never held by a switched-off track (finding 10).
    await enqueue_once(session, task_type, opp.id, priority=int(opp.pre_score * 100),
                       extra={"requested": True})
    await session.commit()
    return None


@router.post("/quarantine/{quarantine_id}/retry", response_class=HTMLResponse)
async def retry(request: Request, quarantine_id: int):
    retry_error: str | None = None
    async with sessionmaker()() as s:
        try:
            retry_error = await _retry_quarantine(s, quarantine_id)
        except Exception as exc:  # a write path must never turn into a 500
            await s.rollback()
            log.warning("quarantine_retry_failed", quarantine_id=quarantine_id,
                        error_type=type(exc).__name__, error=str(exc))
            retry_error = f"Could not retry quarantine #{quarantine_id}: {plain_error(exc)}"
    ctx = await _quarantine_context(request)
    ctx["title"] = "Failed scoring"
    ctx["retry_error"] = retry_error
    return templates().TemplateResponse(request, "partials/quarantine_list.html", ctx)
