"""The live run summary (D-UI-58, D-UI-59): what the current run has found, screened and scored so
far, or how the last one ended, for Today's run panel, the Runs page Latest run panel and the
status rail. Sibling of kestrel.analytics.progress, whose `scoring_progress` shape is unchanged.

`run_summary(session, track)` returns:

    {
      "has_run": bool,            # False on a fresh install (or a track that never ran)
      "active": bool,             # the worker runs (not paused) and live work waits or runs
      "paused": bool,             # the worker is paused: no live dot, no stall warning
      "finishing": int,           # paused: tasks still under a live lease (they finish)
      "run_id": int | None, "started_at": str | None, "ended_at": str | None,
      "status": str | None,       # the Run row's status ("running" while active)
      "stage": str,               # searching | quick_screen | full_review | finishing |
                                  #   paused | idle
      "searches_total", "searches_done", "searches_failed", "searches_partial",
      "searches_cut_off": int,    # cut off: started, never finished, run closed (R11)
      "current_search": str | None,  # "Operations Analyst on Indeed and Glassdoor" (redacted)
      "problems": [{"name", "why"}],  # searches that failed or partly failed (R4)
      "found_new": int,           # new roles this run's searches stored (signals: promoted
                                  #   plus postings, R5)
      "set_aside": int,           # of those, the ones the rules check set aside
      "set_aside_rules": [{"word", "n"}],  # by fit.rule_word ("location", "title")
      "quick_screen_done", "quick_screen_total", "quick_screen_kept": int,
      "rated": int,               # roles a full review (Tier 2) finished this run
      "to_rate": int,             # roles that passed the quick screen and wait for or got one
      "failed": int,              # full review failed this run and was not redone (R6)
      "rating_now": int, "waiting": int,  # full reviews leased now / still to start
      "groups": {"strong", "good", "maybe", "not_fit", "your_call": int},  # over `rated`
      "groups_by_track": {track: {...same keys...}},
      "last_activity_at": str | None,  # newest touch by the run on ANY track (stall check)
      "recent_events": [{"at", "text", "tone"}],  # newest first, at most 10
    }

Everything is derived from rows that already exist (Run, ScrapeRun, Opportunity, Evaluation,
Quarantine, Task, one setting row stamped on resume), so a restart mid-run loses nothing (hard
rule 5) and there is no event table.

The window (R3): active only while the worker is not paused AND a scrape, quick screen or full
review task waits or runs on a track that is switched on (a switched-off track's tasks are held,
D-UI-68), or while the desktop Run row is still open (it then reads "Finishing up" at least,
never finished). Active, the window is the open desktop Run's start (a resume never cuts it),
else the earliest live task's creation, but never earlier than the newest Run start or the
latest resume, so a days-old pending task cannot stretch "this run". Paused, the same window
reads "Pausing: finishing N" while leased tasks finish, then "Paused". Idle, the newest
finished Run that started before the track's latest scrape or evaluation; a track that never ran
has no last run (R10), it never borrows another track's.

Counts are filtered by `track` (None: every track). Today and the rail call it with None (R1):
one run scans every track, so its progress is one figure; only the fit groups are per track
(`groups_by_track`). `last_activity_at` answers "is the worker alive" for the whole run. Titles
and search names are redacted under confidential mode with one map per request (R7, R9). Polled
every 5 s while active: about ten small indexed statements over the columns they need, no model
or network call.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import (
    Evaluation,
    Opportunity,
    Quarantine,
    Run,
    ScrapeRun,
    Setting,
    Source,
    Task,
)
from kestrel.web.fit import (
    GOOD,
    MAYBE,
    NOT_FIT,
    STRONG,
    YOUR_CALL,
    failure_words,
    fit_for,
    rule_word,
)
from kestrel.web.words import words

APP_RUN_NAME = "desktop"  # runlog.APP_RUN_NAME
# Stamped by POST /api/worker/resume: the window never reaches back past it (R3).
RESUMED_KEY = "worker.resumed_at"
_RUN_TASKS = ("scrape", "t1", "t2")
_WORK_TASKS = ("scrape", "embed", "t1", "t2")
EVENTS_SHOWN = 10
# A run that is open with no new row for this long reads as stalled on the Runs page.
STALL_MINUTES = 10
GROUP_KEYS = ("strong", "good", "maybe", "not_fit", "your_call")
GROUP_WORDS = {
    "strong": "Strong fit", "good": "Good fit", "maybe": "Maybe", "not_fit": "Not a fit",
    "your_call": "Your call",
}
# The Pipeline group each fit word lands in (kestrel.web.fit): Worth a look holds strong, good
# and your call, so "added N to Worth a look" sums them.
WORTH_KEYS = ("strong", "good", "your_call")
_FIT_KEY = {STRONG: "strong", GOOD: "good", MAYBE: "maybe", NOT_FIT: "not_fit",
            YOUR_CALL: "your_call"}
_FIT_TONE = {"strong": "good", "good": "good", "maybe": "warn", "your_call": "warn",
             "not_fit": "idle"}

# Boards as the operator reads them. JobSpy sources name theirs in `site_name`; every other kind
# is one board or feed.
_SITE_WORDS = {
    "indeed": "Indeed", "google": "Google", "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter", "bayt": "Bayt", "naukri": "Naukri",
}
_KIND_WORDS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
    "smartrecruiters": "SmartRecruiters", "rippling": "Rippling", "adp": "ADP Workforce Now",
    "sec_edgar": "SEC filings", "sam_gov": "SAM.gov",
    "rss": "a feed", "pe_portfolio": "PE portfolio pages", "manual": "a pasted posting",
}


def _empty() -> dict[str, Any]:
    return {
        "has_run": False, "active": False, "paused": False, "run_id": None, "started_at": None,
        "ended_at": None, "status": None, "stage": "idle",
        "searches_total": 0, "searches_done": 0, "searches_failed": 0, "searches_partial": 0,
        "searches_cut_off": 0, "current_search": None, "problems": [],
        "found_new": 0, "set_aside": 0, "set_aside_rules": [],
        "quick_screen_done": 0, "quick_screen_total": 0, "quick_screen_kept": 0,
        "rated": 0, "to_rate": 0, "failed": 0, "rating_now": 0, "waiting": 0,
        "groups": dict.fromkeys(GROUP_KEYS, 0), "groups_by_track": {},
        "last_activity_at": None, "recent_events": [], "finishing": 0,
    }


def _join_and(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def board_label(kind: str | None, config: dict | None) -> str:
    """The board a search runs on, in words: "Indeed and Glassdoor", "Greenhouse"."""
    if kind == "jobspy":
        sites = (config or {}).get("site_name") or []
        if isinstance(sites, str):
            sites = [sites]
        named = [_SITE_WORDS.get(str(s), str(s).replace("_", " ").title()) for s in sites]
        return _join_and(named) or "job boards"
    return _KIND_WORDS.get(kind or "", (kind or "a source").replace("_", " ").title())


def search_term(name: str | None, config: dict | None) -> str:
    """What the search looks for: its search term when it has one, else the search's name."""
    term = str((config or {}).get("search_term") or "").strip()
    return term or (name or "a search")


