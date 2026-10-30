"""Sources page (SPEC-INTERFACES.md 2.1, ~line 241; redesigned in D-SRC-3).

The operator should not have to write a search query. The page leads with "Your searches", the
job searches generated from the profile (kestrel/ingest/generated.py), each shown by a
plain-English summary with last run and health, and one switch that turns all of them on or off.
The summary is recomputed from the profile on every render with `build_specs` (the same function
that built the row), so no column stores it and it can never go stale. A row whose name no longer
matches a spec falls back to its name. Query syntax is behind a "Show details" disclosure.

Editing a generated search makes it `custom`: it keeps its name, stops following the profile, and
the rebuild does not build that name again. Every row the operator adds here is `custom` too.

Everything else is under "Advanced", collapsed by default:
- Watch a company's job board: the operator types a company name; kestrel/ingest/board_lookup.py
  tries the public Greenhouse, Lever, Ashby and SmartRecruiters boards and, on a match, adds a
  custom source. On no match it says so plainly and points at the manual form.
- SAM.gov (consulting only): what it is, why it needs a key, how to get one, and a field that
  saves the key to the data-home .env through `providers.write_secret` (the same helper the
  hosted-model keys use) and then enables the source.
- RSS feeds and PE portfolio pages (consulting only), one line of explanation each.
- Add a source by hand: the original form. Validation reuses the SAME `validate_config` the scan
  runner calls (kestrel/ingest/runner.py, kestrel/ingest/signal_runner.py), so a structurally
  broken config is refused here with a clear message rather than saved.

When consulting is switched off (D-UI-28) consulting rows and the consulting-only parts of
Advanced are not rendered; the rows stay in the database. The "Tracks I use" setting is saved by
`save_tracks` below; the control lives on Settings > Core (partials/settings_tracks.html).

Run-now calls the real runner inline, wrapped so a dead network or a down Ollama is a message in
the row, never a 500."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.enums import SOURCE_KINDS, TRACKS
from kestrel.db.models import ScrapeRun, Source, Task
from kestrel.ingest import board_discovery
from kestrel.ingest.adapters import get_adapter
from kestrel.ingest.base import SourceConfigError
from kestrel.ingest.generated import SEC_TITLES_LABEL, sec_titles_opt_in
from kestrel.ingest.signal_sources import SIGNAL_SOURCE_KINDS, get_signal_source
from kestrel.llm import credit
from kestrel.services import tracks as tracks_svc
from kestrel.web import source_view as sv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

router = APIRouter()

_DEFAULT_CADENCE = 24
GENERATED = "generated"
CUSTOM = "custom"
SAM_ENV = "SAM_GOV_API_KEY"
SAM_NAME = "SAM.gov federal IT contracts"
RRS_ENV = "REMOTEROCKETSHIP_API_KEY"
RRS_NAME = "RemoteRocketship"
USAJOBS_ENV = "USAJOBS_API_KEY"
ADZUNA_ID_ENV = "ADZUNA_APP_ID_KEY"
ADZUNA_KEY_ENV = "ADZUNA_APP_KEY"
USAJOBS_EMAIL_KEY = "sources.usajobs_email"  # the Setting row holding the email USAJobs requires
SAM_NAICS = ["541512", "541611", "541519"]


def _validate_source_config(kind: str, config: dict) -> str | None:
    """Reuse the exact validators the scan runner uses. An employment adapter's `validate_config`
    (JobSpy's Indeed conflict; every ATS's slug/token/tenant presence) or a
    consulting signal source's `validate_config` (EDGAR queries, SAM.gov key, RSS feeds, PE
    portfolio firms) raises `SourceConfigError`, which becomes the operator-facing message. A kind
    with no registered validator (`page`, `manual`) has nothing to check structurally and passes;
    it is not a form of uncertainty to reject, just a kind this build has no automated fetch for."""
    adapter = get_adapter(kind)
    if adapter is not None:
        try:
            adapter.validate_config(config)
        except SourceConfigError as exc:
            return plain_error(exc)
        return None
    signal_source = get_signal_source(kind)
    if signal_source is not None:
        try:
            signal_source.validate_config(config)
        except SourceConfigError as exc:
            return plain_error(exc)
    return None


def _parse_config(raw: str) -> tuple[dict, str | None]:
    """Parse the operator's config JSON. A syntax error is a validation message, never a crash."""
    text = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"config must be valid JSON (line {exc.lineno}, column {exc.colno})"
    if not isinstance(parsed, dict):
        return {}, "config must be a JSON object (e.g. {\"token\": \"...\"})"
    return parsed, None


def _validate_common(name: str, track: str, kind: str, cadence_raw: str) -> tuple[int, str | None]:
    """Shared field checks for create and update. Returns (cadence_hours, error)."""
    if not name:
        return _DEFAULT_CADENCE, "name is required"
    if track not in TRACKS:
        return _DEFAULT_CADENCE, f"track must be one of: {', '.join(TRACKS)}"
    if kind not in SOURCE_KINDS:
        return _DEFAULT_CADENCE, f"kind must be one of: {', '.join(SOURCE_KINDS)}"
    try:
        cadence_hours = int(cadence_raw)
        if cadence_hours < 1:
            raise ValueError
    except ValueError:
        return _DEFAULT_CADENCE, "cadence_hours must be a positive integer"
    return cadence_hours, None


async def _all_sources(session: AsyncSession, *, removed: bool = False) -> list[Source]:
    """Every source in page order. A removed one (the Undo marker, see source_view) is left out
    unless asked for: it is hidden, never deleted, so the roles it found keep their source."""
    stmt = select(Source).order_by(Source.track, Source.kind, Source.name)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows if removed else [s for s in rows if not sv.is_removed(s)]


async def _recent_runs(
    session: AsyncSession, sources: list[Source], n: int = 10
) -> dict[int, list[ScrapeRun]]:
    """The last `n` scrape runs per source, newest first, for the sparkline and the drawer's
    health history. Bounded like `_latest_runs`: a source with no runs has no entry."""
    ids = [s.id for s in sources]
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ScrapeRun)
                .where(ScrapeRun.source_id.in_(ids))
                .order_by(ScrapeRun.started_at.desc())
                .limit(max(500, 12 * len(ids)))
            )
        )
        .scalars()
        .all()
    )
    out: dict[int, list[ScrapeRun]] = {}
    for run in rows:
        bucket = out.setdefault(run.source_id, [])
        if len(bucket) < n:
            bucket.append(run)
    return out


