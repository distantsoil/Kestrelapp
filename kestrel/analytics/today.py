"""Data behind the Today page (SPEC-INTERFACES 2.1, docs/UI_PROPOSAL.html Screen 1): what needs the
operator today, in three columns (review, act, overnight) plus what the system wants from them.

Every function reads existing rows only; nothing here calls a model or the network. Each result
carries the count it rests on (`n`), and any derived statistic below five observations is marked
insufficient (hard rule 6). Confidential mode is applied here, not in the template, so the REST
and MCP surfaces that reuse these dicts cannot leak the current employer (CLAUDE.md privacy).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import iso
from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Run,
    ScrapeRun,
    Setting,
    Source,
)
from kestrel.services.hidden_orgs import any_hidden, not_hidden_org_clause, opp_visible_clause
from kestrel.web.queries import (
    _latest_eval_subq,
    failure_summary,
    open_quarantine_count,
    open_quarantine_errors,
)
from kestrel.web.redact import confidential, redact_name, redact_text
from kestrel.web.words import words

log = structlog.get_logger()


def _utc_today() -> date:
    """UTC today. NOT used for "today" in this module -- see `_local_date_of` for why."""
    return datetime.now(UTC).date()


def _local_date_of(stored: str | None) -> date | None:
    """The LOCAL calendar date of a stored UTC timestamp, or of a bare date string.

    Two different kinds of value live in these columns and they need different treatment:

    - `next_action_at` / `deadline` are CALENDAR dates the operator typed into a date input
      ("due tomorrow"). They have no timezone and must be compared against today's LOCAL date, or
      after 20:00 in a US time zone an item due today would already read as overdue.
    - `applied_at` / `last_activity_at` are UTC TIMESTAMPS written by `db.base.utcnow_iso`. Counting
      days against them with the local date (the original bug) undercounted by one for the four
      hours each evening when UTC has already rolled to the next day: a pursuit 30 days quiet was
      reported as 29.

    So a bare `YYYY-MM-DD` is returned as-is, and a full timestamp is converted from UTC to the
    operator's local calendar day before the day count is taken (review)."""
    text = (stored or "").strip()
    if not text:
        return None
    try:
        if len(text) <= 10:  # a calendar date, not an instant
            return date.fromisoformat(text[:10])
        return datetime.fromisoformat(text).astimezone().date()
    except ValueError:
        return None

# Verdicts that put an item on the shortlist: employment "apply", consulting "pursue".
SHORTLIST_VERDICTS: tuple[str, ...] = ("apply", "pursue")
TRACKS: tuple[str, ...] = ("employment", "consulting")
# Terminal pursuit statuses across both tracks (mirrors services/sweep.py). A closed pursuit never
# has a next action due and is never counted as ghosting.
_TERMINAL: tuple[str, ...] = (
    "accepted", "rejected", "withdrawn", "ghosted", "won", "lost", "no_response",
)
# Hard rule 6: a rate or derived statistic from fewer than this many observations is insufficient.
MIN_N = 5
# The sample a score-vs-outcome verdict is measured against on the Overnight meter.
_HONEST_SAMPLE_TARGET = 30
_WINDOW_HOURS = 24


def _window_start(run: Run | None) -> str:
    """The ISO instant "overnight" starts at: 24 hours ago, or the latest run's start when that
    is earlier (a long run is counted whole). Never later than 24 hours ago: the desktop loop
    opens a run per working cycle (D-UI-34), and a window that began at the last few-minute cycle
    showed almost nothing."""
    base = datetime.now(UTC) - timedelta(hours=_WINDOW_HOURS)
    if run is not None and run.started_at:
        try:
            started = datetime.fromisoformat(run.started_at)
        except ValueError:
            return iso(base)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if started < base:
            return run.started_at
    return iso(base)


async def latest_run(session: AsyncSession) -> Run | None:
    return (
        await session.execute(select(Run).order_by(Run.id.desc()).limit(1))
    ).scalar_one_or_none()


def _track_scope(track: str | None) -> str | None:
    return track if track in TRACKS else None


def summary_line(blocks: dict | None) -> str | None:
    """The one line of the model's own summary: Tier 2 block A, "what this actually is" for
    employment, the stated situation for consulting. None when the evaluation has neither."""
    if not isinstance(blocks, dict):
        return None
    a = blocks.get("a_role_summary")
    if isinstance(a, dict) and a.get("what_this_actually_is"):
        return str(a["what_this_actually_is"])
    a = blocks.get("a_situation")
    if isinstance(a, dict) and a.get("stated"):
        return str(a["stated"])
    return None


def disagrees(ev: Evaluation) -> bool:
    """True when the model's verdict and the weighted verdict differ, or the divergence flag is on.
    Either way the operator's call decides what the learning loop records."""
    if ev.needs_review:
        return True
    return bool(ev.verdict_model) and ev.verdict_model != ev.verdict