def search_label(name: str | None, kind: str | None, config: dict | None) -> str:
    """"Operations Analyst on Indeed and Glassdoor"."""
    return f"{search_term(name, config)} on {board_label(kind, config)}"


def problem_words(error: str | None, status: str | None) -> str:
    """What went wrong with a search, in a few words (R4): a 429 is a rate limit, a 5xx a
    server error. Never the raw error text, which can carry a URL or an org name."""
    e = (error or "").lower()
    if "429" in e or "too many requests" in e or "rate limit" in e or "rate-limit" in e:
        return "was rate-limited"
    if any(code in e for code in (" 500", " 502", " 503", " 504", "server error")) or any(
        e.startswith(code) for code in ("500", "502", "503", "504")
    ):
        return "returned a server error"
    if "timed out" in e or "timeout" in e:
        return "timed out"
    if e.startswith("disabled"):
        return "was switched off after a setup problem"
    if status == "failed":
        return "failed"
    return "had a problem"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _opp_id():
    return func.json_extract(Task.payload, "$.opportunity_id")


def _src_id():
    return func.json_extract(Task.payload, "$.source_id")


def _live_where(*types: str):
    """Live tasks of these types that the worker may run: the queue's own in-flight predicate
    (review finding 1), so a switched-off track's held tasks (D-UI-68) are not work in progress
    and every "is something running" count agrees with what a lease would take."""
    from kestrel.db.base import utcnow_iso
    from kestrel.queue.queue import in_flight

    return and_(Task.type.in_(types), in_flight(utcnow_iso()))


