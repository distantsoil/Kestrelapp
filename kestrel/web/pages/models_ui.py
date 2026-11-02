"""Models page, the parts that act (D-UI-105, D-UI-147): Health check, downloads, the online key,
the Change model drawer and the speed setting. Each swaps its own section in place.

  GET  /models/health            the Health check card (polled while a check or fix runs)
  POST /models/health/run        Run check: the preflight in a background thread
  POST /models/health/fix        Fix it: rebuild Kestrel's models, then test again
  GET  /models/downloads         the Models on this computer card (polled while work runs)
  POST /models/download          Download what is missing, then tune it automatically
  POST /models/download/pause    Pause (a later Resume continues where it stopped)
  POST /models/online/key        Test, Save or Remove a provider key (never echoed back)
  GET  /models/change/{tier}     the Change model drawer body
  POST /models/change/{tier}     Save a pick (local, or online after the operator confirmed)
  POST /models/change/{tier}/undo | /reset
  POST /models/speed             roles scored at once

Nothing here sends operator data anywhere: an online model is only recorded, and the ask before
anything is sent stays where the send happens (hard rule 1, Privacy). A model list request carries
only the key."""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Concurrency, Settings
from kestrel.date_display import format_display
from kestrel.db.models import Run, Setting
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.services import hosted_models as hm
from kestrel.services import model_tasks as mt
from kestrel.services import setup_state as ss
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web import bench_view
from kestrel.web import models_view as mv
from kestrel.web.deps import sessionmaker, templates
from kestrel.web.errors import fixed_reason, plain_error

log = structlog.get_logger()
router = APIRouter()

UNDO_PREFIX = "models.undo."
# In-process conveniences only: what to do when the work now running ends. Nothing that matters
# lives here (hard rule 5): a restart just leaves the buttons for the operator to press again.
_after: dict[str, bool] = {"retest": False, "tune": False}


def _tier_key(tier: str) -> str | None:
    return tier if tier in mv.CHANGEABLE else None