async def _latest_runs(session: AsyncSession, sources: list[Source]) -> dict[int, ScrapeRun]:
    """Most recent scrape_run per source, for the health/last-run/counts/errors columns SPEC-
    INTERFACES 2.1 asks for beyond the bare Source row. Bounded and best-effort: recent runs across
    all matched sources are scanned for the first (most recent) hit per source_id; the bound keeps
    this cheap even with a long-running history, and an empty/short history is handled gracefully
    (a source with no runs yet simply has no entry)."""
    ids = [s.id for s in sources]
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ScrapeRun)
                .where(ScrapeRun.source_id.in_(ids))
                .order_by(ScrapeRun.started_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, ScrapeRun] = {}
    for run in rows:
        latest.setdefault(run.source_id, run)
    return latest


async def _site_lines(
    session: AsyncSession, sources: list[Source], runs: dict[int, ScrapeRun]
) -> dict[int, list[dict]]:
    """Per job board postings from a JobSpy search's last run: [{"label": "Indeed", "count": 98}].

    JobSpy tags each row with raw["site"]. A run that records `counts["by_site"]` is read from
    there; otherwise the count is the search's stored postings the last run saw (first seen, or
    seen again, since it started), grouped by that tag. Every board the search asks appears, so
    a board that returned nothing shows 0 and is flagged on the row. A posting another search or
    board saw first is not counted here, so the figure can run low but never invents a result."""
    from kestrel.analytics.run_summary import _SITE_WORDS
    from kestrel.db.models import Opportunity
    from kestrel.ingest.adapters.jobspy import _sites

    out: dict[int, list[dict]] = {}
    for src in sources:
        run = runs.get(src.id)
        if src.kind != "jobspy" or run is None or run.status == "running":
            continue
        asked = _sites(src.config or {})
        by_site = (run.counts or {}).get("by_site")
        if not isinstance(by_site, dict):
            raws = (
                await session.execute(
                    select(Opportunity.raw).where(
                        Opportunity.source_id == src.id,
                        Opportunity.last_seen_at >= run.started_at,
                    )
                )
            ).scalars().all()
            by_site = {}
            for raw in raws:
                site = str((raw or {}).get("site") or "").lower()
                if site:
                    by_site[site] = by_site.get(site, 0) + 1
        names = [*asked, *(s for s in by_site if s not in asked)]
        out[src.id] = [
            {"label": _SITE_WORDS.get(n, n.replace("_", " ").title()),
             "count": int(by_site.get(n, 0))}
            for n in names
        ]
    return out


async def _summaries(session: AsyncSession) -> dict[str, str]:
    """Plain-English line per generated search, recomputed from the profile (see module doc).
    A profile that cannot be read yields no summaries; rows then show their names."""
    from kestrel.ingest.generated import _profile_inputs, build_specs

    try:
        blocks, email = _profile_inputs()
        enabled = await tracks_svc.load_enabled(session)
        return {sp.name: sp.summary for sp in build_specs(blocks, enabled, email)}
    except Exception as exc:  # a bad profile.yml must not take the Sources page down
        import structlog

        structlog.get_logger().warning("source_summaries_failed", error=str(exc))
        return {}


def _visible_kinds(enabled_tracks: tuple[str, ...]) -> tuple[str, ...]:
    """The consulting signal kinds are offered only while the consulting track is in use."""
    if "consulting" in enabled_tracks:
        return SOURCE_KINDS
    from kestrel.ingest.signal_sources import known_signal_kinds

    signal = set(known_signal_kinds())
    return tuple(k for k in SOURCE_KINDS if k not in signal)


def _sam_status() -> dict:
    from kestrel.llm.providers import masked, secret_present, secret_value

    present = secret_present(SAM_ENV)
    return {"present": present, "masked": masked(secret_value(SAM_ENV)) if present else ""}


async def _rrs_status(session: AsyncSession) -> dict:
    """The RemoteRocketship card: whether a key is saved (only its last four characters are ever
    shown) and the source row, if the user has turned it on."""
    from kestrel.llm.providers import masked, secret_present, secret_value

    present = secret_present(RRS_ENV)
    src = (
        await session.execute(select(Source).where(Source.kind == "remoterocketship").limit(1))
    ).scalar_one_or_none()
    return {
        "present": present, "masked": masked(secret_value(RRS_ENV)) if present else "",
        "source": src, "on": bool(src is not None and src.enabled),
    }


async def usajobs_status(session: AsyncSession) -> dict:
    """The USAJobs card and the Add sheet's key panel: whether a key is saved (only its last four
    characters are ever shown) and the email, an ordinary setting."""
    from kestrel.db.models import Setting
    from kestrel.llm.providers import masked, secret_present, secret_value

    present = secret_present(USAJOBS_ENV)
    row = await session.get(Setting, USAJOBS_EMAIL_KEY)
    email = str(row.value or "") if row is not None and isinstance(row.value, str) else ""
    return {
        "present": present, "masked": masked(secret_value(USAJOBS_ENV)) if present else "",
        "email": email, "ready": bool(present and email),
    }


async def save_usajobs_credentials(
    session: AsyncSession, *, key: str, email: str
) -> str | None:
    """Save what the user typed: the key through the helper the online-model keys use (data-home
    .env only), the email as a setting copied into every USAJobs search so each run can send it.
    An empty field keeps what is saved. Returns a plain sentence on refusal, else None."""
    import re

    from kestrel.db.base import utcnow_iso
    from kestrel.db.models import Setting
    from kestrel.llm import providers

    email = email.strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return "That does not look like an email address."
    if key:
        try:
            providers.write_secret(USAJOBS_ENV, key)
        except (ValueError, OSError) as exc:
            log.warning("usajobs_key_not_saved", error_type=type(exc).__name__)
            return plain_error(exc)
    if email:
        row = await session.get(Setting, USAJOBS_EMAIL_KEY)
        if row is None:
            session.add(Setting(key=USAJOBS_EMAIL_KEY, value=email))
        else:
            row.value = email
            row.updated_at = utcnow_iso()
        for src in (
            await session.execute(select(Source).where(Source.kind == "usajobs"))
        ).scalars():
            src.config = {**(src.config or {}), "contact_email": email}
            src.consecutive_failures = 0
        await session.commit()
    return None


async def adzuna_status(session: AsyncSession) -> dict:
    """The Adzuna card and the Add sheet's key panel: whether the app ID and app key are saved
    (only the last four characters of the key are ever shown). Neither is read from the database;
    both live in the data-home .env."""
    from kestrel.llm.providers import masked, secret_present, secret_value

    has_id, has_key = secret_present(ADZUNA_ID_ENV), secret_present(ADZUNA_KEY_ENV)
    return {
        "id_present": has_id, "key_present": has_key,
        "masked": masked(secret_value(ADZUNA_KEY_ENV)) if has_key else "",
        "ready": bool(has_id and has_key),
    }


