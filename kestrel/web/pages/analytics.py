"""Analytics page (SPEC-INTERFACES 2.1, SPEC-MODULES 7). The honest summary leads the page: the
code-computed "not discriminating" / "insufficient data" verdicts from `honest_summary` are
authoritative and rendered first, so the narrative can never quietly read as good news when the
numbers say the score is not working (hard rule 6). Below it: the funnel, response rate by score
band, breakdowns by source/size/ownership/remote, and cost/quality/time — every rate shown beside
the sample size it rests on, anything under five observations labeled insufficient.

`honest_summary` already degrades gracefully when the local model is not reachable (a GuardResult
with ok=False from `run_guarded`'s own try/except, never a raised exception — see llm/guard.py),
returning a code-built narrative instead. The try/except here is a second, outer safety net for
anything that raises before that point (a settings problem, an empty/unmigrated database), so this
page never 500s for that reason; it degrades to `collect_metrics` plus a plain note instead."""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kestrel.analytics.funnel import funnel
from kestrel.analytics.metrics import collect_metrics
from kestrel.analytics.summary import honest_summary
from kestrel.llm.client import LLMClient
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import active_track, sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

router = APIRouter()

_TRACKS = ("employment", "consulting")


async def _load(track: str, *, narrative: bool = False) -> dict:
    """Assemble the honest summary, the funnel, and the underlying metrics for one track. Never
    raises: a not-running model or a problem building the summary falls back to plain
    `collect_metrics` plus an explanatory note rather than a 500 (hard rule 4 in spirit —
    uncertainty here is surfaced, not treated as failure). The page itself never calls the
    model (`narrative=False`): the code verdicts show at once and the narrative is written on
    request (D-UI-19)."""
    async with sessionmaker()() as s:
        summary = None
        model_note: str | None = None
        try:
            if narrative:
                async with httpx.AsyncClient() as http:
                    client = LLMClient(await load_settings_with_db(s), http=http)
                    summary = await honest_summary(s, client, track)
            else:
                summary = await honest_summary(s, None, track)
            metrics = summary.metrics
        except Exception as exc:  # a problem outside honest_summary's own model-call guard
            metrics = await collect_metrics(s, track)
            log.warning("analytics_summary_failed", error_type=type(exc).__name__, error=str(exc))
            model_note = (
                f"Summary unavailable ({plain_error(exc)}); showing computed metrics only."
            )
        fn = await funnel(s, track)
    return {"summary": summary, "metrics": metrics, "fn": fn, "model_note": model_note}


@router.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    track = active_track(request)
    if track not in _TRACKS:
        track = "employment"
    data = await _load(track)
    ctx = {"title": "Analytics", "query": dict(request.query_params), "track": track, **data}
    tmpl = "partials/analytics_body.html" if request.headers.get("HX-Request") else "analytics.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/analytics/narrative", response_class=HTMLResponse)
async def analytics_narrative(request: Request):
    """Write the P12 narrative on request: the one place the Analytics page calls the model."""
    track = active_track(request)
    if track not in _TRACKS:
        track = "employment"
    data = await _load(track, narrative=True)
    ctx = {"track": track, "svo": data["metrics"]["score_vs_outcome"], **data}
    return templates().TemplateResponse(request, "partials/analytics_summary.html", ctx)
