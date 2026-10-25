"""Runs (D-UI-103, SPEC-INTERFACES 2.1 `/runs`): the header that follows the run state, the live
card, Latest run, the history timeline with its drawer, the Schedule card and the Needs another try
card.

  GET  /runs                      the page; an HTMX request gets the live fragment (header and
                                  card), which `static/js/runs.js` patches in place
  GET  /runs/history              the timeline, ten runs or all of them (`?all=1`)
  GET  /runs/{id}/drawer          one past run: summary, roles per minute, sources, details
  GET  /runs/start/choose         "This run only": Local or Online, with the ask-first text
  POST /runs/start                start a run now (every enabled search); an online choice
                                  needs `confirmed=1` and is checked here again
  POST /runs/pause|resume|stop    the worker controls; each answers with `run-refresh`
  POST /runs/quarantine/{id}/retry  Try again on a role from the Needs another try card

Nothing here calls a model or sends anything: every action writes task rows through the durable
queue, and the worker does the work (hard rules 1, 4 and 5). Every number comes from rows in the
database (`kestrel.analytics.run_summary`, `Run.counts`), so a restart shows the same picture."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from kestrel.analytics import run_summary as rsum
from kestrel.db.models import Evaluation, Run, ScrapeRun, Source
from kestrel.pipeline.runlog import INTERRUPTED
from kestrel.services import hosted_balance, run_model
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error
from kestrel.web.pages.needs_try import _role_views
from kestrel.web.pages.runs_detail import _open_total, _quarantine_rows, _retry_quarantine

log = structlog.get_logger()
router = APIRouter()

SHOWN = 10  # runs in the timeline before "Show more"
CARD_ROLES = 3  # roles the Needs another try card lists
EVENTS = rsum.EVENTS_SHOWN

# The four ways a run reads in History. Tone feeds `k-pill--<tone>` and the node color.
_STATUS = {
    "finished": ("Finished", "good"),
    "partial": ("Partial", "warn"),
    "stopped": ("Stopped by you", ""),
    "failed": ("Did not run", "bad"),
    "running": ("Running", "info"),
}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _clock(dt: datetime) -> str:
    from kestrel.date_display import format_display

    return format_display(dt, kind="time")


def when_parts(ts: str | None, now: datetime | None = None) -> tuple[str, str]:
    """("Yesterday", "8:00 AM") in the operator's local time; ("Oct 21", ...) after that."""
    dt = _parse(ts)
    if dt is None:
        return ("", "")
    local = dt.astimezone()
    today = (now or datetime.now(UTC)).astimezone().date()
    gap = (today - local.date()).days
    from kestrel.date_display import format_display

    day = "Today" if gap == 0 else "Yesterday" if gap == 1 else format_display(local, "short")
    return day, _clock(local)


def duration_words(run: Run) -> str | None:
    start, end = _parse(run.started_at), _parse(run.finished_at)
    if start is None or end is None:
        return None
    mins = int((end - start).total_seconds() // 60)
    if mins < 1:
        return "under 1 min"
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60} min"


def _scrape(run: Run) -> dict[str, Any]:
    return dict((run.counts or {}).get("scrape") or {})


def status_of(run: Run) -> dict[str, Any]:
    """How a past run reads: the label, its tone, and one plain line of why. Partial is a run
    where a search was rate-limited or errored (retried next run), never red. "Did not run" is
    rare: every search crashed, or the app closed before the run finished."""
    scrape = _scrape(run)
    errors = int(scrape.get("errors", 0) or 0)
    note = (run.notes or "").strip()
    if run.status == "running":
        key, why = "running", "This run is still going."
    elif run.status == "stopping":
        key = "stopped"
        why = note if note.startswith("Stopped by you") else "You stopped this run."
    elif run.status == "failed":
        key = "failed"
        why = "No search could run. Nothing was lost; they run again."
    elif run.status == INTERRUPTED:
        key = "failed"
        why = "Kestrel closed before this run ended. Nothing was lost."
    elif errors:
        key = "partial"
        n = f"{errors} search{'es' if errors != 1 else ''}"
        why = f"{n} had a problem. Kestrel retries next run."
    else:
        key = "finished"
        done = int(scrape.get("completed", 0) or 0)
        why = f"All {done} searches answered." if done > 1 else "All searches answered."
    label, tone = _STATUS[key]
    return {"key": key, "label": label, "tone": tone, "why": why}