async def save_adzuna_credentials(
    session: AsyncSession, *, app_id: str, app_key: str
) -> str | None:
    """Save what the user typed through the helper the online-model keys use (data-home .env
    only). An empty field keeps what is saved. Returns a plain sentence on refusal, else None."""
    from kestrel.llm import providers

    for env, value in ((ADZUNA_ID_ENV, app_id), (ADZUNA_KEY_ENV, app_key)):
        if not value:
            continue
        try:
            providers.write_secret(env, value)
        except (ValueError, OSError) as exc:
            log.warning("adzuna_key_not_saved", error_type=type(exc).__name__)
            return plain_error(exc)
    if app_id or app_key:
        for src in (
            await session.execute(select(Source).where(Source.kind == "adzuna"))
        ).scalars():
            src.consecutive_failures = 0
        await session.commit()
    return None


async def _checking(session: AsyncSession, *, held: bool = False) -> int:
    """Checks someone asked for that have not finished: scrape tasks waiting or running."""
    from kestrel.services.online_consent import APPROVAL_HOLD

    rows = (
        await session.execute(
            select(Task.last_error).where(
                Task.type == "scrape", Task.status.in_(("pending", "leased")),
            )
        )
    ).all()
    return sum((error == APPROVAL_HOLD) == held for (error,) in rows)


def _lane_dest(settings) -> str:
    """The destination (backend, base URL, model) the lane scores with on an online tier."""
    from kestrel.services.online_consent import destination_id

    tier = "tier2" if settings.llm.tiers.get("tier2") else "tier1"
    return destination_id(settings, tier)


def _lane_model(settings) -> str:
    """The model the lane would score with on an online tier: what the confirmation names and
    what the server checks the posted value against."""
    tier = settings.llm.tiers.get("tier2") or settings.llm.tiers.get("tier1")
    return str(getattr(tier, "model", "") or "")


def _lane_approval(settings) -> dict[str, list[str]]:
    from kestrel.services.online_consent import approval_for

    return approval_for(settings, ("tier1", "tier2"))


async def _lane_ctx(session: AsyncSession) -> dict:
    """The legacy backlog, its local scoring time and whether online settings hold it."""
    from kestrel.pipeline import lane
    from kestrel.services.setting_overrides import load_settings_with_db

    waiting = await lane.waiting_count(session)
    if not waiting:
        return {"waiting": 0}
    secs, measured = await lane.seconds_per_role(session)
    sends, model, dest, confidential, approval_json = "", "", "", False, "{}"
    try:
        settings = await load_settings_with_db(session)
        hosted = lane.scores_on_hosted(settings)
        if hosted:
            from kestrel.setup import tier_sends

            tiers = tier_sends(settings)
            sends = " ".join(v.rstrip(".") + "." for k, v in tiers.items()
                             if k in ("tier1", "tier2"))
            model = _lane_model(settings)
            dest = _lane_dest(settings)
            import json

            from kestrel.services.online_consent import approval_for
            approval_json = json.dumps(
                approval_for(settings, ("tier1", "tier2")), separators=(",", ":")
            )
            confidential = bool(settings.confidential_mode)
    except Exception:  # an unreadable setting must not take the page down; say the cautious thing
        hosted = True
    minutes = max(1, round(waiting * secs / 60))
    return {
        "waiting": waiting, "seconds": round(secs), "measured": measured, "hosted": hosted,
        "sends": sends, "model": model, "dest": dest, "confidential": confidential,
        "approval_json": approval_json,
        "minutes": minutes,
        "time": f"{minutes} minutes" if minutes < 120 else f"about {round(minutes / 60)} hours",
    }


def _page_track(request: Request, enabled: tuple[str, ...]) -> str:
    """The track tab the page reads (D-UI-57): the asked-for or remembered one while it is in
    use, else the first in use. A switched-off track's rows never show here."""
    from kestrel.web.deps import active_track

    track = active_track(request)
    return track if track in enabled else enabled[0]


def _groups(track: str, views: list[sv.SourceView]) -> list[dict]:
    """The page's groups for `track`: its standard ones (shown even when empty) plus any other
    group a row belongs to. Only sources that are on; the paused ones sit in their own group."""
    on = [v for v in views if v.enabled]
    order = [(k, t, d) for k, t, d in sv.GROUPS[track]]
    known = {k for k, _t, _d in order}
    order += [(v.group, sv.GROUP_TITLES[v.group], sv.GROUP_NOTES[v.group])
              for v in {v.group: v for v in on if v.group not in known}.values()]
    return [
        {"key": k, "title": t, "note": d, "icon": sv.GROUP_ICON[k], "add": sv.ADD_LABEL[k],
         "views": [v for v in on if v.group == k]}
        for k, t, d in order
    ]


async def _panel_ctx(
    request: Request,
    session: AsyncSession,
    *,
    create_error: str | None = None,
    create_values: dict | None = None,
    edit_error: dict | None = None,
    **extra,
) -> dict:
    from kestrel.services import source_suggestions

    enabled_tracks = await tracks_svc.load_enabled(session)
    track = _page_track(request, enabled_tracks)
    in_use = [s for s in await _all_sources(session) if s.track in enabled_tracks]
    sources = [s for s in in_use if s.track == track]
    generated = [s for s in in_use if s.origin == GENERATED]
    recent = await _recent_runs(session, in_use)
    latest = {sid: rs[0] for sid, rs in recent.items()}
    site_lines = await _site_lines(session, sources, latest)
    summaries = await _summaries(session) if generated else {}
    views = [
        sv.build_view(s, recent.get(s.id, []), summary=summaries.get(s.name, ""),
                      sites=site_lines.get(s.id))
        for s in sources
    ]
    counts = sv.health_counts(views)
    on_total = sum(1 for v in views if v.enabled)
    # Pre-rendered JSON for each row's edit form, built here so the template needs no filter.
    config_text = {s.id: json.dumps(s.config or {}, indent=2) for s in sources}
    ctx = {
        "track": track,
        "track_name": "Consulting" if track == "consulting" else "Employment",
        "views": views,
        "groups": _groups(track, views),
        "paused": [v for v in views if not v.enabled],
        "n_on": on_total,
        "n_paused": len(views) - on_total,
        "health": [
            {"key": k, "n": counts[k], "label": sv.HEALTH[k][0], "sub": sub,
             **sv.ring(counts[k], on_total)}
            for k, sub in (("g", "Working normally"), ("w", "A site asked Kestrel to wait"),
                           ("b", "Address moved or removed"))
        ],
        "type_options": [("", "All types")] + [(k, t) for k, t, _d in sv.GROUPS[track]],
        "origins": sv.ORIGINS,
        "suggestions": await source_suggestions.suggestions(
            session, track, [s.name for s in in_use]
        ),
        "found_boards": sum(1 for s in sources if s.origin == board_discovery.DISCOVERED),
        "found_cap": board_discovery.MAX_DISCOVERED,
        "generated": any(s.track == track for s in generated),
        "generated_on": any(s.enabled for s in generated),
        "config_text": config_text,
        "kinds": _visible_kinds(enabled_tracks),
        "tracks": enabled_tracks,
        "show_track": len(enabled_tracks) > 1,
        "consulting": "consulting" in enabled_tracks,
        "sec_titles_on": await sec_titles_opt_in(session),
        "sec_titles_label": SEC_TITLES_LABEL,
        "sam": _sam_status(),
        "rrs": await _rrs_status(session),
        "usajobs": await usajobs_status(session),
        "adzuna": await adzuna_status(session),
        "create_error": create_error,
        "create_values": create_values or {},
        "edit_error": edit_error,
        "signal_kinds": SIGNAL_SOURCE_KINDS,
    }
    ctx["lane"] = await _lane_ctx(session)
    import json as json_module

    from kestrel.services.online_consent import approval_for
    from kestrel.services.setting_overrides import load_settings_with_db

    current_settings = await load_settings_with_db(session)
    ctx["online_approval_json"] = json_module.dumps(
        approval_for(current_settings, ("tier1", "tier2", "trigger")), separators=(",", ":")
    )
    ctx["checking"] = await _checking(session)
    ctx["consent_held"] = await _checking(session, held=True)
    ctx.update(extra)
    return ctx


