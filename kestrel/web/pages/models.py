"""Models page (SPEC-INTERFACES 2.1, SPEC.md 8.6): read-only history and comparison of the models
that have done the work at each tier, local via Ollama or hosted via the frontier backend, so the
operator can see which performs better before promoting one on the Bench page.

Every rate carries its n and anything under five labeled evaluations reads as insufficient (hard
rule 6). The history and comparison call no model and write no row; the hardware profile and
hosted settings are shown so the operator knows what is configured right now beside what has run.

The one writing panel is "Hosted models per tier" (D-UI-42): Override models fetches the
provider's model list with the stored key (services/hosted_models.py, cached ten minutes), and
Save all writes the picked tiers as overrides that Connect never clobbers. The list request
carries only the key, never operator or opportunity data."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.analytics.models_history import (
    BenchStatus,
    bench_summary,
    best_by_tier,
    max_labeled_by_tier,
    model_summary,
    recent_history,
)
from kestrel.config import Settings
from kestrel.llm import providers as prov
from kestrel.services import hosted_models as hm
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import plain_error

log = structlog.get_logger()

router = APIRouter()

_TRACKS = ("employment", "consulting")
_SINCE_CHOICES = (7, 30, 90)
_HISTORY_LIMIT = 100


def _active_profile() -> dict | None:
    """The hardware profile `kestrel setup` chose, or None when the module or the file is absent.
    The import is inside the try so a build without it degrades to "not recorded"."""
    try:
        from kestrel.llm.profiles import load_active_profile

        rec = load_active_profile()
    except Exception:  # noqa: BLE001 -- a missing or unreadable profile is a note, never a 500
        return None
    if rec is None:
        return None
    return {"profile": rec.profile, "reason": rec.reason, "chosen_at": rec.chosen_at}


def _provider(settings) -> dict | None:
    try:
        from kestrel.llm.providers import provider_status

        st = provider_status(settings)
    except Exception:  # noqa: BLE001 -- the page must render without the hosted summary
        return None
    return {
        "backend_name": st.backend_name, "provider": st.provider, "protocol": st.protocol,
        "base_url": st.base_url, "model": st.model, "env_name": st.env_name,
        "key_present": st.key_present,
    }


def _parse_since(raw: str | None) -> int | None:
    try:
        days = int(raw) if raw else None
    except ValueError:
        return None
    return days if days in _SINCE_CHOICES else None


def _provider_label(backend_name: str) -> str:
    return next(
        (p["label"] for p in prov.PRESETS.values() if p["backend"] == backend_name), backend_name
    )


async def _overrides_ctx(
    session: AsyncSession,
    settings: Settings,
    *,
    open_picker: bool = False,
    refresh: bool = False,
    message: str | None = None,
    ok: bool = True,
) -> dict:
    """Context for partials/model_overrides.html. Opening the picker fetches the model list
    (cached); a failure is shown inline, never a 500."""
    found = hm.catalog_backend(settings)
    ctx: dict = {
        "ov_rows": await hm.tier_rows(session, settings),
        "ov_backend": found[0] if found else None,
        "ov_label": _provider_label(found[0]) if found else "",
        "ov_open": False, "ov_models": [], "ov_reasons": {}, "ov_n_free": 0,
        "ov_free_default": settings.llm.hosted_mode.free_only,
        "ov_error": None, "ov_message": message, "ov_ok": ok,
    }
    if not open_picker or found is None:
        return ctx
    name, backend = found
    try:
        models = hm.picker_models(await hm.fetch_models(name, backend, refresh=refresh))
    except hm.ModelListError as exc:
        ctx["ov_error"] = plain_error(exc)
        return ctx
    if not models:
        ctx["ov_error"] = (
            f"{ctx['ov_label']} lists no chat model Kestrel can ask for JSON, so there is "
            "nothing to pick."
        )
        return ctx
    ctx.update(
        ov_open=True,
        ov_models=models,
        ov_n_free=sum(1 for m in models if m.free),
        ov_reasons={
            r.tier: {
                m.id: why for m in models if (why := hm.block_reason(m, r.tier, backend))
            }
            for r in ctx["ov_rows"]
        },
    )
    return ctx


async def _load(track: str | None, since_days: int | None) -> dict:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = await model_summary(s, track=track, since_days=since_days)
        history = await recent_history(s, limit=_HISTORY_LIMIT, track=track)
        overrides = await _overrides_ctx(s, settings)
    tiers = sorted({r.tier for r in rows})
    configured = {
        name: {"model": cfg.model, "backend": cfg.backend}
        for name, cfg in sorted(settings.llm.tiers.items())
    }
    try:
        bench: BenchStatus = bench_summary()
    except Exception as exc:  # noqa: BLE001 -- the gold-set listing is a note, never a 500
        log.warning("bench_gold_sets_unreadable", error_type=type(exc).__name__, error=str(exc))
        bench = BenchStatus(
            persisted=False, note=f"Gold sets could not be read ({plain_error(exc)})."
        )
    return {
        "rows": rows,
        "tiers": tiers,
        "best": best_by_tier(rows),
        "max_labeled": max_labeled_by_tier(rows),
        "history": history,
        "bench": bench,
        "configured": configured,
        "hardware": _active_profile(),
        "provider": _provider(settings),
        "n_evaluations": sum(r.n for r in rows),
        **overrides,
    }


@router.get("/models", response_class=HTMLResponse)
async def models(request: Request):
    track = request.query_params.get("track") or ""
    if track not in _TRACKS:
        track = ""
    since_days = _parse_since(request.query_params.get("since"))
    data = await _load(track or None, since_days)
    ctx = {
        "title": "Models", "query": dict(request.query_params),
        "track": track, "since": since_days, "since_choices": _SINCE_CHOICES,
        **data,
    }
    return templates().TemplateResponse(request, "models.html", ctx)


def _overrides_partial(request: Request, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, "partials/model_overrides.html", ctx)


@router.get("/models/hosted/overrides", response_class=HTMLResponse)
async def overrides_panel(request: Request):
    """The panel, closed, or with `open=1` the picker (fetching the list; `refresh=1` skips the
    cache)."""
    q = request.query_params
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        ctx = await _overrides_ctx(
            s, settings, open_picker=q.get("open") == "1", refresh=q.get("refresh") == "1"
        )
    return _overrides_partial(request, ctx)


@router.post("/models/hosted/overrides", response_class=HTMLResponse)
async def overrides_save(request: Request):
    """Save all: every row with a model picked AND its Save box ticked becomes an override.
    Ids are checked against the provider's list (the cache, or a fresh fetch) and each tier's
    context budget; anything else is refused by name. Rows not picked are untouched."""
    form = await request.form()
    picks = {
        tier: str(form.get(f"model.{tier}") or "").strip()[:200]
        for tier in hm.ASSIGNABLE_TIERS
        if str(form.get(f"model.{tier}") or "").strip() and form.get(f"save.{tier}") == "on"
    }
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        found = hm.catalog_backend(settings)
        if found is None:
            ctx = await _overrides_ctx(
                s, settings, message="Connect a hosted provider first.", ok=False
            )
            return _overrides_partial(request, ctx)
        if not picks:
            ctx = await _overrides_ctx(s, settings, message="Nothing picked, so nothing changed.")
            return _overrides_partial(request, ctx)
        name, backend = found
        try:
            models = await hm.fetch_models(name, backend)
        except hm.ModelListError as exc:
            ctx = await _overrides_ctx(
                s, settings, message=f"Nothing saved. {plain_error(exc)}", ok=False
            )
            return _overrides_partial(request, ctx)
        result = await hm.save_overrides(s, settings, name, backend, picks, models)
        settings = await load_settings_with_db(s)
        ctx = await _overrides_ctx(
            s, settings, message=result.message, ok=not result.refused
        )
    return _overrides_partial(request, ctx)


@router.post("/models/hosted/reset", response_class=HTMLResponse)
async def overrides_reset(request: Request):
    form = await request.form()
    tier = str(form.get("tier") or "").strip()
    async with sessionmaker()() as s:
        ok, msg = await hm.reset_override(s, tier)
        settings = await load_settings_with_db(s)
        ctx = await _overrides_ctx(s, settings, message=msg, ok=ok)
    return _overrides_partial(request, ctx)