def _now_iso() -> str:
    from kestrel.db.base import utcnow_iso

    return utcnow_iso()


def _is_paused() -> bool:
    try:
        from kestrel.web.app import is_paused
    except ImportError:  # pragma: no cover - the web app is always importable in this package
        return False
    return is_paused()


async def track_touched(session: AsyncSession, track: str) -> bool:
    """Whether any search or evaluation ever ran on this track (R10)."""
    ev = select(Evaluation.id).where(Evaluation.track == track).limit(1)
    sr = (
        select(ScrapeRun.id).join(Source, Source.id == ScrapeRun.source_id)
        .where(Source.track == track).limit(1)
    )
    row = (await session.execute(select(ev.exists(), sr.exists()))).one()
    return bool(row[0] or row[1])


async def _window(
    session: AsyncSession, track: str | None, paused: bool
) -> tuple[Run | None, str | None, bool]:
    """(the Run row, the window start, open). `open` is True while the run has not ended
    (active or paused). See the module docstring."""
    open_run = (
        await session.execute(
            select(Run)
            .where(Run.name == APP_RUN_NAME, Run.status == "running")
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    first_live, newest_start, resumed = (
        await session.execute(
            select(
                select(func.min(Task.created_at)).where(_live_where(*_RUN_TASKS))
                .scalar_subquery(),
                select(func.max(Run.started_at)).scalar_subquery(),
                select(Setting.value).where(Setting.key == RESUMED_KEY).scalar_subquery(),
            )
        )
    ).one()
    resumed = resumed if isinstance(resumed, str) else None
    if first_live or open_run is not None:
        # An open run is always the window and always reads as running (review finding 5):
        # "Finishing up" while it embeds or sweeps the rules with no scrape or tier task live,
        # never "Last run · finished". A resume never cuts an open run's window (finding 2), so
        # its counts cannot drop on resume; the resume stamp only bounds a window with no open
        # run, where a days-old pending task must not stretch "this run" (R3).
        if open_run is not None:
            start = open_run.started_at
        else:
            start = max(first_live, newest_start or "", resumed or "")
        return open_run, start, True
    # Idle: the newest finished run that touched this track (any track when None).
    ev = select(func.max(Evaluation.created_at))
    sr = select(func.max(ScrapeRun.started_at))
    if track:
        ev = ev.where(Evaluation.track == track)
        sr = sr.join(Source, Source.id == ScrapeRun.source_id).where(Source.track == track)
    last_ev, last_sr = (await session.execute(select(ev.scalar_subquery(),
                                                     sr.scalar_subquery()))).one()
    touched = max((t for t in (last_ev, last_sr) if t), default=None)
    if track and touched is None:
        return None, None, False  # R10: a track that never ran has no last run
    stmt = select(Run).order_by(Run.started_at.desc(), Run.id.desc())
    if touched:
        stmt = stmt.where(Run.started_at <= touched)
    run = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if run is None:
        return None, None, False
    return run, run.started_at, False


def _in_window(col, start: str, end: str | None):
    return and_(col >= start, col <= end) if end else col >= start


async def run_summary(
    session: AsyncSession, track: str | None = None, *, paused: bool | None = None
) -> dict[str, Any]:
    """The run summary dict (module docstring). `paused` defaults to the worker's own state."""
    from kestrel.web.redact import confidential_once, redact_text

    track = track if track in ("employment", "consulting") else None
    paused = _is_paused() if paused is None else paused
    out = _empty()
    run, start, is_open = await _window(session, track, paused)
    if start is None:
        return out
    active = is_open and not paused
    end = None if is_open else (run.finished_at if run is not None else None)
    out.update(
        has_run=True, active=active, paused=is_open and paused,
        run_id=run.id if run is not None else None, started_at=start, ended_at=end,
        status=run.status if run is not None else "running",
    )
    events: list[dict[str, Any]] = []
    conf, mapping = await confidential_once(session)

    def redact(text: str | None) -> str:
        return redact_text(text or "Untitled", mapping, enabled=conf) or "Untitled"

    # ---------------------------------------------------------------- searches (ScrapeRun)
    stmt = (
        select(ScrapeRun.id, ScrapeRun.started_at, ScrapeRun.finished_at, ScrapeRun.status,
               ScrapeRun.error, ScrapeRun.counts, Source.name, Source.kind, Source.config,
               Source.track)
        .join(Source, Source.id == ScrapeRun.source_id)
        .where(_in_window(ScrapeRun.started_at, start, end))
        .order_by(ScrapeRun.started_at, ScrapeRun.id)
    )
    if track:
        stmt = stmt.where(Source.track == track)
    run_ids: list[int] = []
    last_scrape_end: str | None = None
    for sr_id, sr_start, sr_end, status, error, counts, name, kind, config, strack in (
        await session.execute(stmt)
    ).all():
        run_ids.append(sr_id)
        term = redact(search_term(name, config))
        board = board_label(kind, config)
        if sr_end is None:
            if is_open:
                if active:
                    out["current_search"] = f"{term} on {board}"
                continue
            # The run closed with this search unfinished: the app stopped mid-search (R11).
            out["searches_cut_off"] += 1
            events.append({"at": sr_start, "tone": "warn",
                           "text": f"Search for '{term}' on {board} was cut off"})
            continue
        out["searches_done"] += 1
        counts = counts or {}
        if "new" in counts:
            new = int(counts.get("new", 0) or 0)
        else:  # a signal source (EDGAR, SAM.gov) counts what it promoted and stored (R5)
            new = int(counts.get("promoted", 0) or 0) + int(counts.get("postings", 0) or 0)
        out["found_new"] += new
        last_scrape_end = max(last_scrape_end or "", sr_end)
        w = words(strack)
        if status in ("failed", "partial"):
            why = problem_words(error, status)
            out["searches_failed" if status == "failed" else "searches_partial"] += 1
            label = board if kind != "jobspy" else f"{term} on {board}"
            out["problems"].append({"name": label, "why": why})
            events.append({"at": sr_end, "tone": "bad" if status == "failed" else "warn",
                           "text": f"Search for '{term}' on {board} {why}; it is retried "
                                   "next run"})
        if status != "failed" or new:
            found = (f"{new} {_plural(new, w['role'], w['roles'])}" if new
                     else f"no new {w['roles']}")
            events.append({"at": sr_end, "tone": "good" if new else "idle",
                           "text": f"Found {found} on {board} for '{term}'"})

    # ------------------------------------------------------------------- live tasks, by type
    live_scrape = select(func.count()).select_from(Task).where(_live_where("scrape"))
    if track:
        live_scrape = live_scrape.join(Source, Source.id == _src_id()).where(
            Source.track == track)
    live_score = (
        select(Task.type, Task.status, func.count())
        .select_from(Task)
        .join(Opportunity, Opportunity.id == _opp_id())
        .where(_live_where("t1", "t2"))
        .group_by(Task.type, Task.status)
    )
    if track:
        live_score = live_score.where(Opportunity.track == track)
    n_scrape = int((await session.execute(live_scrape)).scalar_one()) if is_open else 0
    live: dict[tuple[str, str], int] = (
        {(t, st): int(n) for t, st, n in (await session.execute(live_score)).all()}
        if is_open else {}
    )
    n_t1 = live.get(("t1", "pending"), 0) + live.get(("t1", "leased"), 0)
    t2_leased = live.get(("t2", "leased"), 0)
    n_t2 = live.get(("t2", "pending"), 0) + t2_leased
    out["searches_total"] = out["searches_done"] + n_scrape
    if out["current_search"] and not n_scrape:
        out["searches_total"] += 1  # a CLI scan in another process holds no task here

    # --------------------------------------------------------------- rules check (Tier 0)
    if run_ids:
        reasons = (
            await session.execute(
                select(Opportunity.stage_reason).where(
                    Opportunity.scrape_run_id.in_(run_ids), Opportunity.stage == "t0_reject"
                )
            )
        ).scalars().all()
        out["set_aside"] = len(reasons)
        by_rule = Counter(rule_word([r]) for r in reasons)
        out["set_aside_rules"] = [{"word": w_, "n": n} for w_, n in by_rule.most_common()]
        if reasons and last_scrape_end:
            events.append({"at": last_scrape_end, "tone": "idle",
                           "text": f"Rules set aside {len(reasons)}"})

    # -------------------------------------------------------------- quick screen (Tier 1)
    t1 = select(
        func.count(func.distinct(Evaluation.opportunity_id)),
        func.count(func.distinct(
            case((Evaluation.verdict == "keep", Evaluation.opportunity_id)))),
        func.max(Evaluation.created_at),
    ).where(Evaluation.tier == 1, _in_window(Evaluation.created_at, start, end))
    if track:
        t1 = t1.where(Evaluation.track == track)
    done1, kept1, at1 = (await session.execute(t1)).one()
    out["quick_screen_done"], out["quick_screen_kept"] = int(done1), int(kept1)
    out["quick_screen_total"] = int(done1) + n_t1
    if done1:
        events.append({"at": at1, "tone": "idle",
                       "text": f"Quick screen kept {kept1} of {done1}"})

    # --------------------------------------------------------------- full review (Tier 2)
    t2 = (
        select(Evaluation.opportunity_id, Evaluation.created_at, Evaluation.tier,
               Evaluation.verdict, Evaluation.score, Evaluation.weighted_score,
               Evaluation.needs_review, Opportunity.title, Opportunity.stage, Opportunity.track)
        .join(Opportunity, Opportunity.id == Evaluation.opportunity_id)
        .where(Evaluation.tier == 2, _in_window(Evaluation.created_at, start, end))
        .order_by(Evaluation.created_at, Evaluation.id)
    )
    if track:
        t2 = t2.where(Evaluation.track == track)
    latest: dict[int, Any] = {}
    for row in (await session.execute(t2)).all():
        latest[row.opportunity_id] = row
    # A Tier 2 failure, or a Tier 3 failure of a role that has no Tier 2 evaluation at all: a
    # failed Tier 3 read of a scored role leaves its Tier 2 score standing (R6).
    has_t2 = (
        select(Evaluation.id)
        .where(Evaluation.opportunity_id == Quarantine.opportunity_id, Evaluation.tier == 2)
        .correlate(Quarantine)
        .exists()
    )
    q = (
        select(Quarantine.opportunity_id, Quarantine.created_at, Quarantine.error,
               Opportunity.title)
        .join(Opportunity, Opportunity.id == Quarantine.opportunity_id)
        .where(or_(Quarantine.tier == 2, and_(Quarantine.tier == 3, ~has_t2)),
               _in_window(Quarantine.created_at, start, end))
        .order_by(Quarantine.created_at, Quarantine.id)
    )
    if track:
        q = q.where(Opportunity.track == track)
    failed: dict[int, Any] = {}
    for row in (await session.execute(q)).all():
        failed[row.opportunity_id] = row

    # Rated and failed never overlap (R6): the newer of a role's score and its failure speaks.
    for opp_id, qrow in failed.items():
        later = latest.get(opp_id)
        if later is not None and later.created_at >= qrow.created_at:
            continue  # scored after it failed: the score stands
        latest.pop(opp_id, None)
        out["failed"] += 1
        events.append({"at": qrow.created_at, "tone": "bad",
                       "text": f"Scoring failed for {redact(qrow.title)}: "
                               f"{failure_words(qrow.error)}"})
    by_track: dict[str, dict[str, int]] = {}
    for row in latest.values():
        fit = fit_for(
            {"verdict": row.verdict, "tier": row.tier, "score": row.score,
             "weighted_score": row.weighted_score, "needs_review": row.needs_review},
            row.stage,
        )
        key = _FIT_KEY.get(fit.word)
        if key:
            out["groups"][key] += 1
            tg = by_track.setdefault(row.track or "employment", dict.fromkeys(GROUP_KEYS, 0))
            tg[key] += 1
        word = fit.word if key else "scored"
        score = f" ({fit.score:.1f})" if fit.score is not None and key else ""
        events.append({"at": row.created_at, "tone": _FIT_TONE.get(key or "", "idle"),
                       "text": f"Scored {redact(row.title)}: {word}{score}"})
    out["groups_by_track"] = by_track
    out["rated"] = len(latest)
    out["rating_now"] = t2_leased
    out["to_rate"] = max(out["quick_screen_kept"], out["rated"] + out["failed"] + n_t2)
    out["waiting"] = max(0, out["to_rate"] - out["rated"] - out["failed"] - t2_leased)

    # ------------------------------------------------------------------ stage and activity
    if out["paused"]:
        out["stage"] = "paused"
        # Paused with tasks still under a lease: they finish, nothing new starts (finding 2).
        out["finishing"] = int((await session.execute(
            select(func.count()).select_from(Task).where(
                Task.type.in_(_WORK_TASKS), Task.status == "leased",
                or_(Task.lease_until.is_(None), Task.lease_until >= _now_iso()))))
            .scalar_one())
    elif active and (n_scrape or out["current_search"]):
        out["stage"] = "searching"
    elif active and n_t1:
        out["stage"] = "quick_screen"
    elif active and n_t2:
        out["stage"] = "full_review"
    elif active:
        out["stage"] = "finishing"
    out["last_activity_at"] = await _last_activity(session, start, end, run)
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    out["recent_events"] = events[:EVENTS_SHOWN]
    return out


async def _last_activity(
    session: AsyncSession, start: str, end: str | None, run: Run | None
) -> str | None:
    """The newest row the run wrote on any track: a scrape starting or finishing, an evaluation,
    a quarantine row, or a task leased or finished."""
    sr = select(func.max(func.coalesce(ScrapeRun.finished_at, ScrapeRun.started_at))).where(
        or_(_in_window(ScrapeRun.started_at, start, end),
            _in_window(ScrapeRun.finished_at, start, end)))
    ev = select(func.max(Evaluation.created_at)).where(
        _in_window(Evaluation.created_at, start, end))
    qa = select(func.max(Quarantine.created_at)).where(
        _in_window(Quarantine.created_at, start, end))
    tk = select(func.max(Task.updated_at)).where(
        Task.type.in_(_WORK_TASKS), _in_window(Task.updated_at, start, end))
    row = (
        await session.execute(
            select(sr.scalar_subquery(), ev.scalar_subquery(), qa.scalar_subquery(),
                   tk.scalar_subquery())
        )
    ).one()
    stamps = [t for t in row if t]
    stamps.append(start)
    if run is not None and run.finished_at and not end:
        stamps.append(run.finished_at)
    return max(stamps)


# ------------------------------------------------------------------------------ words
def step_words(summary: dict[str, Any]) -> str:
    """THE current step in words (R1): the Today run panel's headline, the status rail and the
    Schedule card's tooltip all print this one string, so they cannot disagree.
    "Searching 3 of 5 searches", "Quick screen 18 of 42", "Rating 41 of 66", "Pausing: finishing
    2" (paused while tasks still hold a lease), "Paused", "Idle"."""
    s = summary
    if s.get("paused"):
        n = int(s.get("finishing") or 0)
        return f"Pausing: finishing {n}" if n else "Paused"
    if not s.get("active"):
        return "Idle"
    stage = s.get("stage")
    if stage == "searching":
        total = max(int(s["searches_total"]), 1)
        at = min(int(s["searches_done"]) + 1, total)
        return f"Searching {at} of {total} {_plural(total, 'search', 'searches')}"
    if stage == "quick_screen":
        return f"Quick screen {s['quick_screen_done']} of {s['quick_screen_total']}"
    if stage == "full_review":
        return f"Rating {int(s['rated']) + int(s['failed'])} of {s['to_rate']}"
    return "Finishing up"


def stage_line(summary: dict[str, Any]) -> str:
    """The current step with its detail (the Runs page Latest run panel): the step words, then
    the search being run and the roles found, or the failures."""
    s = summary
    line = step_words(s)
    stage = s.get("stage")
    if stage == "searching":
        if s.get("current_search"):
            line += f": {s['current_search']}"
        return line + f" · {s['found_new']} found so far"
    if stage == "full_review" and s["failed"]:
        return line + f" · {s['failed']} failed"
    return "" if line == "Idle" else line


def rail_line(summary: dict[str, Any]) -> str | None:
    """The status rail's worker label while a run is open (active or paused): the step words.
    None when idle (the rail keeps its own idle words)."""
    s = summary
    if not (s.get("active") or s.get("paused")):
        return None
    return step_words(s)


def group_list(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """The fit groups with at least one role, in the fit vocabulary's order."""
    g = summary.get("groups") or {}
    return [{"key": k, "label": GROUP_WORDS[k], "n": int(g.get(k, 0))}
            for k in GROUP_KEYS if int(g.get(k, 0) or 0)]


def added_for(summary: dict[str, Any], track: str) -> dict[str, int]:
    """What this run added to the track's Pipeline groups: Worth a look (strong, good, your
    call), Maybe and Not a fit (scored low by the model; the rules' set-asides are their own
    line)."""
    g = (summary.get("groups_by_track") or {}).get(track) or {}
    return {
        "worth": sum(int(g.get(k, 0) or 0) for k in WORTH_KEYS),
        "maybe": int(g.get("maybe", 0) or 0),
        "not_fit": int(g.get("not_fit", 0) or 0),
    }


def seconds_since(ts: str | None, now: datetime | None = None) -> int | None:
    dt = _parse(ts)
    if dt is None:
        return None
    return max(0, int(((now or datetime.now(UTC)) - dt).total_seconds()))


def ago_words(secs: int | None) -> str:
    """"12 seconds ago", "3 minutes ago", "2 hours ago"."""
    if secs is None:
        return "unknown"
    if secs < 60:
        return f"{secs} {_plural(secs, 'second', 'seconds')} ago"
    if secs < 3600:
        m = secs // 60
        return f"{m} {_plural(m, 'minute', 'minutes')} ago"
    h = secs // 3600
    return f"{h} {_plural(h, 'hour', 'hours')} ago"


def local_when(ts: str | None) -> str | None:
    """"Thu Oct 15, 6:12 AM" on the operator's clock (no platform-specific strftime flags)."""
    dt = _parse(ts)
    if dt is None:
        return None
    dt = dt.astimezone()
    hour = dt.hour % 12 or 12
    return f"{dt:%a %b} {dt.day}, {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def problem_note(summary: dict[str, Any]) -> str | None:
    """The finished run's status tooltip: which source had a problem, and that it is retried."""
    probs = summary.get("problems") or []
    if not probs:
        return None
    parts = [f"{p['name']} {p['why']}" for p in probs[:3]]
    more = len(probs) - len(parts)
    text = "; ".join(parts) + (f"; {more} more" if more > 0 else "")
    return f"{text}. {'It is' if len(probs) == 1 else 'They are'} retried next run."


def view(summary: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """What the templates read: the summary plus the step in words, the fit groups shown,
    the bar widths per stage, the last activity in words and the stall warning (no row for
    STALL_MINUTES while the run is active; never while paused)."""
    s = dict(summary)
    secs = seconds_since(s.get("last_activity_at"), now)
    stalled = bool(s.get("active") and secs is not None and secs >= STALL_MINUTES * 60)

    def pct(done: int, total: int) -> float:
        return round(100 * min(done, total) / total, 1) if total else 0.0

    def hhmm(ts: str | None) -> str | None:
        dt = _parse(ts)
        return dt.astimezone().strftime("%H:%M") if dt else None

    total = int(s.get("to_rate") or 0)
    s["recent_events"] = [{**e, "hhmm": hhmm(e.get("at"))} for e in s.get("recent_events") or []]
    n_problems = len(s.get("problems") or [])
    s.update(
        step=stage_line(s),
        headline=step_words(s),
        shown_groups=group_list(s),
        last_activity_words=ago_words(secs),
        stalled=stalled,
        stalled_minutes=(secs // 60) if stalled and secs is not None else None,
        ended_hhmm=hhmm(s.get("ended_at")),
        when=local_when(s.get("ended_at") or s.get("started_at")),
        problem_count=n_problems,
        problem_note=problem_note(s),
        rules_words=", ".join(f"{r['word']} {r['n']}" for r in s.get("set_aside_rules") or []),
        rate_widths={
            "scored": pct(s["rated"], total), "failed": pct(s["failed"], total),
            "running": pct(s.get("rating_now", 0), total),
            "waiting": pct(s.get("waiting", 0), total),
        },
        bars=[
            {"key": "search", "label": "Searches",
             "done": s["searches_done"], "total": s["searches_total"],
             "pct": pct(s["searches_done"], s["searches_total"])},
            {"key": "quick", "label": "Quick screen",
             "done": s["quick_screen_done"], "total": s["quick_screen_total"],
             "pct": pct(s["quick_screen_done"], s["quick_screen_total"])},
            {"key": "full", "label": "Full review",
             "done": s["rated"] + s["failed"], "total": s["to_rate"],
             "pct": pct(s["rated"] + s["failed"], s["to_rate"])},
        ],
    )
    return s