def _panel(request: Request, ctx: dict, status_code: int = 200) -> HTMLResponse:
    # htmx 2 does not swap a 4xx reply, so a refusal sent with 400 showed the operator nothing
    # (review finding 13). An htmx request always gets 200 and the message in the panel.
    if request.headers.get("HX-Request"):
        status_code = 200
    return templates().TemplateResponse(
        request, "partials/sources_panel.html", ctx, status_code=status_code
    )


@router.get("/sources", response_class=HTMLResponse)
async def page(request: Request):
    q = request.query_params
    message = None
    code = str(q.get("run_outcome") or "")
    rid = str(q.get("run_id") or "")
    if rid.isdigit() and code == "ran":
        message = {"id": int(rid), "text": "Ran just now."}
    elif rid.isdigit() and code in RUN_OUTCOMES:
        message = {"id": int(rid), "text": RUN_OUTCOMES[code][1]}
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(request, s, run_message=message)
    ctx["title"] = "Sources"
    ctx["query"] = dict(request.query_params)
    tmpl = "partials/sources_panel.html" if request.headers.get("HX-Request") else "sources.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/sources", response_class=HTMLResponse)
async def create_source(request: Request):
    """Create a source by hand. Validates BEFORE writing: a bad config re-renders the panel with a
    400 and a warnbox, and the operator's typed values are preserved in the form rather than lost.
    A row added here is `custom`: no rebuild ever touches it."""
    form = await request.form()
    name = str(form.get("name") or "").strip()
    track = str(form.get("track") or "").strip()
    kind = str(form.get("kind") or "").strip()
    cadence_raw = str(form.get("cadence_hours") or _DEFAULT_CADENCE).strip()
    config_raw = str(form.get("config") or "{}")
    enabled = bool(form.get("enabled"))

    cadence_hours, error = _validate_common(name, track, kind, cadence_raw)
    config, parse_error = _parse_config(config_raw)
    error = error or parse_error
    if error is None:
        error = _validate_source_config(kind, config)

    values = {
        "name": name, "track": track, "kind": kind,
        "cadence_hours": cadence_raw, "config": config_raw, "enabled": enabled,
    }

    async with sessionmaker()() as s:
        if error is None:
            s.add(
                Source(
                    track=track, kind=kind, name=name, config=config,
                    enabled=enabled, cadence_hours=cadence_hours, origin=CUSTOM,
                )
            )
            await s.commit()
            ctx = await _panel_ctx(request, s, add_message=f"Added {name}.")
            status_code = 200
        else:
            ctx = await _panel_ctx(request, s, create_error=error, create_values=values)
            status_code = 400
    return _panel(request, ctx, status_code)


# --------------------------------------------------------------- fixed paths before /{source_id}
@router.post("/sources/searches", response_class=HTMLResponse)
async def toggle_searches(request: Request):
    """The one switch for every generated search. Rebuilds from the profile at the same time, so
    switching on also brings the rows in line with the latest profile."""
    from kestrel.ingest.generated import rebuild_generated_sources

    form = await request.form()
    enable = str(form.get("enable") or "") in ("1", "on", "true")
    async with sessionmaker()() as s:
        await rebuild_generated_sources(s, enable=enable)
        await s.commit()
        ctx = await _panel_ctx(request, s)
    return _panel(request, ctx)


@router.post("/sources/score-everything", response_class=HTMLResponse)
async def score_everything_now(request: Request):
    """Recheck the legacy backlog and release survivors for local scoring only."""
    from kestrel.pipeline import lane

    form = await request.form()
    if str(form.get("confirm") or "") != "1":  # only the confirmation panel sends this
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(request, s)
        return _panel(request, ctx, 400)
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    if lane.scores_on_hosted(settings):
        await lane.release_all(sessionmaker(), hosted=True)
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(
                request, s, lane_message="Nothing was sent. Waiting background roles use local "
                "models only. Choose local models for both scoring tiers first."
            )
        return _panel(request, ctx, 400)
    credit.clear()  # the operator asked to score now: let an online key try again (D-LLM-8)
    n = await lane.release_all(sessionmaker(), online_approval=form.get("online_approval"))
    async with sessionmaker()() as s:
        msg = (f"Scoring {n} waiting role{'' if n == 1 else 's'} now." if n
               else "No roles were waiting.")
        ctx = await _panel_ctx(request, s, lane_message=msg)
    return _panel(request, ctx)


@router.post("/sources/sec-titles", response_class=HTMLResponse)
async def save_sec_titles(request: Request):
    """The SEC titles opt-in (review finding 3, D-SRC-6): off by default, the SEC watch searches
    neutral filing events only. On, the operator's target titles join its queries, which SEC sees
    beside the contact email its User-Agent rule requires. Rebuilds the generated searches."""
    from kestrel.ingest.generated import rebuild_generated_sources, save_sec_titles_opt_in

    form = await request.form()
    on = str(form.get("on") or "") in ("1", "on", "true")
    async with sessionmaker()() as s:
        await save_sec_titles_opt_in(s, on)
        await rebuild_generated_sources(s)
        await s.commit()
        ctx = await _panel_ctx(request, s)
    return _panel(request, ctx)