def counts_of(run: Run) -> dict[str, int]:
    c = run.counts or {}
    return {
        "new": int(_scrape(run).get("new", 0) or 0),
        "rated": int((c.get("tier2") or {}).get("completed", 0) or 0),
        "failed": int((c.get("tier2") or {}).get("errors", 0) or 0),
    }


def mode_of(run: Run) -> dict[str, Any]:
    online = run.mode in ("hosted", "both")
    label = {"hosted": "Online", "both": "Both", "local": "Local"}.get(run.mode or "")
    return {"online": online, "label": label}


def history_row(
    run: Run, now: datetime | None = None, facts: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One run as History shows it. `facts` (from `run_facts`) adds what needs the database: the
    postings found and the online cost; without it found falls back to the new count."""
    day, at = when_parts(run.started_at, now)
    counts = counts_of(run)
    counts["found"] = max(int((facts or {}).get("found") or 0), counts["new"])
    return {
        "cost": (facts or {}).get("cost"),
        "id": run.id,
        "day": day,
        "at": at,
        "duration": duration_words(run),
        **{"status": status_of(run)},
        "counts": counts,
        "mode": mode_of(run),
    }


# ----------------------------------------------------------------- a run's own window and facts
async def run_window(session, run: Run) -> tuple[str, str | None]:
    """The span this run owns: from its start to its end (a stop closes it), never past the start
    of the next run. ScrapeRun and Evaluation carry no run id, so this window is the strict way to
    attribute them; work that starts after a run ended belongs to a later run (or to none)."""
    nxt = (
        await session.execute(
            select(func.min(Run.started_at)).where(Run.started_at > run.started_at)
        )
    ).scalar_one_or_none()
    ends = [e for e in (run.finished_at, nxt) if e]
    return run.started_at, (min(ends) if ends else None)


def _price_map(settings) -> dict[str, Any]:
    """The online provider's model list from cache (never a network call on a poll), by id."""
    from kestrel.services import hosted_models as hm

    backend = hm.catalog_backend(settings)
    models = (hm.cached(*backend) if backend else None) or []
    return {m.id: m for m in models}


NOT_RECORDED = "Not recorded"


async def run_cost(session, run: Run, prices: dict[str, Any], window=None) -> dict[str, Any] | None:
    """What an online or Both run cost, from the token counts each rating recorded and the
    provider's listed price for the model that answered. None for a Local run (nothing to pay).
    A figure with no recorded tokens or no listed price is "Not recorded", never a guess; this is
    an estimate from tokens, not the provider's bill (no per-call bill is stored for ratings)."""
    from kestrel.services.hosted_balance import usd

    if run.mode not in ("hosted", "both"):
        return None
    start, end = window or await run_window(session, run)
    stmt = (
        select(
            Evaluation.model,
            func.coalesce(func.sum(Evaluation.prompt_tokens), 0),
            func.coalesce(func.sum(Evaluation.completion_tokens), 0),
            func.count(),
        )
        .where(Evaluation.created_at >= start, Evaluation.model.is_not(None))
        .group_by(Evaluation.model)
    )
    if end:
        stmt = stmt.where(Evaluation.created_at <= end)
    rows = [r for r in (await session.execute(stmt)).all() if r[0] in prices]
    tokens = sum(int(r[1]) + int(r[2]) for r in rows)
    roles = sum(int(r[3]) for r in rows)
    if not rows:
        return {"label": NOT_RECORDED, "usd": None, "tokens": 0, "roles": 0}
    total = 0.0
    for model, p_in, p_out, _n in rows:
        m = prices[model]
        if m.prompt_usd_per_m is None or m.completion_usd_per_m is None:
            return {"label": NOT_RECORDED, "usd": None, "tokens": tokens, "roles": roles}
        total += (int(p_in) * m.prompt_usd_per_m + int(p_out) * m.completion_usd_per_m) / 1e6
    label = "Free model" if total <= 0 else "about " + usd(total)
    return {"label": label, "usd": total, "tokens": tokens, "roles": roles}


async def run_facts(session, run: Run, prices: dict[str, Any]) -> dict[str, Any]:
    """Found (postings the run's searches fetched) and the online cost, for History."""
    window = await run_window(session, run)
    found = 0
    for sr in (await session.execute(_scrapes_stmt(select(ScrapeRun), run, window))).scalars():
        c = sr.counts or {}
        stored = sum(int(c.get(k, 0) or 0) for k in ("new", "reseen", "changed"))
        signal = int(c.get("promoted", 0) or 0) + int(c.get("postings", 0) or 0)
        found += int(c.get("fetched") or stored or signal)
    return {"found": found, "cost": await run_cost(session, run, prices, window)}


def _scrapes_stmt(stmt, run: Run, window: tuple[str, str | None]):
    """Restrict a ScrapeRun query to this run, strictly: started inside the run's window, and for
    a stopped run not finished after the stop (a search already in flight at Stop keeps going and
    writes its row later; that row belongs to no run). A search with no end is "cut off"."""
    start, end = window
    stmt = stmt.where(ScrapeRun.started_at >= start)
    if end:
        stmt = stmt.where(ScrapeRun.started_at <= end)
        if run.status == "stopping":
            stmt = stmt.where(
                (ScrapeRun.finished_at.is_(None)) | (ScrapeRun.finished_at <= end)
            )
    return stmt


async def history(session, *, all_runs: bool = False) -> dict[str, Any]:
    """The timeline: the newest ten runs (all of them with `all_runs`), plus the total."""
    # A run that is going has the live card; the timeline holds the ones that ended.
    ended = Run.status != "running"
    total = int(
        (await session.execute(select(func.count()).select_from(Run).where(ended))).scalar_one()
    )
    stmt = select(Run).where(ended).order_by(Run.started_at.desc(), Run.id.desc())
    if not all_runs:
        stmt = stmt.limit(SHOWN)
    runs = list((await session.execute(stmt)).scalars())
    now = datetime.now(UTC)
    settings = await load_settings_with_db(session)
    prices = _price_map(settings)
    rows = [history_row(r, now, await run_facts(session, r, prices)) for r in runs]
    return {
        "rows": rows,
        "total": total,
        "more": max(0, total - len(runs)),
        "all": all_runs,
    }


# ------------------------------------------------------------------------------- live card
def _tile(
    key: str,
    label: str,
    icon: str,
    done: int,
    total: int,
    *,
    current: bool,
    failed: int = 0,
    caption: str | None = None,
    online: bool = False,
) -> dict[str, Any]:
    def pct(n: int) -> float:
        return round(100 * min(n, total) / total, 1) if total else 0.0

    finished = bool(total) and done + failed >= total and not current
    state = "now" if current else "done" if finished else "wait" if not total else "mid"
    return {
        "key": key,
        "label": label,
        "icon": icon,
        "state": state,
        "online": online,
        "done": done,
        "total": total,
        "failed": failed,
        "good_pct": pct(done),
        "bad_pct": pct(failed),
        "caption": caption,
    }


def stage_tiles(v: dict[str, Any], *, online: bool, both: bool = False) -> list[dict[str, Any]]:
    """The five stage tiles (D-UI-58): Searching, Your rules, Quick screen, Rating and Deeper
    read, which always reads "Only when you ask". The current one follows the run's stage."""
    stage = v.get("stage")
    found = int(v["found_new"])
    rules_done = min(found, int(v["set_aside"]) + int(v["quick_screen_total"]))
    rated, failed, to_rate = int(v["rated"]), int(v["failed"]), int(v["to_rate"])
    s_total = int(v["searches_total"])
    s_done = int(v["searches_done"])
    searching = stage == "searching"
    rate_cap = (
        (f"{rated + failed} of {to_rate} roles" + (f" · {failed} failed" if failed else ""))
        if to_rate
        else "Waiting"
    )
    tiles = [
        _tile(
            "search",
            "Searching",
            "scan",
            s_done,
            s_total,
            current=searching,
            caption=f"{s_done} of {s_total} {'search' if s_total == 1 else 'searches'}"
            if s_total
            else "Waiting",
        ),
        _tile(
            "rules",
            "Your rules",
            "tune",
            rules_done,
            found,
            current=False,
            caption=f"{rules_done} of {found} roles" if found else "Waiting",
        ),
        _tile(
            "quick",
            "Quick screen",
            "check",
            int(v["quick_screen_done"]),
            int(v["quick_screen_total"]),
            current=stage == "quick_screen",
            online=online and not both,
            caption=f"{v['quick_screen_done']} of {v['quick_screen_total']} roles"
            if v["quick_screen_total"]
            else "Waiting",
        ),
        _tile(
            "rating",
            "Rating",
            "star",
            rated,
            to_rate,
            failed=failed,
            current=stage == "full_review",
            online=online,
            caption=rate_cap,
        ),
        {
            "key": "deeper",
            "label": "Deeper read",
            "icon": "compass",
            "state": "wait",
            "online": False,
            "done": 0,
            "total": 0,
            "failed": 0,
            "good_pct": 0,
            "bad_pct": 0,
            "caption": "Only when you ask",
        },
    ]
    if v.get("active") and stage in ("quick_screen", "full_review", "finishing"):
        tiles[0]["state"] = "done" if s_total else tiles[0]["state"]
        tiles[1]["state"] = "done" if found else tiles[1]["state"]
    if v.get("active") and stage == "full_review":
        tiles[2]["state"] = "done" if tiles[2]["total"] else tiles[2]["state"]
    return tiles


def event_key(e: dict[str, Any]) -> str:
    return f"{e.get('at') or ''}|{e.get('text') or ''}"


async def _latest_run_row(session, run_id: int | None) -> Run | None:
    return await session.get(Run, run_id) if run_id else None


def _estimate_rest(v: dict[str, Any], prices: dict[str, Any], settings, spent: dict | None) -> str:
    """What the roles still to rate may cost: their count times the average the run has recorded
    (five or more ratings), else the same fixed per-role size as the Needs another try estimate,
    at the listed price of the model that rates. "Not available" when the price is not listed."""
    from kestrel.services.hosted_balance import usd
    from kestrel.web.pages.needs_try import _FIXED_IN_TOKENS, _OUT_TOKENS, _PAGE_CHARS

    left = max(0, int(v["to_rate"]) - int(v["rated"]) - int(v["failed"]))
    tier = settings.llm.hosted_tiers.get("tier2")
    model = prices.get(tier.model) if tier else None
    if model is None or model.prompt_usd_per_m is None or model.completion_usd_per_m is None:
        return "Not available: the model's price is not listed"
    if not left:
        return "Nothing left to rate"
    if spent and spent.get("roles", 0) >= 5 and spent.get("usd") is not None:
        total = spent["usd"] / spent["roles"] * left
    else:
        tin = _FIXED_IN_TOKENS + _PAGE_CHARS / 4
        per_role = tin * model.prompt_usd_per_m + _OUT_TOKENS * model.completion_usd_per_m
        total = left * per_role / 1e6
    return "about " + usd(total) if total > 0 else "Free model"


async def _model_card(
    session, settings, run: Run | None, online: bool, v: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The Model card and the provider line: where the AI ran and which model, and for an online
    run the spend and estimate (from recorded tokens and the listed price; see `run_cost`)."""
    from kestrel.analytics.progress import tier2_label

    label = tier2_label(settings)
    row = await hosted_balance.rail_row(session, settings) if online else None
    provider = None
    if online:
        from kestrel.llm import providers as prov

        try:
            names = [p for p in prov.external_backends(settings)]
        except (AttributeError, KeyError, TypeError):  # no provider configured: say nothing
            names = []
        provider = names[0].replace("_", " ").title() if names else None
    spend = estimate = left = None
    if online and run is not None:
        prices = _price_map(settings)
        cost = await run_cost(session, run, prices)
        spend = cost["label"] if cost else None
        if v is not None and v.get("active"):
            estimate = _estimate_rest(v, prices, settings, cost)
        left = await hosted_balance.credit_left(session, settings)
    both = bool(run is not None and run.mode == "both")
    return {
        "online": online,
        "both": both,
        "title": "Both" if both else "Online" if online else "Local",
        "model": label,
        "provider": provider,
        "credit": row["label"] if row else None,
        "spend": spend,
        "estimate": estimate,
        "left": left,
    }


async def card_context(session) -> dict[str, Any]:
    """What the Schedule card draws: the schedule, the keep-awake switch and the saved run mode
    in words ("Local, qwen3:14b"). The card swaps itself on save, so it needs all of it."""
    from kestrel.analytics.progress import tier2_label
    from kestrel.llm import credit
    from kestrel.web.routes_pages import _keep_awake_context, _mode_context

    settings = await load_settings_with_db(session)
    label = tier2_label(settings)
    return {
        "runs_use": ("Online" if settings.llm.mode == "hosted" else "Local")
        + (f", {label}" if label else ""),
        "credit_notices": credit.notices(),
        **(await _mode_context(session)),
        **(await _keep_awake_context(session)),
    }


async def context(session) -> dict[str, Any]:
    """Everything the page and its live fragment draw."""
    from kestrel.analytics.progress import _model_in_use

    summary = await rsum.run_summary(session, None)
    v = rsum.view(summary)
    settings = await load_settings_with_db(session)
    run_row = await _latest_run_row(session, v.get("run_id"))
    if run_row is not None and run_row.mode:  # the run's own recorded choice wins
        online = run_row.mode in ("hosted", "both")
    else:
        online = bool(v.get("active")) and settings.llm.mode == "hosted"
    state = "active" if v.get("active") else "paused" if v.get("paused") else "idle"
    shown = await _model_in_use(session, settings, bool(v.get("active")))
    model = await _model_card(session, settings, run_row, online, v)
    rows = await _quarantine_rows(session, open_cap=200, resolved_cap=0)
    waiting_views = _role_views([r for r in rows if not r["resolved"] and r["needs_you"]])
    open_total = await _open_total(session)
    started = v.get("started_at")
    scan_n = 0
    if state == "idle" and started and run_row is not None:
        count = _scrapes_stmt(
            select(func.count()).select_from(ScrapeRun), run_row, await run_window(session, run_row)
        )
        scan_n = int((await session.execute(count)).scalar_one() or 0)
    hist = await history(session)
    latest_facts = (
        await run_facts(session, run_row, _price_map(settings)) if run_row is not None else None
    )
    first = hist["rows"][0] if hist["rows"] else None
    return {
        "v": v,
        "state": state,
        "latest": history_row(run_row, None, latest_facts) if run_row is not None else None,
        "live_sig": f"{state}:{v.get('run_id') or 0}:{v.get('status') or ''}",
        "hist_sig": f"{first['id'] if first else 0}:{first['status']['key'] if first else ''}"
        f":{hist['total']}",
        "quar_sig": f"{len(waiting_views)}:"
        + ",".join(str(r["id"]) for r in waiting_views[:CARD_ROLES]),
        "tiles": stage_tiles(v, online=online, both=bool(run_row and run_row.mode == "both")),
        "events": [{**e, "key": event_key(e)} for e in v.get("recent_events") or []],
        "model": model,
        "latest_when": v.get("when"),
        "latest_found": int(v["found_new"]),
        "sources_checked": scan_n,
        "run_model": shown if shown and "(this run only)" in shown else None,
        "history": hist,
        "waiting_views": waiting_views[:CARD_ROLES],
        "waiting_total": len(waiting_views),
        "open_total": open_total,
        "online_run": online,
        **(await card_context(session)),
    }


@router.get("/runs", response_class=HTMLResponse)
async def page(request: Request):
    async with sessionmaker()() as s:
        ctx = await context(s)
    tmpl = "partials/runs_fragment.html" if request.headers.get("HX-Request") else "runs.html"
    # One URL serves the page and the poll's fragment: caches must tell them apart.
    return templates().TemplateResponse(request, tmpl, ctx, headers={"Vary": "HX-Request"})


@router.get("/runs/history", response_class=HTMLResponse)
async def history_partial(request: Request, all: str = ""):
    async with sessionmaker()() as s:
        hist = await history(s, all_runs=all in ("1", "true"))
    return templates().TemplateResponse(request, "partials/runs_history.html", {"history": hist})


# --------------------------------------------------------------------------------- drawer
def _per_minute(c: dict[str, Any]) -> dict[str, Any]:
    done = int(c.get("completed", 0) or 0)
    if done < 5:  # hard rule 6: a rate from fewer than five roles is not a rate
        return {"value": None, "words": f"too few ({done} of 5)"}
    rate = float(c.get("per_minute", 0) or 0)
    return {"value": rate, "words": f"{rate:g}"}


def throughput(run: Run) -> list[dict[str, Any]]:
    c = run.counts or {}
    quick, rating = _per_minute(c.get("tier1") or {}), _per_minute(c.get("tier2") or {})
    rates = [("Quick screen", quick), ("Rating", rating)]
    top = max([r["value"] for _l, r in rates if r["value"]] or [0])
    return [
        {"label": "Your rules", "words": "n/a", "pct": 0},
        *[
            {
                "label": label,
                "words": r["words"],
                "pct": round(100 * r["value"] / top) if r["value"] and top else 0,
            }
            for label, r in rates
        ],
        {"label": "Deeper read", "words": "n/a", "pct": 0},
    ]


async def sources_in(session, run: Run) -> list[dict[str, Any]]:
    """Per-source results for the run's own window (`run_window`, strict; ScrapeRun has no run
    id). The plain words come from `problem_words`; the raw error stays behind Show details."""
    stmt = _scrapes_stmt(
        select(ScrapeRun, Source.name, Source.kind, Source.config).join(
            Source, Source.id == ScrapeRun.source_id, isouter=True
        ),
        run,
        await run_window(session, run),
    ).order_by(ScrapeRun.started_at)
    out = []
    for sr, name, kind, config in (await session.execute(stmt.limit(60))).all():
        counts = sr.counts or {}
        if "new" in counts:
            new = int(counts.get("new", 0) or 0)
        else:
            new = int(counts.get("promoted", 0) or 0) + int(counts.get("postings", 0) or 0)
        if sr.finished_at is None:
            word, tone = "Cut off", "warn"
        elif sr.status == "ok":
            word, tone = "OK", "good"
        elif sr.status == "partial":
            word, tone = (
                "Rate-limited" if "rate" in rsum.problem_words(sr.error, sr.status) else "Partial",
                "warn",
            )
        else:
            word, tone = "Problem", "bad"
        out.append(
            {
                "name": rsum.search_label(name, kind, config)
                if kind == "jobspy"
                else (name or "A source"),
                "new": new,
                "word": word,
                "tone": tone,
                "why": rsum.problem_words(sr.error, sr.status)
                if sr.status in ("failed", "partial")
                else None,
                "error": (sr.error or "")[:160] or None,
                "model": run_model.label_of(sr.counts),
            }
        )
    return out


@router.get("/runs/{run_id}/drawer", response_class=HTMLResponse)
async def drawer(request: Request, run_id: int):
    async with sessionmaker()() as s:
        run = await s.get(Run, run_id)
        if run is None:
            return HTMLResponse("No such run.", status_code=404)
        sources = await sources_in(s, run)
        settings = await load_settings_with_db(s)
        row = history_row(run, None, await run_facts(s, run, _price_map(settings)))
    ctx = {
        "run": run,
        "row": row,
        "chart": throughput(run),
        "sources": sources,
        "counts_json": run.counts or {},
    }
    return templates().TemplateResponse(request, "partials/runs_drawer.html", ctx)


# ------------------------------------------------------------------------------- controls
def _refresh(status: int = 204, **extra: str) -> Response:
    return Response(status_code=status, headers={"HX-Trigger": "run-refresh", **extra})


@router.post("/runs/pause")
async def pause():
    from kestrel.web.app import set_paused

    set_paused(True)
    return _refresh()


@router.post("/runs/resume")
async def resume():
    from kestrel.web.routes_control import worker_resume

    await worker_resume()
    return _refresh()


@router.post("/runs/stop")
async def stop():
    """Stop the run: nothing new starts, searches not yet begun are cancelled, and the open run
    is recorded as stopped by you. Scoring tasks already queued stay queued (hard rule 5), so
    Run now or Resume picks up where it left off; nothing is lost."""
    from kestrel.ingest.scheduler import cancel_pending_scrapes
    from kestrel.pipeline import runlog
    from kestrel.web.app import set_paused

    sm = sessionmaker()
    async with sm() as s:
        summary = rsum.view(await rsum.run_summary(s, None))
        if not (summary.get("active") or summary.get("paused")):
            return _refresh()  # a stale tab or a double click: nothing is open, so nothing changes
        set_paused(True)
        await cancel_pending_scrapes(s)
    step = (summary.get("headline") or "").replace("Paused", "").strip(": ")
    run_id = summary.get("run_id")
    if run_id:
        note = f"Stopped by you{' at ' + step if step else ''}."
        await runlog.finish_run(sm, run_id, status="stopping", notes=note)
    return _refresh()


@router.get("/runs/start/choose", response_class=HTMLResponse)
async def start_choose(request: Request):
    """The "This run only" sheet. Local starts at once. Online lists exactly what is sent (the
    text the operator acknowledged in Hosted mode), what is not, and waits for a yes."""
    from kestrel.config import HostedModeError
    from kestrel.web.pages.needs_try import _employer_state, _sentence

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        import json

        from kestrel.services.online_consent import (
            RUN_TIERS,
            approval_for,
            online_route,
        )

        try:
            hosted_settings = await load_settings_with_db(s, mode="hosted")
        except HostedModeError:
            hosted_settings = settings
        approval = approval_for(hosted_settings, RUN_TIERS)
        routes = [await online_route(s, hosted_settings, tier) for tier in approval]
        try:
            both_settings = await load_settings_with_db(s, mode="both")
        except HostedModeError:
            both_settings = settings
        both_approval = approval_for(both_settings, RUN_TIERS)
        both_routes = [await online_route(s, both_settings, tier) for tier in both_approval]
        employer = await _employer_state(s, settings)
        credit_row = await hosted_balance.rail_row(s, settings)
        mode = await _mode_ctx(s)
    sends = settings.llm.hosted_mode.disclosure_terms()["sends"]
    ctx = {
        "hosted_problem": mode["hosted_problem"],
        "sends": [_sentence(sends[k]) for k in (approval or ("tier1", "tier2"))
                  if sends.get(k)],
        # Both: the quick screen (tier 1) stays on this computer, so only the rating is sent.
        "sends_both": [_sentence(sends[k]) for k in (both_approval or ("tier2",))
                       if sends.get(k)],
        "routes": routes,
        "both_routes": both_routes,
        "employer": employer,
        "credit": credit_row["label"] if credit_row else None,
        "usual": "Online" if settings.llm.mode == "hosted" else "Local",
        "approval_json": json.dumps(approval, separators=(",", ":")),
        "both_approval_json": json.dumps(both_approval, separators=(",", ":")),
    }
    return templates().TemplateResponse(request, "partials/runs_choose.html", ctx)


async def _mode_ctx(s) -> dict[str, Any]:
    from kestrel.web.routes_pages import _mode_context

    return await _mode_context(s)


@router.post("/runs/start", response_class=HTMLResponse)
async def start(request: Request):
    """Run now: queue a search for every enabled source, due or not, and (for a one-time online
    choice) the run mode for the next worker cycle. `mode=hosted` needs `confirmed=1`, which only
    the ask-first sheet sends; hosted mode must also be usable, or nothing is queued."""
    from kestrel.config import RUN_CHOICES, HostedModeError, hosted_mode_problem
    from kestrel.ingest.scheduler import enqueue_all_scrapes
    from kestrel.llm import credit
    from kestrel.queue.queue import TaskQueue
    from kestrel.services.setting_overrides import queue_run_mode

    form = await request.form()
    mode = str(form.get("mode") or "")
    sm = sessionmaker()
    if mode and mode not in RUN_CHOICES:
        return _note("Not started: that choice is not available.", status=400)
    async with sm() as s:
        enabled = int(
            (
                await s.execute(
                    select(func.count()).select_from(Source).where(Source.enabled.is_(True))
                )
            ).scalar_one()
            or 0
        )
        if not enabled:
            return _note("No searches are on. Turn one on under Sources.", status=409)
        settings = await load_settings_with_db(s)
        chosen = mode or settings.llm.mode
        if chosen == "both" and "both_online_approval" in form:
            form = {**form, "online_approval": form["both_online_approval"]}
        online_choice = chosen in ("hosted", "both")
        if online_choice and str(form.get("confirmed") or "") != "1":
            return _note("Not started. Nothing was sent: confirm in the sheet first.", status=400)
        from kestrel.services.online_consent import RUN_TIERS, approval_for, approval_problem

        try:
            effective = await load_settings_with_db(s, mode=chosen)
        except HostedModeError:
            effective = settings
        problem = (
            hosted_mode_problem(settings)
            if chosen in ("hosted", "both")
            else None
        )
        if online_choice:
            from kestrel.web.pages.needs_try import _employer_state

            if await _employer_state(s, settings) == "held":  # confidential, no employer chosen
                return _note(
                    "Not started. Nothing was sent: confidential mode is on and no current "
                    "employer is chosen. Choose it in Settings first.",
                    status=409,
                )
            problem = await approval_problem(s, effective, RUN_TIERS, form)
            if problem:
                return _note("Not started: confirm the online send first.", status=400)
        if problem and not mode:  # the saved choice cannot run: say so now, not in the worker
            return _note(f"Not started: {problem}.", status=409)
        # The sheet offers several modes, but only the selected mode was approved. Local
        # explicitly approves no online destination, even when the hidden field names them.
        selected_approval = approval_for(effective, RUN_TIERS) if online_choice else {}
        credit.clear()  # the operator asked for a run: let sends to an online key resume (D-LLM-8)
        if mode or online_choice:
            try:
                await queue_run_mode(s, chosen, online_approval=selected_approval)
                await s.commit()
            except HostedModeError as exc:
                return _note(f"Not started: {plain_error(exc)}.", status=409)
            except StaleDataError:
                return _note(
                    "Not started: the queue changed while starting. Try again.", status=409
                )
    from kestrel.web.app import is_paused

    if is_paused():
        await resume()
    async with sm() as s:
        queued = await enqueue_all_scrapes(
            s, TaskQueue(sm), online_approval=selected_approval,
            reconfirm_held=online_choice,
        )
    if not queued:
        return _note("No searches are on. Turn one on under Sources.", status=409)
    return _refresh(200)


def _note(text: str, status: int = 200) -> Response:
    """A short sentence the header shows in place (and a toast)."""
    import json

    return Response(
        text,
        status_code=status,
        media_type="text/plain",
        headers={
            "HX-Trigger": json.dumps({"k-toast": {"message": text, "tone": "warn"}}),
            "HX-Reswap": "none",
        },
    )


@router.post("/runs/quarantine/{quarantine_id}/retry", response_class=HTMLResponse)
async def card_retry(request: Request, quarantine_id: int):
    """Try again from the card: the same re-queue as the Needs another try page. A refusal is
    said in a toast and the role stays listed."""
    async with sessionmaker()() as s:
        try:
            err = await _retry_quarantine(s, quarantine_id)
        except SQLAlchemyError as exc:
            log.warning("runs_card_retry_failed", id=quarantine_id, error_type=type(exc).__name__)
            await s.rollback()
            err = "Could not queue that. Try again."
    if err:
        return _note(err, status=409)
    return _refresh(200)