# ------------------------------------------------------------------------------------ review
async def review_queue(
    session: AsyncSession, track: str | None = None, limit: int = 5
) -> dict[str, Any]:
    """Shortlisted since the last run (or the last 24 h), highest score first, with the verdict,
    the (redacted) org, the model's one-line summary, and a flag when the model verdict and the
    weighted verdict disagree. `n` is the full count; `items` is the top `limit`."""
    track = _track_scope(track)
    since = _window_start(await latest_run(session))
    latest = _latest_eval_subq()
    stmt = (
        select(Opportunity, Evaluation, Org.name)
        .join(latest, latest.c.opportunity_id == Opportunity.id)
        .join(Evaluation, Evaluation.id == latest.c.eid)
        .join(Org, Org.id == Opportunity.org_id, isouter=True)
        .outerjoin(Pursuit, Pursuit.opportunity_id == Opportunity.id)
        .where(
            Pursuit.id.is_(None),
            Opportunity.archived_at.is_(None),
            not_hidden_org_clause(),  # a hidden company stays out of Today (D-UI-123)
            Evaluation.tier == 2,
            Evaluation.created_at >= since,
            Evaluation.verdict.in_(SHORTLIST_VERDICTS) | Evaluation.needs_review.is_(True),
        )
        .order_by(Evaluation.score.desc().nulls_last(), Evaluation.id.desc())
    )
    if track:
        stmt = stmt.where(Opportunity.track == track)
    rows = (await session.execute(stmt)).all()
    conf, mapping = await confidential(session)
    items: list[dict[str, Any]] = []
    n_review = 0
    for opp, ev, org_name in rows:
        flag = disagrees(ev)
        n_review += int(flag)
        if len(items) >= limit:
            continue
        where = opp.city or opp.location_raw or ("remote" if opp.is_remote else None)
        items.append(
            {
                "id": opp.id,
                "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
                "org": redact_name(org_name or opp.org_name, mapping, enabled=conf),
                "track": opp.track,
                "where": redact_text(where, mapping, enabled=conf),
                "verdict": ev.verdict,
                "verdict_model": ev.verdict_model,
                "score": ev.score,
                "summary": redact_text(summary_line(ev.blocks), mapping, enabled=conf),
                "needs_review": flag,
            }
        )
    return {"items": items, "n": len(rows), "needs_review": n_review, "since": since}


# --------------------------------------------------------------------------------------- act
def due_label(due: date, today: date) -> tuple[str, str]:
    """(bucket, label) for a due date: overdue, today, tomorrow, or a weekday inside the week."""
    if due < today:
        return "overdue", "overdue"
    if due == today:
        return "today", "today"
    if due == today + timedelta(days=1):
        return "tomorrow", "tomorrow"
    from kestrel.date_display import format_display

    return "week", format_display(due, "weekday")


async def actions_due(
    session: AsyncSession, track: str | None = None, limit: int = 5, *, today: date | None = None
) -> dict[str, Any]:
    """Open pursuits with a next action due this week, overdue first. Each row is labeled
    overdue / today / tomorrow / weekday. `n` is the full count; `items` is the top `limit`."""
    track = _track_scope(track)
    today = today or date.today()
    horizon = (today + timedelta(days=7)).isoformat()
    stmt = (
        select(Pursuit, Opportunity, Org.name)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .join(Org, Org.id == Opportunity.org_id, isouter=True)
        .where(
            Pursuit.next_action_at.is_not(None),
            Pursuit.closed_at.is_(None),
            Pursuit.status.not_in(_TERMINAL),
            Pursuit.next_action_at <= horizon,
        )
        .order_by(Pursuit.next_action_at, Pursuit.id)
    )
    if track:
        stmt = stmt.where(Pursuit.track == track)
    from kestrel.services import pursuit_board as board_svc

    rows = (await session.execute(stmt)).all()
    conf, mapping = await confidential(session)
    items: list[dict[str, Any]] = []
    buckets: Counter[str] = Counter()
    for p, opp, org_name in rows:
        try:
            due = date.fromisoformat((p.next_action_at or "")[:10])
        except ValueError:
            continue
        bucket, label = due_label(due, today)
        action_text = p.next_action or "Review pursuit"
        if bucket == "overdue" and not board_svc.is_waiting(p):
            # A passed interview is not a late follow-up: it asks for the outcome.
            if board_svc.interview_passed(p, today) is None:
                continue
            bucket, label = "outcome", "record the outcome"
            action_text = f"Interview was {board_svc.short_date(due.isoformat())}"
        buckets[bucket] += 1
        if len(items) >= limit:
            continue
        items.append(
            {
                "pursuit_id": p.id,
                "opp_id": opp.id,
                "action": redact_text(action_text, mapping, enabled=conf),
                "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
                "org": redact_name(org_name or opp.org_name, mapping, enabled=conf),
                "track": p.track,
                "status": p.status,
                "due": due.isoformat(),
                "bucket": bucket,
                "label": label,
            }
        )
    return {
        "items": items,
        "n": sum(buckets.values()),
        "overdue": buckets["overdue"],
        "today": buckets["today"],
        "tomorrow": buckets["tomorrow"],
        "week": buckets["week"],
        "outcome": buckets["outcome"],
    }


