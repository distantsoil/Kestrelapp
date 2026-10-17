"""Benchmark harness page (SPEC-INTERFACES 2.1; SPEC.md 8.6; SPEC-MODULES 5.5/5.7). Pick a model,
backend, and prompt version per tier, run against the gold set, see the comparison table, promote a
model to a tier.

Honesty constraint: `kestrel/bench/harness.py` and `evals/goldset.<track>.jsonl` now exist (see
`kestrel bench --models a,b [--prompt-version v]`), but this page's Run button deliberately does
NOT call it. A real bench run drives a live Ollama call per gold-set item per model at tier-2
latency (SPEC.md 15-40s/item) -- multiple minutes for one comparison -- and the API/worker/every DB
session share uvicorn's single event loop (CLAUDE.md), so blocking a web request on that would stall
the whole running app, including the pipeline worker. Running the harness from the web layer needs a
background-task surface (like `/runs`), which is future work, not "wire it up here." This page
therefore still never fabricates a benchmark number: the comparison table is the real "agreement
with the gold set" metric (SPEC.md 8.6) computed from whatever `evaluation` rows already overlap
with the operator's own `label` rows on the same opportunity + track (SPEC-MODULES 5.5: the gold set
IS the accumulated labels). If nothing overlaps yet, the table is honestly empty. The Run button
re-reads this live data and states plainly that no model was called from the web page -- use
`kestrel bench` for a real run.

Model choice is a picker, never free text (D-UI-44): the local models Ollama reports form one
group, and the hosted provider's list (services/hosted_models.py, the Models page's cached list;
fetched only on "Load hosted models") forms another, its values prefixed `hosted:`. Promoting a
hosted model saves it as that tier's hosted override, validated exactly as the Models page does.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Label, Setting
from kestrel.llm import providers as prov
from kestrel.llm.prompts import all_versions
from kestrel.services import hosted_models as hm
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates

router = APIRouter()

# The evaluation tiers a comparison run is meaningful for (embed/docsel/trigger are not verdict-
# and-score models, so they are out of scope for the gold-set agreement metric SPEC.md 8.6 defines).
BENCH_TIERS = ("tier1", "tier2", "tier3")

NO_HARNESS_NOTICE = (
    "This page's Run button does not call the live benchmark harness: a real run drives one Ollama "
    "call per gold-set item per model at tier-2 latency (multiple minutes), which would block the "
    "shared web/worker event loop if triggered from here. Use `kestrel bench --models a,b "
    "[--prompt-version v]` for a real run against evals/goldset.<track>.jsonl. Clicking Run here "
    "makes no model call and invents no numbers: the table below is the real agreement between "
    "stored evaluations and the operator's own labels for whatever has accumulated so far "
    "(SPEC-MODULES 5.5) -- it is empty until both exist for the same opportunity."
)


async def _comparison_rows(session: AsyncSession) -> list[dict]:
    """Group every (evaluation, label) pair that shares an opportunity_id and track by
    (tier, model, prompt_version, track) and report exact-verdict agreement and mean absolute score
    error, with the sample size stated plainly (hard rule 6: n < 5 is labeled insufficient, never
    rendered as if it were solid). A given opportunity may carry more than one label over time (e.g.
    a calibration override); every pairing is counted, so this is an approximation of the eventual
    harness metric, not a precise replacement for it."""
    evaluations = (await session.execute(select(Evaluation))).scalars().all()
    if not evaluations:
        return []
    labels = (await session.execute(select(Label))).scalars().all()
    labels_by_key: dict[tuple[int, str], list[Label]] = defaultdict(list)
    for lbl in labels:
        labels_by_key[(lbl.opportunity_id, lbl.track)].append(lbl)
    if not labels_by_key:
        return []

    groups: dict[tuple[int, str, str, str], list[tuple[Evaluation, Label]]] = defaultdict(list)
    for ev in evaluations:
        for lbl in labels_by_key.get((ev.opportunity_id, ev.track), ()):
            key = (ev.tier, ev.model or "(unknown model)", ev.prompt_version or "(unknown version)",
                   ev.track)
            groups[key].append((ev, lbl))

    rows: list[dict] = []
    for (tier, model, prompt_version, track), pairs in sorted(groups.items()):
        n = len(pairs)
        exact = sum(
            1 for ev, lbl in pairs if ev.verdict and lbl.verdict and ev.verdict == lbl.verdict
        )
        scored = [
            (ev.score, lbl.score) for ev, lbl in pairs
            if ev.score is not None and lbl.score is not None
        ]
        mae = sum(abs(a - b) for a, b in scored) / len(scored) if scored else None
        rows.append({
            "tier": tier, "model": model, "prompt_version": prompt_version, "track": track,
            "n": n, "sufficient": n >= 5,
            "exact_match_rate": exact / n if n else None,
            "mae": mae,
        })
    return rows


def _available_models(settings) -> tuple[list[str], str | None]:
    """Models the local Ollama reports right now, or ([], reason) if it cannot be reached. Never
    raises: a not-running model must degrade this page gracefully, never 500 it. `settings` are
    the app's (load_settings_with_db), so Hosted mode switched on in the app counts."""
    from kestrel.doctor import hosted_only
    from kestrel.llm.client import OllamaClient

    if hosted_only(settings):
        # Every tier is hosted: Ollama is not part of this setup, so it is not probed and its
        # absence is not reported (D-SETUP-8); the hosted picker is the only choice.
        return [], None
    try:
        backend = settings.llm.backends["local"]
        client = OllamaClient(
            backend.base_url, settings.llm.ollama_native_url, backend.api_key or "ollama"
        )
        return client.list_models(), None
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any reachability failure degrades
        return [], f"The AI engine is not running ({exc})."