async def _http_client():
    from kestrel.ingest.http import HttpClient
    from kestrel.services.setting_overrides import load_settings_with_db

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    ing = settings.ingest
    return HttpClient(
        user_agent=ing.user_agent,
        default_rate_per_sec=ing.default_rate_per_sec,
        timeout=min(ing.request_timeout_s, 20.0),
        respect_robots=ing.respect_robots,
        max_attempts=1,  # one request per probe: MAX_PROBES is the request ceiling
    )


@router.post("/sources/company-board", response_class=HTMLResponse)
async def watch_company_board(request: Request):
    """Find a company's public job board from its name and add it as a custom source."""
    from kestrel.ingest.board_lookup import find_company_board

    form = await request.form()
    company = str(form.get("company") or "").strip()[:120]
    if not company:
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(
                request, s, lookup={"ok": False, "text": "Type a company name first."}
            )
        return _panel(request, ctx, 400)

    try:
        async with await _http_client() as http:
            result = await find_company_board(company, http)
    except Exception as exc:  # a dead network is a message, never a 500
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(request, s, lookup={
                "ok": False, "company": company,
                "text": f"The lookup could not run ({type(exc).__name__}). Check the network "
                        "and try again, or use Add a source with its careers page link.",
            })
        return _panel(request, ctx)

    async with sessionmaker()() as s:
        if result.match is None:
            ctx = await _panel_ctx(request, s, lookup={
                "ok": False, "company": company,
                "text": f"No public Greenhouse, Lever, Ashby or SmartRecruiters board was found "
                        f"for {company}. The company may use another system, or a different "
                        "name on its careers page. If you know the board address, use Add a "
                        "source and paste its careers page link.",
            })
            return _panel(request, ctx)
        m = result.match
        existing = [
            r for r in (
                await s.execute(select(Source).where(Source.kind == m.kind))
            ).scalars().all()
            if {k: (r.config or {}).get(k) for k in ("token", "company")}
            == {k: m.config.get(k) for k in ("token", "company")}
        ]
        if existing:
            text = f"{result.company} on {m.platform} is already on your list."
        else:
            s.add(Source(
                track="employment", kind=m.kind, name=f"{result.company} jobs ({m.platform})",
                # Off until the operator confirms it is the right company: a slug match is only
                # "this board has jobs", and "apex" may be someone else's Apex (finding 12).
                config=m.config, enabled=False, cadence_hours=_DEFAULT_CADENCE, origin=CUSTOM,
            ))
            await s.commit()
            jobs = f" with {m.jobs} open job{'s' if m.jobs != 1 else ''}" if m.jobs else ""
            ident = m.config.get("token") or m.config.get("company") or ""
            text = (f"Found a {m.platform} board called {ident}{jobs}. It is added below, "
                    "switched off. Check it is the right company, then turn it on and Kestrel "
                    "checks it once a day.")
        ctx = await _panel_ctx(request, s, lookup={"ok": True, "company": company, "text": text})
    return _panel(request, ctx)


@router.post("/sources/sam-gov/key", response_class=HTMLResponse)
async def save_sam_key(request: Request):
    """Save the SAM.gov key to the data-home .env (never the database, never YAML) through the
    helper the hosted-model keys use, then enable the SAM.gov source, creating it if needed."""
    from kestrel.llm.providers import write_secret

    form = await request.form()
    key = str(form.get("api_key") or "").strip()
    if not key:
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(
                request, s, sam_message={"ok": False, "text": "Paste the key from SAM.gov first."}
            )
        return _panel(request, ctx, 400)
    try:
        write_secret(SAM_ENV, key)
    except (ValueError, OSError) as exc:
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(request, s, sam_message={"ok": False, "text": plain_error(exc)})
        return _panel(request, ctx, 400)

    async with sessionmaker()() as s:
        rows = (
            await s.execute(select(Source).where(Source.kind == "sam_gov"))
        ).scalars().all()
        if not rows:
            s.add(Source(
                track="consulting", kind="sam_gov", name=SAM_NAME,
                config={"api_key_env": SAM_ENV, "naics": SAM_NAICS}, enabled=True,
                cadence_hours=_DEFAULT_CADENCE, origin=CUSTOM,
            ))
        for row in rows:
            row.enabled = True
            row.consecutive_failures = 0
        await s.commit()
        ctx = await _panel_ctx(request, s, sam_message={
            "ok": True,
            "text": "Key saved on this computer. SAM.gov is on and is checked once a day.",
        })
    return _panel(request, ctx)


@router.post("/sources/remoterocketship/key", response_class=HTMLResponse)
async def save_rrs_key(request: Request):
    """Save the user's RemoteRocketship key through the helper the hosted-model and SAM.gov keys
    use (atomic, data-home .env only). The key is never echoed, logged or stored in the source.
    Always 200 so the panel can show the message; a failure is a plain sentence."""
    from kestrel.llm import providers

    form = await request.form()
    key = str(form.get("api_key") or "").strip()
    message: dict
    if not key:
        message = {"ok": False, "text": "Paste your key first."}
    else:
        try:
            providers.write_secret(RRS_ENV, key)
            message = {"ok": True, "text": "Key saved on this computer."}
        except (ValueError, OSError) as exc:
            log.warning("rrs_key_not_saved", error_type=type(exc).__name__)
            message = {"ok": False, "text": plain_error(exc)}
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(request, s, rrs_message=message)
    return _panel(request, ctx)


@router.post("/sources/remoterocketship/toggle", response_class=HTMLResponse)
async def toggle_rrs(request: Request):
    """Turn RemoteRocketship on (create the source with default config, or re-enable it) or off.
    On needs a saved key; without one nothing changes and the card says so."""
    form = await request.form()
    want_on = str(form.get("on") or "") == "1"
    message: dict | None = None
    try:
        async with sessionmaker()() as s:
            src = (
                await s.execute(select(Source).where(Source.kind == "remoterocketship").limit(1))
            ).scalar_one_or_none()
            from kestrel.llm.providers import secret_present

            if want_on and not secret_present(RRS_ENV):
                message = {"ok": False, "text": "Add your key first."}
            elif want_on:
                if src is None:
                    s.add(Source(
                        track="employment", kind="remoterocketship", name=RRS_NAME,
                        config={}, enabled=True, cadence_hours=_DEFAULT_CADENCE, origin=CUSTOM,
                    ))
                else:
                    src.enabled = True
                    src.consecutive_failures = 0
                await s.commit()
                message = {"ok": True, "text": "On. It checks once a day."}
            elif src is not None:
                src.enabled = False
                await s.commit()
                message = {"ok": True, "text": "Off."}
    except Exception as exc:  # a write path never 500s: the card says so in plain words
        log.warning("rrs_toggle_failed", error_type=type(exc).__name__)
        message = {"ok": False, "text": "Could not change that. Try again."}
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(request, s, rrs_message=message)
    return _panel(request, ctx)