# ------------------------------------------------------------------------- review and act
async def worth_a_look(
    session: AsyncSession, track: str | None = None, *, top: int = 0
) -> dict[str, Any]:
    """THE one count of "worth a look": Today's Review card and the sidebar badge both call
    this, and it is the Pipeline's own Worth a look header, computed by the Pipeline's own
    statement (queries._base_stmt plus the operator join and _group_expr), so the three never
    disagree. That group holds the model's apply / pursue and your-call verdicts (the latest
    evaluation: highest tier, then newest), every role on the shortlist (an open pursuit), and
    every role the operator labeled apply or pursue (D-UI-51), so adding a role to the
    shortlist never drops it from the count. Hidden roles are out; no time window. `strong` and
    `your_call` are the subsets the fit vocabulary calls a strong fit and your call (by the
    model's word).

    `top` > 0 also returns `top`: the best `top` of those same rows by the fit number the
    Pipeline shows (D-UI-60, Today's Review card), each with its fit word and tone, the title
    and the org, redacted under confidential mode exactly as the Pipeline list redacts them
    (redact_text on the title, redact_name on the org)."""
    from kestrel.web.fit import GROUP_WORTH, STRONG, YOUR_CALL
    from kestrel.web.queries import (
        GROUP_ORDER,
        OppFilters,
        _base_stmt,
        _group_expr,
        _with_operator,
        row_fit,
    )

    track = _track_scope(track)
    stmt = _with_operator(
        _base_stmt(OppFilters(track=track), Opportunity, Evaluation, Org.name)
    ).where(_group_expr() == GROUP_ORDER.index(GROUP_WORTH))
    n = strong = your_call = 0
    ranked: list[tuple[float, int, Any, Any, str | None]] = []
    for opp, ev, org_name in (await session.execute(stmt)).all():
        n += 1
        fit = row_fit(opp, ev)
        strong += int(fit.word == STRONG)
        your_call += int(fit.word == YOUR_CALL)
        if top:
            score = fit.score if fit.score is not None else -1.0
            ranked.append((score, opp.id, opp, fit, org_name or opp.org_name))
    out: dict[str, Any] = {"n": n, "strong": strong, "your_call": your_call}
    if top:
        from kestrel.web.redact import confidential_once

        conf, mapping = await confidential_once(session)
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        out["top"] = [
            {
                "id": opp.id,
                "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
                "org": redact_name(org, mapping, enabled=conf),
                "word": fit.word,
                "tone": fit.tone,
                "score": fit.score,
            }
            for _score, _id, opp, fit, org in ranked[:top]
        ]
    return out


async def act_summary(
    session: AsyncSession, track: str | None = None, *, today: date | None = None
) -> dict[str, Any]:
    """The Act card (D-UI-51): roles on the shortlist still to apply to, plus follow-ups due.
    `apply` counts the first board column of each track (To apply: shortlisted or preparing; To
    contact: researching). `follow_up` counts open pursuits waiting on a reply whose next action
    is due today or earlier (the board's quiet rule: a passed interview is an outcome to record,
    not a late follow-up). `earliest` names the oldest due follow-up and how long it has been
    quiet, redacted under confidential mode."""
    from kestrel.services import pursuit_board as board_svc

    track = _track_scope(track)
    today = today or date.today()
    n_apply = await _count(
        session,
        select(func.count()).select_from(Pursuit)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id).where(
            Pursuit.status.in_(("shortlisted", "preparing", "researching")),
            Pursuit.closed_at.is_(None),
            opp_visible_clause(),
            *([Pursuit.track == track] if track else []),
        ),
    )
    stmt = (
        select(Pursuit, Opportunity)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .where(
            Pursuit.next_action_at.is_not(None),
            Pursuit.closed_at.is_(None),
            Pursuit.status.not_in(_TERMINAL),
            Pursuit.next_action_at <= today.isoformat() + "\uffff",
            opp_visible_clause(),
        )
        .order_by(Pursuit.next_action_at, Pursuit.id)
    )
    if track:
        stmt = stmt.where(Pursuit.track == track)
    due: list[tuple[Pursuit, Opportunity, date]] = []
    for p, opp in (await session.execute(stmt)).all():
        try:
            d = date.fromisoformat((p.next_action_at or "")[:10])
        except ValueError:
            continue
        if board_svc.is_waiting(p) and d <= today:
            due.append((p, opp, d))
    earliest = None
    if due:
        conf, mapping = await confidential(session)
        p, opp, d = due[0]
        quiet = None
        # `today` is the LOCAL date (due dates are local calendar dates the operator typed), and
        # the stored UTC timestamp is converted to a local day before counting, so a pursuit 30
        # days quiet reads as 30 rather than 29 in the evening, when UTC has already rolled over.
        since = _local_date_of(p.applied_at or p.last_activity_at)
        if since is not None:
            quiet = max(0, (today - since).days)
        earliest = {
            "opp_id": opp.id,
            "title": redact_text(opp.title or "Untitled", mapping, enabled=conf),
            "days_quiet": quiet,
            "days_overdue": max(0, (today - d).days),
        }
    return {
        "n": n_apply + len(due),
        "apply": n_apply,
        "follow_up": len(due),
        "earliest": earliest,
    }


async def switched_off_work(session: AsyncSession) -> list[dict[str, Any]]:
    """Open pursuits on a track the operator switched off (R2, D-UI-68). Switching a track off
    stops its scanning and scoring; it never hides the applications or leads already open there.
    One entry per switched-off track that has any: {"track", "name", "open", "apply",
    "follow_up"}, where `apply` and `follow_up` are that track's Act counts."""
    from kestrel.services.tracks import switched_off_tracks

    out: list[dict[str, Any]] = []
    for track in switched_off_tracks():
        n_open = await _count(
            session,
            select(func.count()).select_from(Pursuit)
            .join(Opportunity, Opportunity.id == Pursuit.opportunity_id).where(
                Pursuit.track == track, Pursuit.closed_at.is_(None),
                Pursuit.status.not_in(_TERMINAL), opp_visible_clause(),
            ),
        )
        if not n_open:
            continue
        act = await act_summary(session, track)
        out.append({
            "track": track, "name": words(track)["track_name"], "open": n_open,
            "apply": act["apply"], "follow_up": act["follow_up"],
        })
    return out