def _frag(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates().TemplateResponse(request, f"partials/{name}", ctx)


async def _state() -> tuple[ss.SetupState, dict[str, Any]]:
    from kestrel.web.pages.setup import models_snapshot, setup_state

    return await setup_state(), await models_snapshot()


# ======================================================================================= health
async def health_ctx(message: str = "", ok: bool = True) -> dict[str, Any]:
    from kestrel.web.pages.setup import action_ctx, models_snapshot

    snap = await models_snapshot()
    ctx = action_ctx(snap)
    # A fix that finished starts the re-test by itself; one that failed just stops.
    if _after["retest"] and not ctx["running"]:
        _after["retest"] = False
        if not snap.get("failed"):
            try:
                ss.verify()
                ctx = action_ctx(snap)
            except ss.ActionBusy:
                pass
    report = ss.load_report()
    rows = mv.health_rows(report)
    return {
        "hc_rows": rows,
        "hc_running": ctx["running"],
        "hc_fixing": bool(snap.get("live") and snap.get("kind") == "build"),
        "hc_percent": next(
            (i["percent"] for i in snap.get("items", []) if i.get("percent") is not None), None),
        "hc_when": format_display((report or {}).get("ran_at"), kind="datetime", time_style="24h"),
        "hc_failed": any(r["state"] == "fail" for r in rows),
        "hc_any_fix": any(r["fixable"] for r in rows),
        "hc_message": message, "hc_ok": ok,
        "hc_log": (ctx["action"] or {}).get("lines", [])[-12:] if ctx["action"] else [],
    }


@router.get("/models/health", response_class=HTMLResponse)
async def health(request: Request):
    return _frag(request, "models_health.html", await health_ctx())


@router.post("/models/health/run", response_class=HTMLResponse)
async def health_run(request: Request):
    try:
        await asyncio.to_thread(ss.forget_ollama_probe)
        ss.verify()
        msg, ok = "", True
    except ss.ActionBusy:
        msg, ok = "A check is already running.", False
    return _frag(request, "models_health.html", await health_ctx(msg, ok))


@router.post("/models/health/fix", response_class=HTMLResponse)
async def health_fix(request: Request):
    from kestrel.web.pages.setup import start_action

    state, message, status = await start_action("build")
    if status is None:
        _after["retest"] = True
        message = "Rebuilding Kestrel's models. Kestrel tests again when it finishes."
    return _frag(request, "models_health.html", await health_ctx(message or "", status is None))


# ===================================================================================== downloads
SIZES_NOTE = "resumes if interrupted"


async def downloads_ctx(message: str = "", ok: bool = True) -> dict[str, Any]:
    from kestrel.web.pages.setup import start_action

    state, snap = await _state()
    cat = profiles.load_catalog()
    prof = cat.profile(state.profile)
    live = bool(snap.get("live"))
    # Pulls finished and Kestrel's tuned models are still missing: build them without a click.
    if _after["tune"] and not live and not state.missing_tags and state.missing_derived:
        _after["tune"] = False
        _st, msg, status = await start_action("build")
        if status is None:
            snap = (await _state())[1]
            live = bool(snap.get("live"))
    items = {i["target"]: i for i in snap.get("items", [])}
    roles: dict[str, list[str]] = {}
    for key in ("tier1", "tier2", "docsel", "tier3"):
        tag = prof.models.for_tier(key)
        if tag:
            roles.setdefault(tag, []).append(mv.STEP_BY_KEY[key]["name"])
    if prof.models.embed:
        roles.setdefault(prof.models.embed, []).append("Matching similar roles")
    rows = []
    for tag in state.base_tags:
        item = items.get(tag)
        size = cat.model_sizes_gb.get(tag)
        rows.append({
            "tag": tag, "size": f"{size:g} GB" if size else "",
            "what": ", ".join(roles.get(tag, [])).lower().capitalize()
            if len(roles.get(tag, [])) > 1 else (roles.get(tag) or [""])[0],
            "ready": tag not in state.missing_tags,
            "item": item if item and item["status"] in ("pending", "leased") else None,
            "failed": bool(item and item["status"] in ("dead", "failed")),
            "error": (item or {}).get("error", ""),
        })
    missing = [r for r in rows if not r["ready"]]
    total_gb = sum(cat.model_sizes_gb.get(t, 0) for t in state.missing_tags)
    tuning = [i for i in snap.get("items", []) if i["kind"] == "build"
              and i["status"] in ("pending", "leased")]
    async with sessionmaker()() as session:
        settings = await load_settings_with_db(session)
    return {
        **speed_view(settings),
        "dl_rows": rows, "dl_missing": len(missing), "dl_gb": total_gb,
        "dl_live": live, "dl_stopping": bool(snap.get("stopping")),
        "dl_hosted": prof.hosted, "dl_engine_down": state.ollama_version is None
        and not prof.hosted,
        "dl_tuning": tuning, "dl_tune_missing": bool(state.missing_derived) and not missing,
        "dl_commands": [f"ollama pull {t}" for t in state.missing_tags],
        "dl_note": SIZES_NOTE,
        "dl_message": message, "dl_ok": ok,
    }


def _downloads(request: Request, ctx: dict) -> HTMLResponse:
    return _frag(request, "models_downloads.html", ctx)


@router.get("/models/downloads", response_class=HTMLResponse)
async def downloads(request: Request):
    return _downloads(request, await downloads_ctx())


@router.post("/models/download", response_class=HTMLResponse)
async def download(request: Request):
    from kestrel.web.pages.setup import start_action

    state, message, status = await start_action("pull")
    if status is None:
        _after["tune"] = True
    elif message is None:
        message = ""
    return _downloads(request, await downloads_ctx(message or "", status is None))


@router.post("/models/download/tune", response_class=HTMLResponse)
async def tune(request: Request):
    from kestrel.web.pages.setup import start_action

    _s, message, status = await start_action("build")
    return _downloads(request, await downloads_ctx(message or "", status is None))


@router.post("/models/download/pause", response_class=HTMLResponse)
async def download_pause(request: Request):
    from kestrel.web.pages.setup import start_action

    _after["tune"] = False
    _s, message, _status = await start_action("cancel")
    return _downloads(request, await downloads_ctx(message or "Paused. Press Resume to continue."))


# ================================================================================ online key
def _preset_id(provider: str, settings: Settings) -> str | None:
    return provider if provider in prov.presets(settings) else None


async def online_ctx(message: str = "", ok: bool = True, *, open_: bool = False) -> dict[str, Any]:
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
    return online_view(settings, message, ok, open_)


def online_view(settings: Settings, message: str = "", ok: bool = True, open_: bool = False):
    externals = prov.external_backends(settings)
    providers = []
    for pid, preset in prov.presets(settings).items():
        backend = externals.get(preset["backend"])
        saved = bool(backend) and (
            bool(backend.api_key) or prov.secret_present(backend.api_key_env))
        providers.append({
            "id": pid, "label": preset["label"], "saved": saved, "console": preset["console"],
            "mono": preset["label"][:1].upper(), "prefix": preset["key_prefix"],
        })
    connected = [p for p in providers if p["saved"]]
    return {
        "on_providers": providers, "on_connected": connected,
        "on_message": message, "on_ok": ok, "on_open": open_,
        "on_other": externals.get("other"),
        "tier_sends": settings.llm.hosted_mode.disclosure_terms()["sends"],
    }


@router.post("/models/online/key", response_class=HTMLResponse)
async def online_key(request: Request):
    """Test or Save (or Remove) one provider's key. The value is never echoed or logged; only
    its last four characters can appear (settings._keybox_op)."""
    from kestrel.web.pages.settings import _keybox_op

    form = await request.form()
    provider = str(form.get("provider") or "")
    op = str(form.get("op") or "test")
    op = op if op in ("test", "save", "remove") else "test"
    value = str(form.get("api_key") or "").strip()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        pid = _preset_id(provider, settings)
        preset = prov.presets(settings).get(pid or "")
        backend = prov.external_backends(settings).get(preset["backend"]) if preset else None
        if preset is None or backend is None or not backend.api_key_env:
            ctx = online_view(settings, "Choose a provider first.", False, True)
        else:
            if pid == "other" and op != "remove":
                from kestrel.services.other_service import key_action

                if not backend.base_url:
                    result = {"ok": False, "text": (
                        "Set the Other service address in Settings > Computer first, "
                        "then return here to test the connection."
                    )}
                else:
                    result = await key_action(op, backend.base_url, value, s, settings)
            else:
                result = await _keybox_op(op, backend, preset["label"], value)
            log.info("models_online_key", provider=pid, op=op, ok=result["ok"],
                     pasted=bool(value))
            settings = await load_settings_with_db(s)
            ctx = online_view(settings, result["text"], result["ok"], True)
    ctx["on_selected"] = pid or ""
    ctx["oob"] = True  # the Online card on the page updates beside the drawer
    ctx["choice"] = (await _state())[0].choice
    return _frag(request, "models_online.html", ctx)


@router.post("/models/online/connect", response_class=HTMLResponse)
async def online_connect(request: Request) -> HTMLResponse:
    """Connect the saved custom service through the existing hosted acknowledgement gate."""
    from kestrel.services.hosted_connect import connect_provider
    from kestrel.web.pages.settings import _active_profile_name

    form = await request.form()
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        out = await connect_provider(
            s, settings, provider="other", api_key="", acknowledged=form.get("ack") == "on",
            free_only=False, switch=form.get("switch") == "on", profile=_active_profile_name(),
        )
        settings = await load_settings_with_db(s)
    ctx = online_view(settings, out.message, out.ok, True)
    ctx.update(on_selected="other", oob=True, choice=(await _state())[0].choice)
    return _frag(request, "models_online.html", ctx)


@router.get("/models/online", response_class=HTMLResponse)
async def online(request: Request):
    ctx = await online_ctx(open_=request.query_params.get("open") == "1")
    ctx["on_selected"] = ""
    return _frag(request, "models_online.html", ctx)


# ============================================================================= change a model
def connected_backend(settings: Settings) -> tuple[str, Any] | None:
    """The provider an online pick goes to: the one the hosted tiers already use, else the first
    with a saved key. Only a provider whose key is saved counts as connected: the shipped
    deeper-read tier names a provider before any key exists. None means no provider is
    connected (the pick is refused in plain words)."""

    def has_key(backend: Any) -> bool:
        return bool(backend.api_key) or prov.secret_present(backend.api_key_env)

    found = hm.catalog_backend(settings)
    if found is not None and has_key(found[1]):
        return found
    for name, backend in prov.external_backends(settings).items():
        if has_key(backend):
            return name, backend
    return None


async def _local_state(settings: Settings) -> tuple[list[str], profiles.Hardware]:
    version, tags = await asyncio.to_thread(ss.probe_ollama, settings)
    return (tags if version else []), await asyncio.to_thread(ss.detected_hardware)


async def change_ctx(
    tier: str,
    *,
    where: str = "local",
    free: bool = False,
    message: str = "",
    ok: bool = True,
    undo: bool = False,
    refresh: bool = False,
    undo_token: str | None = None,
) -> dict[str, Any]:
    step = mv.STEP_BY_KEY[tier]
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        rows = {r.tier: r for r in await hm.tier_rows(s, settings)}
        undo_row = await s.get(Setting, UNDO_PREFIX + tier)
        local_override = await s.get(Setting, f"llm.tiers.{tier}.model")
        customs = await custom_bases(s)
    cat = profiles.load_catalog()
    active = profiles.load_active_profile()
    prof_name = active.profile if active and active.profile in cat.profiles else None
    base = cat.profile(prof_name).models.for_tier(tier) if prof_name else None
    tags, hw = await _local_state(settings)
    default_tag = base
    candidates = []
    for pname in cat.profiles:
        for t in {cat.profile(pname).models.for_tier(tier)} - {None}:
            if t not in candidates:
                candidates.append(t)
    local_models = []
    for tag in candidates:
        label, tone = mv.fit_tag(cat.model_sizes_gb.get(tag), hw)
        local_models.append(
            {
                "id": tag,
                "name": tag,
                "size": f"{cat.model_sizes_gb[tag]:g} GB" if tag in cat.model_sizes_gb else "",
                "fit": label,
                "tone": tone,
                "downloaded": any(profiles_tag_match(tag, t) for t in tags),
                "current": tag
                == (
                    customs.get(str(local_override.value), local_override.value)
                    if local_override
                    else default_tag
                ),
                "default": tag == default_tag,
            }
        )
    ctx: dict[str, Any] = {
        "ch_tier": tier,
        "ch_step": step,
        "ch_where": where,
        "ch_free": free,
        "ch_local": local_models,
        "ch_message": message,
        "ch_ok": ok,
        "ch_override": local_override is not None or bool(rows.get(tier) and rows[tier].override),
        "ch_undo": undo or undo_row is not None,
        "ch_undo_token": undo_token or (undo_row.value.get("token", "") if undo_row else ""),
        "ch_budget": mv.pages(hm.context_budget(tier)),
        "ch_models": [],
        "ch_reasons": {},
        "ch_error": "",
        "ch_provider": "",
        "ch_connected": False,
        "ch_sends": dict(settings.llm.hosted_mode.disclosure_terms()["sends"]).get(tier, ""),
        "ch_online_ok": tier in hm.ASSIGNABLE_TIERS,
    }
    if where != "online":
        return ctx
    found = connected_backend(settings)
    if found is None:
        ctx["ch_error"] = "No online provider is connected. Set one up under Online model first."
        return ctx
    name, backend = found
    ctx.update(
        ch_connected=True,
        ch_provider=next(
            (p["label"] for p in prov.presets(settings).values() if p["backend"] == name), name
        ),
    )
    try:
        models = hm.picker_models(await hm.fetch_models(name, backend, refresh=refresh))
    except hm.ModelListError as exc:
        ctx["ch_error"] = plain_error(exc)
        return ctx
    ctx["ch_models"] = [m for m in models if m.free] if free else models
    ctx["ch_n_free"] = sum(1 for m in models if m.free)
    ctx["ch_reasons"] = {m.id: why for m in models if (why := hm.block_reason(m, tier, backend))}
    return ctx


def profiles_tag_match(want: str, have: str) -> bool:
    from kestrel import setup as setup_cli

    return bool(setup_cli._tag_present(want, [have]))


@router.get("/models/change/{tier}", response_class=HTMLResponse)
async def change_form(request: Request, tier: str):
    if _tier_key(tier) is None:
        return HTMLResponse("unknown step", status_code=404)
    q = request.query_params
    ctx = await change_ctx(
        tier, where="online" if q.get("where") == "online" else "local",
        free=q.get("free") == "1", refresh=q.get("refresh") == "1")
    return _frag(request, "models_change.html", ctx)


def _same(tier: str, where: str, message: str, ok: bool, **kw) -> Any:
    return change_ctx(tier, where=where, message=message, ok=ok, **kw)


@router.post("/models/change/{tier}", response_class=HTMLResponse)
async def change_save(request: Request, tier: str):
    """Save the pick. A local pick is a setting-table override of that step's model (stored apart
    from defaults, D-UI-42); a model not downloaded yet starts its download. An online pick is
    refused with no connected provider, and needs the operator's confirmation, then is saved
    through the same validated override as Models always used."""
    if _tier_key(tier) is None:
        return HTMLResponse("unknown step", status_code=404)
    form = await request.form()
    where = "online" if form.get("where") == "online" else "local"
    model = str(form.get("model") or "").strip()[:200]
    if not model:
        return _frag(request, "models_change.html", await _same(
            tier, where, "Pick a model first.", False))
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        if where == "online":
            return await _save_online(request, s, settings, tier, model, form)
        return await _save_local(request, s, settings, tier, model)


BASE_PREFIX = "models.base."


async def custom_bases(s: AsyncSession) -> dict[str, str]:
    """derived model name -> the model it was built from, for the models Kestrel tuned on request
    (a pick that is not the profile's own model), so a name never shows as a raw id."""
    rows = (await s.execute(
        select(Setting.key, Setting.value).where(Setting.key.like(BASE_PREFIX + "%"))
    )).all()
    return {k.removeprefix(BASE_PREFIX): str(v) for k, v in rows}


def derived_name(tier: str, base: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return f"kestrel-{tier}-{slug}"


async def _tier_state(s: AsyncSession, tier: str) -> dict[str, Any]:
    state = {}
    for key in (
        f"llm.tiers.{tier}.model",
        f"llm.tiers.{tier}.backend",
        f"llm.hosted_tiers.{tier}.model",
        f"llm.hosted_tiers.{tier}.backend",
        hm.OVERRIDES_KEY,
        hm.DEFAULTS_KEY,
    ):
        row = await s.get(Setting, key)
        value = row.value if row else None
        state[key] = (
            value.get(tier)
            if key in (hm.OVERRIDES_KEY, hm.DEFAULTS_KEY) and isinstance(value, dict)
            else value
        )
    return state


async def snapshot(
    s: AsyncSession, tier: str, settings: Settings, *, online: bool = False
) -> dict[str, Any]:
    return {"before": await _tier_state(s, tier)}


async def store_snapshot(s: AsyncSession, tier: str, value: dict[str, Any]) -> str:
    await s.flush()
    after = await _tier_state(s, tier)
    before = value["before"]
    changed = {k: v for k, v in after.items() if v != before[k]}
    token = secrets.token_urlsafe(12)
    data = {"token": token, "before": {k: before[k] for k in changed}, "after": changed}
    row = await s.get(Setting, UNDO_PREFIX + tier)
    if row is None:
        s.add(Setting(key=UNDO_PREFIX + tier, value=data))
    else:
        row.value = data
    s.info.setdefault("model_undo_tokens", {})[tier] = token
    return token


def online_problem(settings: Settings) -> str | None:
    """An online model is only used once the operator accepted the current disclosure (Settings);
    the client refuses a send without it, so Models and Model check say so up front."""
    if not settings.llm.hosted_mode.acknowledgement_current:
        return ("Online models need your approval first. Tick I understand what gets sent "
                "on the Models page, then try again.")
    return None


async def _profile_name(cat: profiles.ModelCatalog) -> str:
    active = profiles.load_active_profile()
    if active is not None and active.profile in cat.profiles:
        return active.profile
    hw = await asyncio.to_thread(ss.detected_hardware)
    return profiles.recommend_profile(hw)[0]


async def _create_derived(
    settings: Settings, tier: str, base: str, name: str, profile: str, default_base: str
) -> str | None:
    """Tune `base` for this step: Ollama's /v1 endpoint cannot set num_ctx per request, so a raw
    tag would silently read a fraction of a long posting. The derived model bakes in the step's
    own room to read. Built from a model already on this computer, so it takes seconds. None on
    success, else a plain sentence."""
    import httpx

    try:
        text = profiles.render_modelfile(tier, profile).replace(
            f"FROM {default_base}", f"FROM {base}", 1)
        body = mt.create_body(name, text)
    except (KeyError, ValueError) as exc:
        log.warning("model_tune_modelfile_failed", tier=tier, error_type=type(exc).__name__,
                    error=str(exc))
        return f"Kestrel cannot tune a model for this step on this computer: {fixed_reason(exc)}"
    body["stream"] = False
    url = settings.llm.ollama_native_url.rstrip("/") + "/api/create"
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            (await client.post(url, json=body)).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("model_tune_failed", tier=tier, base=base, error_type=type(exc).__name__)
        return f"Kestrel could not tune {base} for this step: {fixed_reason(exc)}"
    await asyncio.to_thread(ss.forget_ollama_probe)
    return None


async def switch_local(
    s: AsyncSession, settings: Settings, tier: str, model: str
) -> tuple[bool, str]:
    """Switch a step to a model on this computer, safely. The profile's own model is the default
    (the override is cleared); another model is tuned for the step first (never a raw tag, which
    would read too little of a long posting) and the step switches only once that exists; a model
    that is not downloaded starts downloading and the step is left alone. Commits on success."""
    name = mv.STEP_BY_KEY[tier]["name"]
    cfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(cfg.backend) if cfg else None
    if backend is not None and backend.external:
        return False, (
            "This step runs online right now. Reset it to the default first, then "
            "pick a model on this computer."
        )
    cat = profiles.load_catalog()
    profile = await _profile_name(cat)
    default_base = cat.profile(profile).models.for_tier(tier)
    version, tags = await asyncio.to_thread(ss.probe_ollama, settings)
    tags = tags if version else []

    def present(tag: str) -> bool:
        return any(profiles_tag_match(tag, t) for t in tags)

    known = {t for p in cat.profiles for t in [cat.profile(p).models.for_tier(tier)] if t}
    if model not in known and not present(model):
        return False, "Kestrel does not know that model. Pick one from the list."
    target: str | None
    custom: str | None = None
    if model.startswith("kestrel-"):
        target = model
    elif model == default_base:
        target = None
    elif default_base is None:
        return False, "This computer is set up to use an online model, so there is nothing to tune."
    else:
        if not present(model):
            await mt.queue_pulls(s, [model])
            await asyncio.to_thread(ss.forget_ollama_probe)
            return False, (
                f"{model} is not downloaded yet. The download has started. Pick it "
                "again when it is ready; Kestrel will not switch to a model that "
                "cannot run yet."
            )
        target = derived_name(tier, model)
        if not present(target):
            err = await _create_derived(settings, tier, model, target, profile, default_base)
            if err:
                return False, err
        custom = model
    before = await snapshot(s, tier, settings)
    key = f"llm.tiers.{tier}.model"
    row = await s.get(Setting, key)
    if target is None:
        if row is not None:
            await s.delete(row)
    elif row is None:
        s.add(Setting(key=key, value=target))
    else:
        row.value = target
    if custom is not None and target is not None:
        base_row = await s.get(Setting, BASE_PREFIX + target)
        if base_row is None:
            s.add(Setting(key=BASE_PREFIX + target, value=custom))
        else:
            base_row.value = custom
    await store_snapshot(s, tier, before)
    await s.commit()
    shown = mv.friendly_model(target, base=custom or default_base) if target else default_base
    return True, f"{name} now uses {shown}."


async def _save_local(request, s: AsyncSession, settings: Settings, tier: str, model: str):
    ok, msg = await switch_local(s, settings, tier, model)
    return _frag(
        request,
        "models_change.html",
        await _same(
            tier,
            "local",
            msg,
            ok,
            undo=ok,
            undo_token=s.info.get("model_undo_tokens", {}).get(tier),
        ),
    )


async def _save_online(request, s, settings, tier: str, model: str, form):
    found = connected_backend(settings)
    if found is None:
        return _frag(
            request,
            "models_change.html",
            await _same(
                tier,
                "online",
                "No online provider is connected. Set one up under Online model first.",
                False,
            ),
        )
    problem = online_problem(settings)
    if problem:
        return _frag(request, "models_change.html", await _same(tier, "online", problem, False))
    if form.get("confirm") != "on":
        return _frag(
            request,
            "models_change.html",
            await _same(
                tier, "online", "Tick the box to confirm what will be sent, then Save.", False
            ),
        )
    name, backend = found
    try:
        models = await hm.fetch_models(name, backend)
    except hm.ModelListError as exc:
        return _frag(
            request,
            "models_change.html",
            await _same(tier, "online", f"Nothing saved. {plain_error(exc)}", False),
        )
    before = await snapshot(s, tier, settings, online=True)
    result = await hm.save_overrides(s, settings, name, backend, {tier: model}, models)
    if not result.refused:  # a refused pick changes nothing, so it leaves no Undo behind
        await store_snapshot(s, tier, before)
        await s.commit()
    return _frag(
        request,
        "models_change.html",
        await _same(
            tier,
            "online",
            result.message,
            not result.refused,
            undo=not result.refused,
            undo_token=s.info.get("model_undo_tokens", {}).get(tier),
        ),
    )


@router.post("/models/change/{tier}/reset", response_class=HTMLResponse)
async def change_reset(request: Request, tier: str):
    if _tier_key(tier) is None:
        return HTMLResponse("unknown step", status_code=404)
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        before = await snapshot(s, tier, settings)
        msg, ok = "Back to the default.", True
        await s.execute(delete(Setting).where(Setting.key == f"llm.tiers.{tier}.model"))
        await s.commit()
        if tier in hm.ASSIGNABLE_TIERS:
            ok_h, msg_h = await hm.reset_override(s, tier)
            if not ok_h and "no override" not in msg_h.lower():
                msg = msg_h
        token = await store_snapshot(s, tier, before)
        await s.commit()
    return _frag(
        request,
        "models_change.html",
        await _same(tier, "local", msg, ok, undo=True, undo_token=token),
    )


async def undo_tier(
    s: AsyncSession,
    tier: str,
    token: str = "",
    *,
    commit: bool = True,
) -> bool:
    """Restore only this action's settings after checking every value it wrote."""
    row = await s.get(Setting, UNDO_PREFIX + tier)
    if row is None or not isinstance(row.value, dict) or not token:
        return False
    prev = row.value
    if token != prev.get("token"):
        return False
    current = await _tier_state(s, tier)
    if any(current[k] != v for k, v in prev["after"].items()):
        return False
    for key, value in prev["before"].items():
        setting = await s.get(Setting, key)
        if key in (hm.OVERRIDES_KEY, hm.DEFAULTS_KEY):
            values = dict(setting.value or {}) if setting else {}
            if value is None:
                values.pop(tier, None)
            else:
                values[tier] = value
            value = values
        if value is None:
            if setting is not None:
                await s.delete(setting)
        elif setting is None:
            s.add(Setting(key=key, value=value))
        else:
            setting.value = value
    await s.delete(row)
    if commit:
        await s.commit()
    else:
        await s.flush()
    return True


@router.post("/models/change/{tier}/undo", response_class=HTMLResponse)
async def change_undo(request: Request, tier: str):
    if _tier_key(tier) is None:
        return HTMLResponse("unknown step", status_code=404)
    async with sessionmaker()() as s:
        done = await undo_tier(s, tier, request.query_params.get("token", ""))
    msg, ok = ("Put back as it was.", True) if done else ("This can no longer be undone.", False)
    return _frag(request, "models_change.html", await _same(tier, "local", msg, ok))


# ====================================================================================== speed
def speed_view(settings: Settings) -> dict[str, Any]:
    return {
        "sp_t1": settings.concurrency.tier1,
        "sp_t2": settings.concurrency.tier2,
        "sp_parallel": max(settings.concurrency.model_dump().values()),
    }


@router.post("/models/speed", response_class=HTMLResponse)
async def speed(request: Request):
    from kestrel.web.pages.settings import _set

    form = await request.form()
    try:
        values = Concurrency.model_validate_strings({
            key: str(form.get(key) or "").strip() for key in ("tier1", "tier2")
        }, strict=True)
    except ValidationError:
        values = None
    async with sessionmaker()() as s:
        if values is not None:
            for key in ("tier1", "tier2"):
                await _set(s, f"concurrency.{key}", getattr(values, key))
            await s.commit()
        settings = await load_settings_with_db(s)
    msg = "Saved. It applies on the next scoring pass." if values is not None else \
        "Enter a whole number from 1 to 16. Neither setting was changed."
    return templates().TemplateResponse(request, "partials/models_speed.html", {
        **speed_view(settings), "sp_message": msg, "sp_ok": values is not None})


# ============================================================================== page context
async def last_run_minutes(s: AsyncSession) -> int | None:
    from datetime import datetime

    run = (await s.execute(
        select(Run).where(Run.finished_at.is_not(None)).order_by(Run.id.desc()).limit(1)
    )).scalar_one_or_none()
    if run is None:
        return None
    try:
        a = datetime.fromisoformat(run.started_at)
        b = datetime.fromisoformat(run.finished_at or "")
    except ValueError:
        return None
    return max(1, round((b - a).total_seconds() / 60))


def parse_capacity(report: dict | None) -> dict[str, int]:
    """tier -> the context the model really took (from the preflight's num_ctx rows)."""
    out: dict[str, int] = {}
    for r in (report or {}).get("results", []):
        name = str(r.get("name", "")) if isinstance(r, dict) else ""
        m = re.search(r"(tier\d|docsel)", name)
        d = re.search(r"num_ctx (\d+)", str(r.get("detail", ""))) if m else None
        if name.startswith("num_ctx ") and m and d:
            out[m.group(1)] = int(d.group(1))
    return out


async def page_ctx(rows: list, settings: Settings, track: str) -> dict[str, Any]:
    """Everything the Models page needs beyond the stored-evaluation history: the data note, This
    computer, Online, the Scoring steps table, and the five section contexts."""
    from kestrel.analytics.metrics import quality_metrics
    from kestrel.bench import check
    from kestrel.learning import reranker as reranker_mod
    from kestrel.llm.embeddings import current_model
    from kestrel.services import hosted_balance

    state, _snap = await _state()
    report = ss.load_report()
    cat = profiles.load_catalog()
    prof = cat.profile(state.profile)
    too_small = mv.too_small_tiers(report)
    capacity = parse_capacity(report)
    async with sessionmaker()() as s:
        tier_rows = {r.tier: r for r in await hm.tier_rows(s, settings)}
        customs = await custom_bases(s)
        locals_ = set((await s.execute(
            select(Setting.key).where(Setting.key.like("llm.tiers.%.model")))).scalars())
        history = await check.list_checks(s)
        quality = await quality_metrics(s, track or "employment")
        spend = await hosted_balance.local_spend(s)
        run_min = await last_run_minutes(s)
        undo_tiers = set((await s.execute(
            select(Setting.key).where(Setting.key.like(UNDO_PREFIX + "%")))).scalars())
        reranker = await reranker_mod.load_active_reranker(
            s, track or "employment", embed_model=current_model(settings))
    better = {
        v["step"] for v in ((history[0].get("steps") if history else None) or []) if v.get("change")
    }
    step_name = {"tier1": "quick_screen", "tier2": "rating", "tier3": "deeper_read"}
    steps = []
    for st in mv.STEPS:
        key = st["key"]
        row: dict[str, Any] = {**st}
        if key == "rules":
            row.update(model="No AI model", online=False, override=False, can_read=None,
                       fit=mv.Fit("Not needed", "muted"), changeable=False, base=None)
            steps.append(row)
            continue
        cfg = settings.llm.tiers.get(key)
        backend = settings.llm.backends.get(cfg.backend) if cfg else None
        online = mv.run_where(state.choice, key, bool(backend and backend.external))
        shown = cfg
        if key == "tier3" and online and "frontier" in settings.llm.tiers:
            shown = settings.llm.tiers["frontier"]
        base = prof.models.for_tier(key)
        tr = tier_rows.get(key)
        need = hm.context_budget(key)
        small = key in too_small
        row.update(
            model=mv.friendly_model(
                shown.model if shown else "",
                base=None if online else customs.get(shown.model if shown else "", base)),
            online=online,
            override=bool((tr and tr.override) or f"llm.tiers.{key}.model" in locals_),
            changeable=True, base=base,
            can_read={
                "label": "Too small: cuts long postings short" if small else mv.READS[key],
                "tone": "bad" if small else "good",
                "tip": f"Needs about {mv.pages(need)} pages ({need:,} words-worth of room). "
                + (f"This model took in about {mv.pages(capacity[key])} pages."
                   if key in capacity else "Run the health check to test it."),
            },
            fit=mv.fit_with_you(rows, st["tier"], shown.model if shown else "",
                                step_name.get(key) in better),
            can_undo=(UNDO_PREFIX + key) in undo_tiers,
        )
        if key == "docsel":
            row["fit"] = mv.Fit("Not measured", "muted")
        steps.append(row)
    t2 = next(s for s in steps if s["key"] == "tier2")
    base2 = prof.models.for_tier("tier2")
    size2 = cat.model_sizes_gb.get("" if base2 is None else base2)
    engine = "down" if (state.ollama_version is None and not prof.hosted) else "up"
    if state.choice == "hosted":
        local_pill = ("Not used right now", "")
    elif engine == "down":
        local_pill = ("AI engine not running", "warn")
    elif state.missing_tags or state.missing_derived:
        local_pill = ("Needs downloads", "warn")
    else:
        local_pill = ("Ready", "good")
    cpu_only = bool(cat.profile(state.recommended).hosted) or (
        state.hardware.gpu_vendor is None and not state.hardware.apple_silicon)
    ctx = {
        "choice": state.choice,
        "choice_label": {"local": "On this computer", "hosted": "Online model",
                         "both": "Both"}.get(state.choice, "On this computer"),
        "steps": steps, "hw_chips": mv.hardware_chips(state.hardware),
        "local_pill": local_pill, "cpu_only": cpu_only,
        "local_model": mv.friendly_model(t2["model"] if t2["online"] else base2 or ""),
        "local_billions": mv.model_billions(base2),
        "local_pages": mv.pages(hm.context_budget("tier2")),
        "local_size": f"About {size2:g} GB" if size2 else "",
        "recommendation": state.recommended_reason,
        "uses_line": ", ".join(f"{s['name']} on {s['model']}" for s in steps
                               if s["key"] in ("tier1", "tier2") and not s["online"]),
        "better_steps": better, "last_check": history[0] if history else None,
        "last_check_when": bench_view.date_words(history[0].get("at")) if history else "",
        **speed_view(settings),
        "sp_message": "", "sp_ok": True,
        "quality": quality, "spend": spend, "run_minutes": run_min,
        "reranker_active": reranker is not None,
        "mean_latency": _mean_latency(rows),
        "profile_name": state.profile,
        **await health_ctx(), **await downloads_ctx(),
        **online_view(settings), "on_selected": "",
    }
    return ctx


def _mean_latency(rows: list) -> dict[int, int]:
    out: dict[int, list[tuple[float, int]]] = {}
    for r in rows:
        if r.mean_latency_ms is not None:
            out.setdefault(r.tier, []).append((r.mean_latency_ms, r.n))
    return {t: round(sum(a * n for a, n in v) / max(1, sum(n for _, n in v)) / 1000.0, 1)
            for t, v in out.items()}