@router.post("/sources/usajobs/key", response_class=HTMLResponse)
async def save_usajobs_key(request: Request):
    """Save the user's USAJobs key and email from the card. The key is never echoed, logged or
    stored in a source. Always 200 so the panel can show the message in plain words."""
    form = await request.form()
    key = str(form.get("api_key") or "").strip()
    email = str(form.get("email") or "").strip()
    async with sessionmaker()() as s:
        if not key and not email:
            message = {"ok": False, "text": "Paste your key or type your email first."}
        else:
            error = await save_usajobs_credentials(s, key=key, email=email)
            message = (
                {"ok": False, "text": error} if error
                else {"ok": True, "text": "Saved on this computer."}
            )
        ctx = await _panel_ctx(request, s, usajobs_message=message)
    return _panel(request, ctx)


@router.post("/sources/adzuna/key", response_class=HTMLResponse)
async def save_adzuna_key(request: Request):
    """Save the user's Adzuna app ID and app key from the card. Neither is echoed, logged or
    stored in a source. Always 200 so the panel can show the message in plain words."""
    form = await request.form()
    app_id = str(form.get("app_id") or "").strip()
    app_key = str(form.get("app_key") or "").strip()
    async with sessionmaker()() as s:
        if not app_id and not app_key:
            message = {"ok": False, "text": "Paste your app ID and app key first."}
        else:
            error = await save_adzuna_credentials(s, app_id=app_id, app_key=app_key)
            message = (
                {"ok": False, "text": error} if error
                else {"ok": True, "text": "Saved on this computer."}
            )
        ctx = await _panel_ctx(request, s, adzuna_message=message)
    return _panel(request, ctx)


@router.post("/sources/rss", response_class=HTMLResponse)
async def add_rss(request: Request):
    form = await request.form()
    feeds = [u.strip() for u in str(form.get("feeds") or "").splitlines() if u.strip()]
    name = str(form.get("name") or "").strip() or "News feeds"
    bad = [u for u in feeds if not u.lower().startswith(("http://", "https://"))]
    if not feeds or bad:
        text = "Paste one feed address per line, each starting with https://."
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(request, s, add_message=text, add_error=True)
        return _panel(request, ctx, 400)
    return await _add_simple(request, "rss", name, {"feeds": feeds}, 24)


@router.post("/sources/pe-portfolio", response_class=HTMLResponse)
async def add_pe_portfolio(request: Request):
    form = await request.form()
    firm = str(form.get("firm") or "").strip()
    url = str(form.get("url") or "").strip()
    if not firm or not url.lower().startswith(("http://", "https://")):
        text = "Give the firm's name and its portfolio page address (starting with https://)."
        async with sessionmaker()() as s:
            ctx = await _panel_ctx(request, s, add_message=text, add_error=True)
        return _panel(request, ctx, 400)
    return await _add_simple(
        request, "pe_portfolio", f"{firm} portfolio", {"firms": [{"name": firm, "url": url}]}, 168
    )


async def _add_simple(
    request: Request, kind: str, name: str, config: dict, cadence: int
) -> HTMLResponse:
    error = _validate_source_config(kind, config)
    async with sessionmaker()() as s:
        if error:
            ctx = await _panel_ctx(request, s, add_message=error, add_error=True)
            return _panel(request, ctx, 400)
        s.add(Source(track="consulting", kind=kind, name=name, config=config, enabled=True,
                     cadence_hours=cadence, origin=CUSTOM))
        await s.commit()
        ctx = await _panel_ctx(request, s, add_message=f"Added {name}.")
    return _panel(request, ctx)


# ------------------------------------------------------------------------------- per-row routes
@router.post("/sources/{source_id}", response_class=HTMLResponse)
async def update_source(request: Request, source_id: int):
    """Update a source. Same validate-before-save contract as create; on failure the row reopens
    in edit mode with the attempted values and the reason, and nothing is written. A saved edit
    makes the row `custom`: an edited generated search no longer follows the profile."""
    form = await request.form()
    name = str(form.get("name") or "").strip()
    track = str(form.get("track") or "").strip()
    kind = str(form.get("kind") or "").strip()
    cadence_raw = str(form.get("cadence_hours") or _DEFAULT_CADENCE).strip()
    config_raw = str(form.get("config") or "{}")
    enabled = bool(form.get("enabled"))

    cadence_hours, error = _validate_common(name, track, kind, cadence_raw)
    config, parse_error = _parse_config(config_raw)
    error = error or parse_error
    if error is None:
        error = _validate_source_config(kind, config)

    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None:
            return HTMLResponse("source not found", status_code=404)

        if error is None:
            src.name = name
            src.track = track
            src.kind = kind
            src.config = config
            src.cadence_hours = cadence_hours
            src.enabled = enabled
            src.origin = CUSTOM
            await s.commit()
            ctx = await _panel_ctx(request, s)
            status_code = 200
        else:
            values = {
                "name": name, "track": track, "kind": kind,
                "cadence_hours": cadence_raw, "config": config_raw, "enabled": enabled,
            }
            ctx = await _panel_ctx(
                request, s, edit_error={"id": source_id, "message": error, "values": values}
            )
            status_code = 400
        if form.get("drawer") and request.headers.get("HX-Request"):
            from kestrel.web.pages import sources_ui

            dctx = await sources_ui._drawer_ctx(
                request, s, source_id, edit_error=ctx["edit_error"],
                note={"tone": "good", "text": "Saved."} if error is None else None,
            )
            return sources_ui.drawer_with_list(request, dctx)
    return _panel(request, ctx, status_code)


def toast_header(message: str, undo_url: str | None = None, **events) -> dict[str, str]:
    """An HX-Trigger header: a toast (with Undo when `undo_url` is given) and any named events.
    ensure_ascii keeps it latin-1 safe for the header."""
    toast: dict = {"message": message}
    if undo_url:
        toast["undoUrl"] = undo_url
    return {"HX-Trigger": json.dumps({"k-toast": toast, **events}, ensure_ascii=True)}


def list_response(
    request: Request, ctx: dict, headers: dict[str, str] | None = None
) -> HTMLResponse:
    """The groups alone (plus the health tiles and the counts, out of band): what a row action
    swaps, so the page, the toolbar and the filters stay where they are."""
    return templates().TemplateResponse(
        request, "partials/sources_list.html", ctx, headers=headers or {}
    )


@router.get("/sources/list", response_class=HTMLResponse)
async def source_list(request: Request):
    """The groups again, for Undo and for a change made elsewhere."""
    async with sessionmaker()() as s:
        ctx = await _panel_ctx(request, s)
    return list_response(request, ctx)