# --------------------------------------------------------------------- scoring progress panel
# How long "Scoring finished" stays on Today after the queue empties (D-UI-49).
FINISHED_SHOWN_HOURS = 12
_FINISHED_STATUSES = ("done", "finished", "ok", "complete", "completed", "success")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run_ended_word(status: str | None) -> str:
    """How a run ended in one word: "finished" for any clean end, else the stored status."""
    s = (status or "").lower()
    return "finished" if s in _FINISHED_STATUSES else (s or "finished")


def progress_view(progress: dict[str, Any] | None, *, now: datetime | None = None) -> dict:
    """What Today's progress panel shows, from the scoring_progress dict (D-UI-49).

    `state` is "active" (anything pending or leased: headline, meter, legend, model, the time
    left only when the estimate exists), "finished" (the queue is empty and the last run ended
    within FINISHED_SHOWN_HOURS: "Scoring finished: N scored, M failed"), "failed" (neither, but
    `open_failed` roles sit in Scoring failed, D-UI-56) or "none" (nothing to
    show, which is what the all-zero contract stub gives). The meter widths are percentages of
    `total`, never more than 100 together. `done` is scored plus failed: the roles that have
    left the queue, which is what "S of T roles scored" counts."""
    p = dict(progress or {})
    total = int(p.get("total") or 0)
    scored = int(p.get("scored") or 0)
    failed = int(p.get("failed") or 0)
    set_aside = int(p.get("set_aside") or 0)  # Tier 1 set these aside: they left the queue too
    running = int(p.get("running") or 0)
    waiting = int(p.get("waiting") or 0)
    last = p.get("last_run") or None
    view: dict[str, Any] = {
        "state": "none", "total": total, "scored": scored, "failed": failed,
        "running": running, "waiting": waiting, "done": scored + failed + set_aside,
        "set_aside": set_aside,
        "model": p.get("model"), "eta_minutes": p.get("eta_minutes"), "last_run": None,
        "widths": {"scored": 0.0, "failed": 0.0, "running": 0.0},
        # Roles in Scoring failed now, whatever the latest run says (D-UI-56).
        "open_failed": int(p.get("open_failed") or 0),
    }
    if last:
        ended = _parse_iso(last.get("ended_at"))
        view["last_run"] = {
            "ended_at": last.get("ended_at"),
            "hhmm": _hhmm(ended.isoformat()) if ended else None,
            "status": run_ended_word(last.get("status")),
            "scored": int(last.get("scored") or 0),
            "failed": int(last.get("failed") or 0),
            "recent": bool(
                ended and (now or datetime.now(UTC)) - ended
                <= timedelta(hours=FINISHED_SHOWN_HOURS)
            ),
        }
    if p.get("active") and (running or waiting or total):
        view["state"] = "active"
        base = max(total, scored + failed + set_aside + running + waiting, 1)
        view["widths"] = {
            k: round(100 * v / base, 1)
            for k, v in (("scored", scored), ("failed", failed), ("running", running))
        }
        view["total"] = max(total, scored + failed + set_aside + running + waiting)
    elif view["last_run"] and view["last_run"]["recent"]:
        view["state"] = "finished"
    elif view["open_failed"]:
        view["state"] = "failed"  # no recent run to report, but failures still wait
    return view


# --------------------------------------------------------------------------------- overnight
def _blocker_rule(stage_reason: str | None) -> str | None:
    """Tier 0 writes `blocker:<rule>: <reason>`; the rule name is the readable bucket."""
    if not stage_reason or not stage_reason.startswith("blocker:"):
        return None
    rest = stage_reason[len("blocker:") :]
    return rest.split(":", 1)[0].strip() or None


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


async def ghosting_count(
    session: AsyncSession, track: str | None, *, days: int = 21, today: date | None = None
) -> int:
    """Open pursuits whose last activity is older than the ghosting threshold."""
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    g_stmt = (
        select(func.count())
        .select_from(Pursuit)
        .join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .where(
            opp_visible_clause(),
            Pursuit.closed_at.is_(None),
            Pursuit.status.not_in(_TERMINAL),
            Pursuit.last_activity_at.is_not(None),
            func.substr(Pursuit.last_activity_at, 1, 10) <= cutoff,
        )
    )
    if track:
        g_stmt = g_stmt.where(Pursuit.track == track)
    return await _count(session, g_stmt)


