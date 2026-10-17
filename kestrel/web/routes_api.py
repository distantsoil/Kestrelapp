"""REST API under /api (SPEC-INTERFACES 3). Everything the UI does is here as JSON; the UI is a
consumer of this surface, not a privileged path, so the CLI, MCP server, and extension get full
capability. The override endpoint content-negotiates: an HTMX form post gets an HTML partial back,
a JSON client gets JSON."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kestrel import __version__
from kestrel.db.enums import VERDICTS
from kestrel.db.models import Opportunity
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.fit import fit_word_for_verdict, verdict_for_fit
from kestrel.web.queries import (
    OppFilters,
    get_detail,
    list_opportunities,
    list_runs,
    live_activity,
    lookup_by_urls,
    record_override,
)
from kestrel.web.redact import confidential, redact_name, redact_text

router = APIRouter()


def filters_from_query(params) -> OppFilters:
    def num(name):
        v = params.get(name)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def integer(name, default):
        try:
            return int(params.get(name) or default)
        except (TypeError, ValueError):
            return default

    return OppFilters(
        track=params.get("track") or None,
        stage=params.get("stage") or None,
        verdict=params.get("verdict") or None,
        score_min=num("score_min"),
        score_max=num("score_max"),
        org=params.get("org") or None,
        source=params.get("source") or None,
        needs_review=params.get("needs_review") in ("1", "true", "on", "True"),
        include_archived=params.get("archived") in ("1", "true", "on", "True"),
        hidden_only=params.get("hidden") in ("1", "true", "on", "True"),
        ghost_min=num("ghost_min"),
        q=params.get("q") or None,
        sort=params.get("sort") or "score",
        page=integer("page", 1),
        page_size=integer("page_size", 50),
    )


def _row_json(row, mapping: dict[str, str] | None = None, *, conf: bool = False) -> dict:
    """Serialize one row. `conf`/`mapping` carry confidential mode.

    The UI is a CONSUMER of this API, not a privileged path, so the two disagreeing about what the
    operator's current employer is called was the bug: /opportunities rendered "[Confidential]"
    while /api/opportunities returned the real name to anything that asked.
    """
    o, e = row.opp, row.ev
    return {
        "id": o.id,
        "track": o.track,
        "title": redact_text(o.title, mapping or {}, enabled=conf),
        "org": redact_name(row.org_name or o.org_name, mapping or {}, enabled=conf),
        "source": row.source_name,
        "stage": o.stage,
        "verdict": e.verdict if e else None,
        "score": e.score if e else None,
        "weighted_score": e.weighted_score if e else None,
        "needs_review": bool(e.needs_review) if e else False,
        "ghost_score": o.ghost_score,
        "pre_score": o.pre_score,
        "url": o.url,
    }


@router.get("/health")
async def health():
    # `service` is the identity marker the browser extension probes for. The extension POSTs the
    # text of the page the operator is reading, so before it sends anything it must confirm the
    # thing listening on a candidate port is actually Kestrel and not some unrelated local server
    # that happens to answer /health.
    return {"status": "ok", "service": "kestrel", "version": __version__}


@router.post("/api/quit")
async def quit_app():
    """The in-app "Quit Kestrel" button: the same real quit as the tray's Quit (D-UI-27), never
    the close-to-tray hide. Headless serve has no shell to quit and says so."""
    from kestrel.web.desktop_bridge import request_quit

    return {"quitting": request_quit()}


@router.post("/api/focus")
async def focus():
    # A second launch POSTs here to raise the running window (SPEC-INTERFACES 1.1 F5). The desktop
    # shell registers show_window via desktop_bridge; headless `kestrel serve` has no handler, so
    # this is a harmless no-op there.
    from kestrel.web.desktop_bridge import request_focus

    return {"focused": request_focus()}


@router.post("/api/opportunities/lookup")
async def opportunities_lookup(request: Request):
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    urls = body.get("urls") if isinstance(body, dict) else None
    async with sessionmaker()() as s:
        return await lookup_by_urls(s, list(urls or []))


@router.post("/api/opportunities/ingest")
async def opportunities_ingest(request: Request):
    """Ingest one captured page (the extension's capture button; the MCP ingest_opportunity tool):
    a url and/or the readable text of a page the operator is already viewing. It enters the pipeline
    like any scraped posting. Reads a handed-over page only; never crawls or acts externally."""
    from kestrel.ingest.manual import manual_ingest

    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict) or not (body.get("url") or body.get("text")):
        return JSONResponse({"error": "provide a url or text"}, status_code=400)
    async with sessionmaker()() as s:
        opp = await manual_ingest(
            s, url=body.get("url"), text=body.get("text"), title=body.get("title"),
            org_name=body.get("org_name"), track=body.get("track") or "employment",
        )
    if opp is None:
        return JSONResponse({"error": "nothing to ingest"}, status_code=400)
    return {"id": opp.id, "title": opp.title, "stage": opp.stage, "track": opp.track}


@router.get("/api/opportunities")
async def api_opportunities(request: Request):
    f = filters_from_query(request.query_params)
    async with sessionmaker()() as s:
        page = await list_opportunities(s, f)
        conf, mapping = await confidential(s)
    return {
        "total": page.total,
        "page": page.page,
        "pages": page.pages,
        "page_size": page.page_size,
        "rows": [_row_json(r, mapping, conf=conf) for r in page.rows],
    }


@router.get("/api/opportunities/{opp_id}")
async def api_opportunity(opp_id: int):
    async with sessionmaker()() as s:
        detail = await get_detail(s, opp_id)
        if detail is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        conf, mapping = await confidential(s)
    o, e = detail["opp"], detail["ev"]
    org = detail["org"].name if detail.get("org") else o.org_name
    return {
        "id": o.id,
        "title": redact_text(o.title, mapping, enabled=conf),
        "org": redact_name(org, mapping, enabled=conf),
        "track": o.track,
        "stage": o.stage,
        "verdict": e.verdict if e else None,
        "verdict_model": e.verdict_model if e else None,
        "score": e.score if e else None,
        "weighted_score": e.weighted_score if e else None,
        "needs_review": bool(e.needs_review) if e else False,
        # The redacted display copy from get_detail: the Tier 2 prose names the employer.
        "dimensions": detail["ev_json"]["dimensions"],
        "blockers": detail["ev_json"]["blockers"],
        "legitimacy": detail["ev_json"]["legitimacy"],
        "dedup_members": [m.id for m in detail["dedup_members"]],
        "override": _override_json(detail["override"]),
    }


async def oob_nav_counts(session) -> str:
    """The sidebar count badges as out-of-band swaps (the /status/counts markup), for a write
    response whose change moves a badge."""
    from kestrel.web.routes_control import _nav_counts

    counts = await _nav_counts(session)
    return "".join(
        f'<span id="nav-count-{key}" class="n" hx-swap-oob="true">{n if n else ""}</span>'
        for key, n in counts.items()
    )


def _override_json(row) -> dict | None:
    if row is None:
        return None
    return {
        "operator_verdict": row.operator_verdict,
        "operator_score": row.operator_score,
        "reason": row.reason,
        "created_at": row.created_at,
    }


@router.post("/api/opportunities/{opp_id}/override")
async def api_override(opp_id: int, request: Request):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    else:
        data = dict(await request.form())
    if not isinstance(data, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    mode = str(data.get("mode") or "agree")
    reason = (data.get("reason") or "").strip() or None
    # The Disagree control sends a fit word (D-UI-45); it is mapped to the stored verdict and a
    # training score below, once the opportunity's track is known. Anything outside the four
    # words is refused like an unknown verdict.
    fit_word = (data.get("fit") or "").strip().lower() or None
    if fit_word is not None:
        mode = "override"
        try:
            verdict_for_fit(fit_word)
        except ValueError:
            return JSONResponse({"error": f"not a fit word: {fit_word}"}, status_code=422)
    if mode == "override" and not reason:
        # A full verdict override is a disagreement label; the "why" is the point (learning loop).
        return JSONResponse({"error": "override requires a reason"}, status_code=400)
    operator_verdict = data.get("operator_verdict") or None
    if operator_verdict is not None and operator_verdict not in VERDICTS:
        # The override is a label the learning loop trains on; an arbitrary string here would
        # poison calibration silently rather than fail loudly.
        return JSONResponse(
            {"error": f"operator_verdict must be one of {', '.join(VERDICTS)}"}, status_code=422
        )
    score = data.get("operator_score")
    try:
        score = float(score) if score not in (None, "") else None
    except (TypeError, ValueError):
        score = None
    async with sessionmaker()() as s:
        if fit_word is not None:
            opp = await s.get(Opportunity, opp_id)
            if opp is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            operator_verdict, score = verdict_for_fit(fit_word, opp.track)
        row = await record_override(
            s,
            opp_id=opp_id,
            mode=mode,
            operator_verdict=operator_verdict,
            operator_score=score,
            reason=reason,
        )
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        override_json = _override_json(row)
        word = fit_word_for_verdict(row.operator_verdict, row.operator_score)
        # The row's "you said" pill and the badges follow the save (D-UI-45): the badges out of
        # band, the list through its pipelineRefresh listener.
        oob = await oob_nav_counts(s) if request.headers.get("HX-Request") else ""
    if request.headers.get("HX-Request"):
        body = templates().TemplateResponse(
            request, "partials/override_done.html", {"override": row, "word": word}
        ).body.decode("utf-8")
        return HTMLResponse(body + oob, headers={"HX-Trigger": "pipelineRefresh"})
    override_json["fit_word"] = word
    return {"ok": True, "override": override_json}


@router.get("/api/runs")
async def api_runs():
    async with sessionmaker()() as s:
        runs = await list_runs(s)
    return {
        "runs": [
            {
                "id": r.id, "name": r.name, "track": r.track, "status": r.status,
                "started_at": r.started_at, "finished_at": r.finished_at, "counts": r.counts,
            }
            for r in runs
        ]
    }


@router.get("/api/runs/active")
async def api_runs_active():
    async with sessionmaker()() as s:
        return await live_activity(s)