@router.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def toggle_source(request: Request, source_id: int):
    """Flip enabled. A disabled source is skipped by the scheduler; this is the manual override to
    re-enable one the auto-disable (3 consecutive failures) turned off, or to pause one by hand.
    A source is paused, never deleted. Script gets the groups and a toast with Undo."""
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None or sv.is_removed(src):
            return HTMLResponse("source not found", status_code=404)
        from kestrel.ingest.adapters.jobspy import NO_BOARDS_KEY, NO_BOARDS_REFUSED

        # Undo posts `set=<0|1>`: it puts back exactly the state the action changed, so a second
        # click or a later change cannot flip it again.
        want = request.query_params.get("set")
        if want in ("0", "1") and src.enabled == (want == "1"):
            ctx = await _panel_ctx(request, s)
            if request.headers.get("HX-Request"):
                return list_response(request, ctx)
            return _panel(request, ctx)
        if not src.enabled and (src.config or {}).get(NO_BOARDS_KEY):
            # LinkedIn was its only board: it stays off until the operator picks boards.
            ctx = await _panel_ctx(request, s, run_message={"id": source_id,
                                                            "text": NO_BOARDS_REFUSED})
            return _panel(request, ctx)
        src.enabled = not src.enabled
        if src.enabled:
            src.consecutive_failures = 0  # a manual re-enable gets a clean slate
        await s.commit()
        name, now_on = src.name, src.enabled
        title = sv.GROUP_TITLES.get(sv.group_of(src), "its group")
        ctx = await _panel_ctx(request, s)
    if not request.headers.get("HX-Request"):
        return _panel(request, ctx)
    message = (f"{name} is on again. It is back in {title}." if now_on
               else f"{name} is paused and moved to Paused. Nothing is deleted.")
    return list_response(request, ctx, toast_header(
        message, f"/sources/{source_id}/toggle?set={0 if now_on else 1}",
        **{"src-moved": {"id": source_id}}))


@router.post("/sources/{source_id}/default-boards", response_class=HTMLResponse)
async def use_default_boards(request: Request, source_id: int):
    """A job-board search left with no boards when LinkedIn was taken out: name Kestrel's
    default boards explicitly and turn it back on (release round review, finding 2)."""
    from kestrel.ingest.adapters.jobspy import DEFAULT_SITES, NO_BOARDS_KEY

    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None or src.kind != "jobspy":
            return HTMLResponse("source not found", status_code=404)
        config = {k: v for k, v in (src.config or {}).items() if k != NO_BOARDS_KEY}
        config["site_name"] = list(DEFAULT_SITES)
        src.config = config
        src.enabled = True
        src.consecutive_failures = 0
        await s.commit()
        ctx = await _panel_ctx(
            request, s, run_message={"id": source_id, "text": "Now searching Indeed and Glassdoor."}
        )
    return _panel(request, ctx)


# What a no-script Run now submit reports back: a code in the URL, fixed copy here (as Score
# again does, D-UI-71). `ok` False renders in the warning box.
RUN_OUTCOMES: dict[str, tuple[bool, str]] = {
    "no_pick": (False, "Not run: pick a model, or choose My usual models."),
    "signal": (
        False,
        "Not run: a one-run model is not available for this kind of search. "
        "Choose My usual models.",
    ),
    "bad_model": (False, "Not run: that model is not available right now. Pick another."),
    "not_confirmed": (False, "Not run: nothing was sent. Confirm the online send first."),
    "held": (False, "Not run: nothing was sent. Confidential mode is on and no current employer "
                    "is chosen. Choose it in Settings, then try again."),
    "hosted_off": (False, "Not run: nothing was sent. Online models need Hosted mode, "
                          "which is off."),
}


# The one-run model reaches the full review (Tier 2) only; the quick screen always runs on the
# configured Tier 1 model (D-UI-72 amended).
RUN_SCOPE_NOTE = (
    "Applies to the full review of the roles this run finds, and only to this run. The quick "
    "screen always uses your usual model. Your settings do not change, and later scans use your "
    "usual models."
)


def _signal_kind(kind: str | None) -> bool:
    return kind in SIGNAL_SOURCE_KINDS


async def _run_choice(
    s: AsyncSession, src: Source | None, form
) -> tuple[dict | None, str | None, tuple[str, str] | None]:
    """(override, its label, a refusal as (code, plain message)) for the Run now dialog's form.
    "My usual models" (or no choice at all) is no override. "A different model" is validated
    exactly as Score again validates it (rescore._override_for): a local pick must be one of
    Kestrel's derived models Ollama has, a hosted pick only while Hosted mode is on and
    acknowledged and the model is listed; an empty pick is refused, never read as "usual".
    `src` None is a whole scan (Today's Scan now): signal searches in it run on usual models.
    """
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.pages.rescore import HOSTED_PREFIX, _override_for, online_refusal

    if str(form.get("model_choice") or "usual") != "other":
        return None, None, None
    pick = str(form.get("model") or "").strip()[:240]
    if not pick:
        return None, None, ("no_pick", RUN_OUTCOMES["no_pick"][1])
    if src is not None and _signal_kind(src.kind):
        return None, None, ("signal", RUN_OUTCOMES["signal"][1])
    settings = await load_settings_with_db(s)
    if pick.startswith(HOSTED_PREFIX):
        # An online pick needs the ask-first step's confirmation, Hosted mode on, and a listed
        # model (the last is _override_for). Nothing is queued otherwise.
        code = await online_refusal(s, settings, form)
        if code:
            return None, None, (code, RUN_OUTCOMES[code][1])
    override, label, why = await _override_for(settings, pick)
    if override is None:
        return None, None, ("bad_model", f"Not run: {why}")
    return override, label, None


@router.get("/sources/{source_id}/run-now/choose", response_class=HTMLResponse)
async def run_now_choose(request: Request, source_id: int):
    """The Run now dialog: usual models (default) or one model for this run only, on the shared
    picker (the Score again dialog's contract). Without script (`page=1`, no HX-Request) it is
    a page that posts back to /sources."""
    import asyncio

    from kestrel.analytics.progress import tier2_label
    from kestrel.services.setting_overrides import load_settings_with_db
    from kestrel.web.pages import rescore as rs

    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None:
            return HTMLResponse("source not found", status_code=404)
        settings = await load_settings_with_db(s)
        consent = await rs._consent(s, settings)
    local, local_note = await asyncio.to_thread(rs._local_models, settings)
    label, _name, _raw, hosted, reasons, hosted_note = await rs._hosted_models(settings)
    bases = await rs._bases()
    page = request.query_params.get("page") == "1" and not request.headers.get("HX-Request")
    ctx = {
        **rs.usual_model_ctx(settings, bases),
        "consent": consent,
        "local_labels": {m: rs.friendly_name(m, bases) for m in local},
        "source": src,
        "source_label": src.name,
        "can_pick": not _signal_kind(src.kind),
        "scope_note": RUN_SCOPE_NOTE,
        "page": page,
        "current_label": tier2_label(settings),
        "local_models": local,
        "local_note": local_note,
        "hosted_label": label,
        "hosted_models": hosted,
        "hosted_reasons": reasons,
        "hosted_note": hosted_note,
        "hosted_prefix": rs.HOSTED_PREFIX,
        "hosted_while_local": settings.llm.mode != "hosted",
        "online_approval": consent.get("approval_json", "{}"),
    }
    if page:
        ctx.update(title="Run now", query=dict(request.query_params))
        return templates().TemplateResponse(request, "run_now_page.html", ctx)
    return templates().TemplateResponse(request, "partials/run_now_dialog.html", ctx)