async def overnight(
    session: AsyncSession,
    track: str | None = None,
    *,
    ghosting_days: int = 21,
    today: date | None = None,
) -> dict[str, Any]:
    """What the system did since the most recent run started (or in the last 24 h when no run is
    recorded): sources scanned and new postings, Tier 0 in/kept/blocked with the top two blocker
    rules, Tier 1 in/kept/rejected, Tier 2 evaluated/shortlisted, open quarantine, pursuits past
    the ghosting threshold with no reply, and the run mode. Raw counts, so no n beyond themselves;
    the tier-0 figures come from the run row when it has them and from opportunity rows otherwise.
    """
    track = _track_scope(track)
    today = today or date.today()
    run = await latest_run(session)
    since = _window_start(run)

    # Scrapes since the window opened: sources touched and new postings.
    scr = select(ScrapeRun).where(ScrapeRun.started_at >= since)
    if track:
        scr = scr.join(Source, Source.id == ScrapeRun.source_id).where(Source.track == track)
    scrape_rows = (await session.execute(scr)).scalars().all()
    sources_scanned = len({r.source_id for r in scrape_rows})
    new_postings = sum(int((r.counts or {}).get("new", 0) or 0) for r in scrape_rows)

    # Tier 0. The run row carries the sweep's own counts (both tracks); a per-track view, or a
    # window with no run, is derived from the opportunities first seen in the window.
    # Only when the run spans the whole window; a short cycle's counts would undercount it.
    spans = run is not None and run.started_at == since
    # The run row's own counts include every company, so with a hidden company they are not used.
    t0_counts = (run.counts or {}).get("tier0") if spans and not await any_hidden(session) else None
    if track is None and isinstance(t0_counts, dict) and "seen" in t0_counts:
        t0 = {
            "in": int(t0_counts.get("seen", 0) or 0),
            "kept": int(t0_counts.get("pass", 0) or 0),
            "blocked": int(t0_counts.get("reject", 0) or 0),
        }
    else:
        seen_stmt = select(Opportunity.stage, func.count()).where(
            Opportunity.first_seen_at >= since, Opportunity.stage != "new",
            opp_visible_clause(),
        )
        if track:
            seen_stmt = seen_stmt.where(Opportunity.track == track)
        by_stage = dict((await session.execute(seen_stmt.group_by(Opportunity.stage))).all())
        blocked = int(by_stage.get("t0_reject", 0))
        kept = sum(int(v) for k, v in by_stage.items() if k != "t0_reject")
        t0 = {"in": kept + blocked, "kept": kept, "blocked": blocked}
    reason_stmt = select(Opportunity.stage_reason).where(
        Opportunity.stage == "t0_reject", Opportunity.first_seen_at >= since,
        opp_visible_clause(),
    )
    if track:
        reason_stmt = reason_stmt.where(Opportunity.track == track)
    rules = Counter(
        rule
        for (reason,) in (await session.execute(reason_stmt)).all()
        if (rule := _blocker_rule(reason))
    )
    t0["reasons"] = [{"rule": r, "n": c} for r, c in rules.most_common(2)]

    # Tiers 1 and 2 from the evaluations written in the window.
    ev_stmt = (
        select(Evaluation.tier, Evaluation.verdict, func.count())
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .where(Evaluation.created_at >= since, Evaluation.tier.in_((1, 2)), opp_visible_clause())
    )
    if track:
        ev_stmt = ev_stmt.where(Evaluation.track == track)
    ev_rows = (await session.execute(ev_stmt.group_by(Evaluation.tier, Evaluation.verdict))).all()
    t1 = {"in": 0, "kept": 0, "rejected": 0}
    t2 = {"evaluated": 0, "shortlisted": 0}
    for tier, verdict, count in ev_rows:
        count = int(count)
        if tier == 1:
            t1["in"] += count
            if verdict == "keep":
                t1["kept"] += count
            elif verdict == "kill":
                t1["rejected"] += count
        else:
            t2["evaluated"] += count
            if verdict in SHORTLIST_VERDICTS:
                t2["shortlisted"] += count

    # Quarantine rows stay as history; open means a failure a later score has not superseded
    # and the operator still has to act on (web.queries.quarantine_needs_you_clause, D-UI-70).
    quarantine_open = await open_quarantine_count(session, track)

    ghosting = await ghosting_count(session, track, days=ghosting_days, today=today)

    return {
        "run": (
            {
                "id": run.id,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "mode": run.mode,
            }
            if run is not None
            else None
        ),
        "since": since,
        "window_hours": _WINDOW_HOURS,
        "sources_scanned": sources_scanned,
        "new_postings": new_postings,
        "tier0": t0,
        "tier1": t1,
        "tier2": t2,
        "quarantine_open": quarantine_open,
        "ghosting": {"n": ghosting, "days": ghosting_days},
        "mode": run.mode if run is not None else None,
    }


# ------------------------------------------------------------------------------------- honest
async def honest_line(session: AsyncSession, track: str | None = None) -> dict[str, Any]:
    """The honest analytics line, exactly as the dashboard carried it: whether the score is
    discriminating, with the outcome count it rests on. Never raises; the landing page must not
    go down with analytics. `meter` is progress toward a sample that can carry a verdict."""
    try:
        from kestrel.analytics.metrics import collect_metrics

        m = await collect_metrics(session, track or "employment")
        svo = m["score_vs_outcome"]
        n = int(svo["n_outcomes"])
        return {
            "n": n,
            "discriminating": svo["discriminating"],
            "headline": svo["headline"],
            "insufficient": n < MIN_N,
            "meter": min(100, round(100 * n / _HONEST_SAMPLE_TARGET)),
            "target": _HONEST_SAMPLE_TARGET,
        }
    except Exception as exc:  # analytics must never take down the landing page
        return {"error": str(exc), "n": 0, "discriminating": None, "meter": 0}


# --------------------------------------------------------------------------------------- asks
def _ask(
    key: str, title: str, line: str, href: str, button: str, priority: int,
    *, dismissable: bool = False,
) -> dict[str, Any]:
    return {
        "key": key, "title": title, "line": line, "href": href, "button": button,
        "priority": priority, "dismissable": dismissable,
    }


# Asks the operator can wave away for good (a settings row, like the guide strips). Every other
# ask clears itself when its cause is fixed, so it is never dismissable.
ASKS_DISMISSED_KEY = "ui.asks_dismissed"
DISMISSABLE_ASKS: tuple[str, ...] = ("taste",)
# The calibrate ask appears once enough roles are scored to rate (D-UI-49 mockup section 1).
TASTE_MIN_SCORED = 15