HOSTED_PREFIX = "hosted:"


async def _hosted_models(settings, *, fetch: bool) -> tuple[str, list, str | None]:
    """(provider label, picker models, error). Without `fetch` only a cached list is used, so a
    plain page load makes no network call."""
    found = hm.catalog_backend(settings)
    if found is None:
        return "", [], None
    name, backend = found
    label = next((p["label"] for p in prov.PRESETS.values() if p["backend"] == name), name)
    if not fetch:
        return label, hm.picker_models(hm.cached(name, backend) or []), None
    try:
        return label, hm.picker_models(await hm.fetch_models(name, backend)), None
    except hm.ModelListError as exc:
        return label, [], str(exc)


async def _page_ctx(request: Request) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await _comparison_rows(s)
    # The Ollama tag list is a sync httpx call; never on the event loop (CLAUDE.md).
    models, models_error = await asyncio.to_thread(_available_models, settings)
    hosted_label, hosted, hosted_error = await _hosted_models(
        settings, fetch=request.query_params.get("hosted") == "1"
    )
    return {
        "hosted_label": hosted_label, "hosted_models": hosted, "hosted_error": hosted_error,
        "hosted_prefix": HOSTED_PREFIX,
        "title": "Benchmark", "query": dict(request.query_params),
        "tiers": BENCH_TIERS,
        "tier_config": {t: settings.llm.tiers.get(t) for t in BENCH_TIERS},
        "backends": sorted(settings.llm.backends.keys()),
        "prompt_versions": all_versions(),
        "available_models": models, "models_error": models_error,
        "rows": rows, "notice": NO_HARNESS_NOTICE, "ran": False,
    }


@router.get("/bench", response_class=HTMLResponse)
async def page(request: Request):
    ctx = await _page_ctx(request)
    tmpl = "partials/bench_table.html" if request.headers.get("HX-Request") else "bench.html"
    return templates().TemplateResponse(request, tmpl, ctx)


@router.post("/bench/run", response_class=HTMLResponse)
async def run(request: Request):
    """"Run against the gold set." Since no harness exists (see module docstring), this never calls
    a model -- it re-reads the real evaluation/label overlap and says exactly that, honoring the
    "never fabricate results" instruction rather than pretending a run happened."""
    form = await request.form()
    ctx = {
        "tier": str(form.get("tier") or ""),
        "model": str(form.get("model") or "").removeprefix(HOSTED_PREFIX),
        "backend": str(form.get("backend") or ""),
        "prompt_version": str(form.get("prompt_version") or ""),
        "notice": NO_HARNESS_NOTICE, "ran": True,
    }
    try:
        async with sessionmaker()() as s:
            ctx["rows"] = await _comparison_rows(s)
    except Exception as exc:  # noqa: BLE001 -- a DB hiccup must render a message, never a 500
        ctx["rows"] = []
        ctx["notice"] = f"{NO_HARNESS_NOTICE} Also could not read stored evaluations: {exc}"
    return templates().TemplateResponse(request, "partials/bench_table.html", ctx)


@router.post("/bench/promote", response_class=HTMLResponse)
async def promote(request: Request):
    """Record the operator's chosen model for a tier as a setting-table row -- the
    highest-precedence layer (SPEC.md 4.6) -- so it takes effect on the next request/task lease
    with no restart. Never 500s: a bad tier/model or a DB problem renders a warnbox instead."""
    form = await request.form()
    tier = str(form.get("tier") or "").strip()
    model = str(form.get("model") or "").strip()
    if tier not in BENCH_TIERS:
        return HTMLResponse('<div class="warnbox">Pick one of tier1, tier2, tier3 first.</div>')
    if not model:
        return HTMLResponse('<div class="warnbox">Pick a model to promote first.</div>')
    if model.startswith(HOSTED_PREFIX):
        return await _promote_hosted(tier, model.removeprefix(HOSTED_PREFIX))
    key = f"llm.tiers.{tier}.model"
    try:
        async with sessionmaker()() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=model))
            else:
                row.value = model
            await s.commit()
    except Exception as exc:  # noqa: BLE001 -- surface as a warnbox, never a 500
        return HTMLResponse(f'<div class="warnbox">Could not save: {exc}</div>')
    ctx = {"tier": tier, "model": model, "key": key}
    return templates().TemplateResponse(request, "partials/bench_promote_result.html", ctx)


async def _promote_hosted(tier: str, model_id: str) -> HTMLResponse:
    """A hosted pick becomes that tier's hosted override (the Models page's rules: the id must be
    in the provider's list and fit the tier's context budget)."""
    from html import escape

    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        found = hm.catalog_backend(settings)
        if found is None:
            return HTMLResponse('<div class="warnbox">Connect a hosted provider first.</div>')
        name, backend = found
        try:
            models = await hm.fetch_models(name, backend)
        except hm.ModelListError as exc:
            return HTMLResponse(f'<div class="warnbox">{escape(str(exc))}</div>')
        result = await hm.save_overrides(s, settings, name, backend, {tier: model_id}, models)
    box = "warnbox" if result.refused else "okbox"
    return HTMLResponse(
        f'<div class="{box}" data-testid="bench-promote-hosted">{escape(result.message)} '
        'See <a href="/models#hosted-overrides">Models</a> to reset it.</div>'
    )