@router.post("/sources/{source_id}/run-now", response_class=HTMLResponse)
async def run_now(request: Request, source_id: int):
    """Trigger one source's scan immediately, on the operator's usual models or on one model for
    this run only (D-UI-72). Calls the real runner (kestrel/ingest/runner.py's `run_source` for
    adapter kinds, kestrel/ingest/signal_runner.py's `run_signal_source` for consulting signal
    kinds) wrapped in a broad catch: a dead network, a down Ollama (needed only for signal-source
    classification), or any other failure becomes a message on the row, never a 500. The run
    itself already never aborts the batch on a recoverable error (SPEC.md 5.1); this wrapper only
    guards against something unexpected escaping that contract.

    A refused choice runs nothing and says why. A request without script (a form post with no
    HX-Request) answers with a redirect to /sources carrying a fixed outcome code."""
    from fastapi.responses import RedirectResponse

    form = await request.form()
    hx = bool(request.headers.get("HX-Request"))
    async with sessionmaker()() as s:
        src = await s.get(Source, source_id)
        if src is None:
            return HTMLResponse("source not found", status_code=404)
        override, label, refused = await _run_choice(s, src, form)
        if refused is None:
            from kestrel.pipeline._shared import settings_with_override
            from kestrel.services.online_consent import approval_problem
            from kestrel.services.setting_overrides import load_settings_with_db
            settings = await load_settings_with_db(s)
            effective = settings
            if override is not None:
                effective = settings_with_override(settings, "tier2", override)
            tiers = ("trigger",) if _signal_kind(src.kind) else ("tier1", "tier2")
            problem = await approval_problem(s, effective, tiers, form)
            if problem is not None:
                refused = (
                    "not_confirmed", "Not run: nothing was sent. Confirm the online send first."
                )

    if refused is not None:
        code, text = refused
        if not hx:
            return RedirectResponse(f"/sources?run_outcome={code}&run_id={source_id}",
                                    status_code=303)
        message = text
    else:
        approval = form.get("online_approval")
        if approval is None:
            message = await _trigger_run(source_id, override, label)
        else:
            message = await _trigger_run(
                source_id, override, label, online_approval=approval
            )
        if not hx:
            return RedirectResponse(f"/sources?run_outcome=ran&run_id={source_id}",
                                    status_code=303)

    async with sessionmaker()() as s:
        ctx = await _panel_ctx(request, s, run_message={"id": source_id, "text": message})
    return _panel(request, ctx)


async def _trigger_run(
    source_id: int, override: dict | None = None, label: str | None = None,
    online_approval: object = None,
) -> str:
    from kestrel.ingest.http import HttpClient
    from kestrel.ingest.runner import run_source
    from kestrel.ingest.signal_runner import run_signal_source
    from kestrel.services.setting_overrides import load_settings_with_db

    try:
        async with sessionmaker()() as s:
            settings = await load_settings_with_db(s)  # the setting-table layer: hosted rows
            src = await s.get(Source, source_id)
            if src is None:
                return "source not found"
            kind = src.kind
            from kestrel.services.online_consent import approval_covers, approval_for

            tiers = ("trigger",) if kind in SIGNAL_SOURCE_KINDS else ("tier1", "tier2")
            effective = settings
            if override is not None:
                from kestrel.pipeline._shared import settings_with_override

                effective = settings_with_override(settings, "tier2", override)
            expected = approval_for(effective, tiers)
            if expected and not approval_covers(expected, online_approval):
                return "Nothing was sent. The online destination changed. Open Run now again."
        async with HttpClient(
            user_agent=settings.ingest.user_agent,
            default_rate_per_sec=settings.ingest.default_rate_per_sec,
            timeout=settings.ingest.request_timeout_s,
            respect_robots=settings.ingest.respect_robots,
        ) as http:
            if kind in SIGNAL_SOURCE_KINDS:
                from kestrel.llm.client import LLMClient

                client = LLMClient(settings)
                outcome = await run_signal_source(
                    sessionmaker(), source_id, http=http, client=client, settings=settings
                )
            else:
                outcome = await run_source(
                    sessionmaker(), source_id, http=http, model_override=override,
                    model_label=label, online_approval=online_approval,
                )
        text = f"{outcome.status}: {outcome.counts}"
        if outcome.error:
            text += f" ({outcome.error})"
        return text
    except Exception as exc:  # never let a dead network/model/adapter 500 the run-now button
        log.warning("run_now_failed_immediately", source_id=source_id,
                    error_type=type(exc).__name__, error=str(exc))
        return f"queued, but the run failed immediately: {plain_error(exc)}"


# ------------------------------------------------------------------ Settings > Tracks I use
@router.post("/settings/tracks", response_class=HTMLResponse)
async def save_tracks(request: Request):
    """Save which tracks the operator uses (D-UI-28), then rebuild the generated searches so a
    switched-off track's searches go and a switched-on track's appear. At least one track: an
    empty choice is refused with a message rather than silently read as both."""
    from kestrel.ingest.generated import rebuild_generated_sources

    form = await request.form()
    picked = [t for t in TRACKS if t in form.getlist("tracks")]
    if not picked:
        return templates().TemplateResponse(
            request, "partials/settings_tracks.html",
            {"tracks_message": "Pick at least one track.", "tracks_ok": False},
            status_code=200 if request.headers.get("HX-Request") else 400,
        )
    async with sessionmaker()() as s:
        before = await tracks_svc.load_enabled(s)
        after = await tracks_svc.save_enabled(s, picked)
        await s.commit()
        try:
            await rebuild_generated_sources(s)
            await s.commit()
        except Exception as exc:  # the choice is saved; the searches catch up on the next save
            import structlog

            structlog.get_logger().warning("generated_sources_rebuild_failed", error=str(exc))
    response = templates().TemplateResponse(
        request, "partials/settings_tracks.html",
        {"tracks_message": "Saved.", "tracks_ok": True},
    )
    if tuple(before) != tuple(after):
        response.headers["HX-Refresh"] = "true"  # the menus and tabs change with the choice
    return response