async def dismissed_asks(session: AsyncSession) -> list[str]:
    row = await session.get(Setting, ASKS_DISMISSED_KEY)
    value = row.value if row is not None else None
    return [str(k) for k in value] if isinstance(value, list) else []


async def dismiss_ask(session: AsyncSession, key: str) -> bool:
    """Record that the operator dismissed a dismissable ask. False for any other key."""
    if key not in DISMISSABLE_ASKS:
        return False
    keys = await dismissed_asks(session)
    if key not in keys:
        keys.append(key)
        row = await session.get(Setting, ASKS_DISMISSED_KEY)
        if row is None:
            session.add(Setting(key=ASKS_DISMISSED_KEY, value=keys))
        else:
            row.value = keys
        await session.commit()
    return True


async def _scored_roles(session: AsyncSession, track: str | None) -> int:
    """Visible roles with a finished full read (Tier 2 or above, a fit verdict)."""
    stmt = (
        select(func.count(func.distinct(Evaluation.opportunity_id)))
        .select_from(Evaluation)
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .where(
            Evaluation.tier >= 2,
            Evaluation.verdict.in_(("apply", "pursue", "consider", "pass")),
            Opportunity.archived_at.is_(None),
            opp_visible_clause(),
        )
    )
    if track:
        stmt = stmt.where(Opportunity.track == track)
    return await _count(session, stmt)


async def _a_run_finished(session: AsyncSession) -> bool:
    return bool(
        await _count(
            session, select(func.count()).select_from(Run).where(Run.finished_at.is_not(None))
        )
    )


def _operator_profile_tracks() -> set[str] | None:
    """Track blocks in the operator's own profile.yml, or None when only the shipped example
    exists. The example is fiction and does not count as a profile."""
    import yaml

    from kestrel.services.backup import profile_path

    path = profile_path()
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return set()
    return {t for t in TRACKS if isinstance(data, dict) and isinstance(data.get(t), dict)}


def _hosted_key_missing(settings) -> str | None:
    """The env var name of a hosted backend with no key, when hosted mode is on. None otherwise."""
    from kestrel.llm.providers import secret_present

    llm = settings.llm
    if not llm.hosted_mode.enabled:
        return None
    for tier in llm.hosted_tiers.values():
        backend = llm.backends.get(tier.backend)
        if backend is None or not backend.external:
            continue
        if not (backend.api_key or secret_present(backend.api_key_env)):
            return backend.api_key_env or tier.backend
    return None


async def _derived_models_missing(settings) -> list[str]:
    """The derived models setup_state reports missing from Ollama, when Ollama answered. An
    unreachable Ollama is the setup page's finding, not a missing-model ask; and the module is
    written concurrently, so its absence or any failure in it skips the ask rather than guessing.
    No tier on Ollama (hosted only, D-SETUP-8): nothing to build, so no ask and no probe.
    """
    import asyncio

    from kestrel.llm.profiles import ollama_needed

    if not ollama_needed(settings):
        return []
    try:
        from kestrel.services import setup_state
    except ImportError:
        return []
    try:
        state = await asyncio.to_thread(setup_state.assess, settings)
    except Exception as exc:  # a setup probe must never take the landing page down
        log.warning("today_setup_probe_failed", error=str(exc))
        return []
    if getattr(state, "ollama_version", None) is None:
        return []
    return [str(m) for m in (getattr(state, "missing_derived", None) or [])]


def failing_sources() -> tuple:
    """WHERE clauses for a search the operator must hear about (D-UI-33): it failed its last
    run and is still on, or the runner switched it off after three failures. A source the
    operator retired, or switched off before it reached three, is not raised again."""
    return (
        Source.consecutive_failures > 0,
        or_(Source.enabled.is_(True), Source.consecutive_failures >= 3),
        Source.origin != "retired",
    )


async def system_asks(
    session: AsyncSession, settings, track: str | None = None
) -> list[dict[str, Any]]:
    """What the system wants from the operator, highest priority first: a missing profile,
    missing derived models, hosted mode with no key, confidential mode with no redaction map,
    quarantine items waiting, and calibration labels short of the few-shot and reranker floors
    (n of N). Each ask is a title, one line, a link, a button label, and a priority."""
    from kestrel.learning.capture import labeled_examples
    from kestrel.web.redact import confidential_is_inert

    track = _track_scope(track)
    tracks = (track,) if track else TRACKS
    asks: list[dict[str, Any]] = []

    failing = (
        await session.execute(select(Source).where(*failing_sources()).order_by(Source.id))
    ).scalars().all()
    if failing:
        # A search that fails must say so where the operator starts the day (D-UI-33).
        first = failing[0]
        why = (
            await session.execute(
                select(ScrapeRun.error).where(ScrapeRun.source_id == first.id)
                .order_by(ScrapeRun.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or ""
        why = why.strip()
        asks.append(
            _ask(
                "searches_failing",
                f"{len(failing)} search{'es' if len(failing) != 1 else ''} failing",
                (f"{first.name}: {why[:160]}" if why else
                 f"{first.name} failed its last run.") + " Nothing new arrives from it until "
                "this is fixed.",
                "/sources", "See Sources", 0,
            )
        )

    present = _operator_profile_tracks()
    if present is None:
        asks.append(
            _ask(
                "profile", "Create your profile",
                "Kestrel is scoring against the shipped example profile, which is fiction. "
                "Fill in your profile: who you are, what you want, where you will work.",
                "/profile", "Open Profile", 1,
            )
        )
    else:
        for t in tracks:
            if t not in present:
                asks.append(
                    _ask(
                        f"profile:{t}", f"No {t} profile",
                        f"profile.yml has no {t} block, so that track scores against nothing "
                        "of yours.",
                        "/backup", "Import", 1,
                    )
                )

    missing = await _derived_models_missing(settings)
    if missing:
        asks.append(
            _ask(
                "models", "Set up Kestrel's models",
                "Missing: " + ", ".join(missing) + ". Nothing is scored until they exist.",
                "/models", "Build", 1,
            )
        )

    env = _hosted_key_missing(settings)
    if env:
        asks.append(
            _ask(
                "hosted_key", "Add the hosted key",
                f"Hosted mode is on but {env} is not set, so hosted runs cannot start.",
                "/settings", "Add key", 2,
            )
        )

    if await confidential_is_inert(session):
        from kestrel.docs.current_employer import choose_href

        asks.append(
            _ask(
                "redaction", "Choose your current employer",
                "Confidential mode is on but no role is marked as your current employer yet, so "
                "nothing is redacted.",
                await choose_href(session), "Choose", 2,
            )
        )
    else:
        from kestrel.web.redact import confidential

        enabled, emap = await confidential(session)
        if enabled and emap:
            from kestrel.docs.current_employer import is_confirmed

            # D-PRIV-1: separate from "inert". Kestrel is hiding something it inferred, or the
            # operator never confirmed the choice: keep asking until they do.
            if any(e.inferred for e in emap.employers) or not await is_confirmed(session):
                asks.append(
                    _ask(
                        "redaction_confirm", "Confirm the employer Kestrel is hiding",
                        "Confidential mode hides a role that looks like your current job. "
                        "Confirm it or choose another.",
                        "/settings#core", "Confirm", 2,
                    )
                )

    # Only failures that still need the operator: a role scored after it failed is resolved
    # (D-UI-70), and one already queued to score again is Being scored (D-UI-56).
    fails = await failure_summary(session)
    if fails.total:
        # Parked, not pending: quarantine is never something to wait for (D-UI-37). A failed
        # score-again is named apart: its earlier score stands (D-UI-70).
        why = quarantine_why(await quarantine_kinds(session))
        failed = fails.scoring + fails.other
        again = fails.rescore
        if failed and again:
            title = (f"{_plural(failed, 'posting', 'postings')} {_need(failed)} another try; "
                     f"score again failed for {again}")
        elif again:
            title = f"Score again failed for {_plural(again, 'posting', 'postings')}"
        else:
            title = f"{_plural(failed, 'posting', 'postings')} {_need(failed)} another try"
        stands = (" Their earlier scores stand." if again > 1
                  else " Its earlier score stands." if again else "")
        asks.append(
            _ask(
                "quarantine", title,
                f"{why}{stands} Retry them, or pick another model on Models.",
                "/quarantine", "Fix", 3,
            )
        )

    few = settings.learning.min_labels_for_few_shot
    rr = settings.learning.min_labels_for_reranker
    counts = {t: len(await labeled_examples(session, t)) for t in tracks}
    short = [t for t in tracks if counts[t] < few]
    # Teach Kestrel your taste: once, after the first finished run that left enough scored roles
    # to rate, until the operator dismisses it. It replaces the bare label-count ask meanwhile.
    taste = (
        bool(short)
        and "taste" not in await dismissed_asks(session)
        and await _a_run_finished(session)
        and await _scored_roles(session, track) >= TASTE_MIN_SCORED
    )
    if taste:
        asks.append(
            _ask(
                "taste", "Teach Kestrel your taste.",
                f"Rate {TASTE_MIN_SCORED} scored {words(short[0])['roles']} and it starts ranking "
                "the way you would. "
                "Takes about five minutes.",
                f"/calibrate?track={short[0]}", "Rate roles", 4, dismissable=True,
            )
        )
    for t in tracks:
        n = counts[t]
        if n < few and taste:
            continue
        if n < few:
            need = few - n
            asks.append(
                _ask(
                    f"labels:{t}", f"{need} more {'rating' if need == 1 else 'ratings'}",
                    f"{need} more and Kestrel starts learning from your examples for {t} "
                    f"({n} of {few}).",
                    f"/calibrate?track={t}", "Rate roles", 4,
                )
            )
        elif n < rr:
            need = rr - n
            asks.append(
                _ask(
                    f"labels:{t}", f"{need} more ratings",
                    f"{need} more and Kestrel can learn your ranking for {t} ({n} of {rr}).",
                    f"/calibrate?track={t}", "Rate roles", 5,
                )
            )

    return sorted(asks, key=lambda a: a["priority"])


# ---------------------------------------------------------------------------- happening now
_LENGTH_MARKS = ("max_tokens", "finish_reason: length", "before the structured object was "
                 "complete", "context window", "context length", "num_ctx", "truncat")
_SCHEMA_MARKS = ("schema", "invalid json", "validation", "not valid", "enum", "required",
                 "expecting", "json")


def quarantine_kind(error: str | None) -> str:
    """Why one stored failure happened, from its error text: `length` (the reply hit its token
    or context budget), `schema` (an answer came back in the wrong shape), else `other`."""
    text = (error or "").lower()
    if any(m in text for m in _LENGTH_MARKS):
        return "length"
    if any(m in text for m in _SCHEMA_MARKS):
        return "schema"
    return "other"


async def quarantine_kinds(session: AsyncSession) -> Counter[str]:
    """Why the open failures happened (the same rows open_quarantine_count counts)."""
    return Counter(quarantine_kind(e) for e in await open_quarantine_errors(session))


def quarantine_why(kinds: Counter[str] | None) -> str:
    """One sentence on why the parked postings failed, from what was actually stored."""
    kinds = Counter({k: v for k, v in (kinds or Counter()).items() if v})
    if not kinds:
        return "Open them to see why."
    if set(kinds) == {"length"}:
        return "The model ran out of room."
    if set(kinds) == {"schema"}:
        return "The model's answer was not in the required format."
    if set(kinds) == {"other"}:
        return "Open them to see why."
    parts = []
    if kinds["length"]:
        parts.append(f"{kinds['length']} ran out of room")
    if kinds["schema"]:
        parts.append(f"{kinds['schema']} came back in the wrong format")
    if kinds["other"]:
        parts.append(f"{kinds['other']} failed for another reason")
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] + "."


def quarantine_line(n: int, kinds: Counter[str] | None = None) -> str:
    """Failed postings are separate from pending work: they never clear by waiting. The reason
    follows the stored errors (a budget overrun and a malformed answer need different fixes)."""
    return (
        f"{_plural(n, 'posting', 'postings')} {_need(n)} another try. {quarantine_why(kinds)} "
        "Retry them, or pick another model on Models."
    )


async def latest_scan(session: AsyncSession) -> dict[str, Any] | None:
    """The most recent finished scan: from the desktop loop's Run rows (their `scrape` counts,
    D-UI-34) when one exists, else the newest scrape run with no new-posting total. `id` is the
    Run id, the event the scan-finished toast keys on; None for the fallback. Found by its
    `scrape` key in SQL, however many scoring-only runs came after it. A scan in which every
    search crashed counts (completed 0, errors N): it finished, with errors."""
    run = (
        await session.execute(
            select(Run)
            .where(func.json_type(Run.counts, "$.scrape") == "object")
            .order_by(Run.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if run is not None:
        scrape = (run.counts or {}).get("scrape") or {}
        return {
            "id": run.id,
            "at": scrape.get("finished_at") or run.started_at,
            "new": int(scrape.get("new", 0) or 0),
            "searches": int(scrape.get("completed", 0) or 0),
            "errors": int(scrape.get("errors", 0) or 0),
        }
    last = (
        await session.execute(
            select(func.max(func.coalesce(ScrapeRun.finished_at, ScrapeRun.started_at)))
        )
    ).scalar_one_or_none()
    if last is None:
        return None
    return {"id": None, "at": last, "new": None, "searches": None, "errors": 0}


def _hhmm(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    from kestrel.date_display import format_display

    return format_display(dt, kind="time", time_style="24h", local=True)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _need(n: int) -> str:
    """"needs" for one, "need" for any other count (the totals read "need another try")."""
    return "needs" if n == 1 else "need"


async def happening_now(
    session: AsyncSession, *, mode: str, paused: bool = False
) -> dict[str, Any]:
    """One plain line on what the worker is doing and whether the operator has to act
    (D-UI-37): scanning, scoring, paused, or idle with the last scan. Quarantine gets its own
    line because it never clears by waiting. Reads the task queue only; no model, no network."""
    from kestrel.web.queries import live_activity

    activity = await live_activity(session)
    tasks = activity.get("tasks") or {}

    def _open(kind: str) -> int:
        t = tasks.get(kind, {})
        return int(t.get("pending", 0)) + int(t.get("leased", 0))

    n_scan, n1, n2, n_embed = _open("scrape"), _open("t1"), _open("t2"), _open("embed")
    if paused:
        state = "paused"
        line = "Paused. Nothing runs until you resume the worker from the tray."
    elif n_scan:
        state = "scanning"
        line = f"Scanning: {_plural(n_scan, 'search', 'searches')} to go. Nothing to do yet."
        if n1 + n2:
            line += f" {_plural(n1 + n2, 'posting', 'postings')} queued for scoring after it."
    elif n1 + n2:
        state = "scoring"
        tiers = (
            "the quick screen and the full review" if n1 and n2
            else ("the quick screen" if n1 else "the full review")
        )
        line = (
            f"Scoring: {_plural(n1 + n2, 'posting', 'postings')} waiting for {tiers} on {mode} "
            "models. Nothing to do; results land in Pipeline."
        )
    elif n_embed:
        state = "scoring"
        line = (
            f"Scoring: {_plural(n_embed, 'posting', 'postings')} being prepared for scoring. "
            "Nothing to do; results land in Pipeline."
        )
    else:
        state = "idle"
        scan = await latest_scan(session)
        at = _hhmm(scan["at"]) if scan else None
        if scan is None or at is None:
            line = "Idle. No scan yet; press Run now to run the first one."
        elif scan.get("errors"):
            e = scan["errors"]
            line = (
                f"Idle. Last search {at}: {scan['new'] or 0} new, "
                f"{_plural(e, 'source', 'sources')} had a problem. See Sources."
            )
        elif scan["new"] is None:
            line = f"Idle. Last scan {at}. Nothing is running; anything below is yours."
        else:
            line = (
                f"Idle. Last scan {at} found {scan['new']} new. Nothing is running; anything "
                "below is yours."
            )
    q = int(activity.get("quarantined") or 0)
    return {
        "state": state,
        "line": line,
        "quarantine": quarantine_line(q, await quarantine_kinds(session)) if q else None,
        "quarantined": q,
    }
