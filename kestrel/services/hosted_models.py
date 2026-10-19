"""Hosted model overrides (D-UI-42..): the operator's own per-tier pick on the Models page, kept
apart from the automatic default that Connect chooses, so re-running Connect never clobbers an
override and "Reset to default" restores the automatic choice.

Storage. The effective model stays where the config layer already reads it
(`llm.hosted_tiers.<tier>.backend/model`, and `llm.tiers.frontier.*` for the frontier tier), so
the pipeline and doctor need no change. Two state rows beside it, both outside the config layer
(setting_overrides.TRANSIENT_KEYS):

- `hosted_model_defaults`: {tier: {"backend", "model"}} or {tier: None}: what Connect chose last
  (or, for a tier overridden before any Connect recorded a default, what was assigned then).
- `hosted_model_overrides`: {tier: {"backend", "model", "at"}}: the operator's own picks.

The model list comes from the provider's GET /models (providers.list_models, the same call
Connect makes), filtered to models Kestrel can ask for JSON (D-UI-48: strict schema, JSON mode,
or prompt-only, each labeled), and is cached in-process for
CACHE_TTL_S so opening the picker twice costs one request. The request carries only the key; no
operator or opportunity data (hard rule 1)."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import BackendConfig, HostedModelCaps, Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.llm import providers as prov
from kestrel.llm.service_name import service_name
from kestrel.services.setting_overrides import (
    HOSTED_MODEL_DEFAULTS_KEY,
    HOSTED_MODEL_OVERRIDES_KEY,
)

log = structlog.get_logger()

# The per-model capability map the client reads (config.LLMSettings.hosted_model_caps): one
# setting row, because model ids contain dots a dotted key cannot carry.
CAPS_KEY = "llm.hosted_model_caps"
DEFAULTS_KEY = HOSTED_MODEL_DEFAULTS_KEY
OVERRIDES_KEY = HOSTED_MODEL_OVERRIDES_KEY
CACHE_TTL_S = 600.0
# Every tier Hosted mode can route (providers.HOSTED_TIERS), in the Settings table's order.
ASSIGNABLE_TIERS: tuple[str, ...] = prov.HOSTED_TIERS
# Tiers with no num_ctx row of their own borrow the budget of the prompt they send: the trigger
# classifier is a Tier 1 sized prompt, the frontier escalation sends the Tier 2 prompt.
_CTX_TIER = {"trigger": "tier1", prov.FRONTIER_TIER: "tier2"}
# SPEC.md 8.2 floors, used only when the packaged models.yml cannot be read.
_CTX_FALLBACK = {"tier1": 4096, "tier2": 16384, "tier3": 20480, "docsel": 8192}

_cache: dict[tuple[str, str, str], tuple[float, list[prov.ModelInfo]]] = {}

# One guard for every read-modify-write of the defaults/overrides rows: Save all, Reset to
# default, Settings > Hosted > Advanced and the Connect apply step. All of them run on uvicorn's
# single loop, so an asyncio.Lock serializes them; each reads the state inside the lock.
STATE_LOCK = asyncio.Lock()


class ModelListError(RuntimeError):
    """The provider's model list could not be fetched; the message is shown to the operator."""


@dataclass
class PickerModel:
    """One model as the picker shows it."""

    id: str
    name: str
    context_length: int | None
    free: bool
    price: str  # "free", "$0.10/$0.40 per M in/out", or "price not listed"
    # How the model returns JSON (D-UI-48): "schema", "json_object" or "prompt", and its words.
    json_mode: str = "schema"
    mode_label: str = prov.JSON_MODE_LABELS["schema"]


@dataclass
class TierRow:
    """One Models page row: what the tier runs on a hosted run, and whether that is an override."""

    tier: str
    backend: str
    model: str
    assigned: bool
    override: bool
    default_model: str | None
    budget: int
    paid: bool = False  # an override on a model that is not free: calls to this tier are billed

    @property
    def label(self) -> str:
        """"(Override)" for the operator's own pick ("(Override · paid)" when that pick is
        billed); "Default" for Connect's choice (or a tier with no recorded default yet); "Set
        elsewhere" when the assignment was changed outside the Models page (`kestrel setup
        --hosted`) after the default was recorded."""
        if self.override:
            return "(Override · paid)" if self.paid else "(Override)"
        if not self.assigned or self.default_model in (None, self.model):
            return "Default"
        return "Set elsewhere"


# ------------------------------------------------------------------------------- classification
def is_free(m: prov.ModelInfo) -> bool:
    """Free when the id is OpenRouter's `:free` variant or both prices are listed as zero.
    Unknown pricing is never free (providers.free_models)."""
    if prov.is_free_variant(m.id):
        return True
    return m.prompt_usd_per_m == 0 and m.completion_usd_per_m == 0


def price_label(m: prov.ModelInfo) -> str:
    if is_free(m):
        return "free"
    if m.prompt_usd_per_m is None or m.completion_usd_per_m is None:
        return "price not listed"
    return f"${m.prompt_usd_per_m:.2f}/${m.completion_usd_per_m:.2f} per M in/out"


def picker_models(models: list[prov.ModelInfo]) -> list[PickerModel]:
    """Free first, then by id, so the free-only view and the full view read the same way."""
    out = [
        PickerModel(
            m.id, m.name or m.id, m.context_length, is_free(m), price_label(m),
            json_mode=m.json_mode or "schema", mode_label=prov.json_mode_label(m),
        )
        for m in models
    ]
    out.sort(key=lambda p: (not p.free, p.id))
    return out


def context_budget(tier: str) -> int:
    """The tier's context budget (SPEC.md 8.2, resources/config/models.yml num_ctx floor)."""
    key = _CTX_TIER.get(tier, tier)
    try:
        from kestrel.llm.profiles import load_catalog

        return load_catalog().num_ctx.for_tier(key)
    except (KeyError, ValueError, OSError, AttributeError) as exc:
        log.warning("context_budget_fallback", tier=tier, error=type(exc).__name__)
        return _CTX_FALLBACK.get(key, _CTX_FALLBACK["tier2"])


def block_reason(
    model: PickerModel | prov.ModelInfo, tier: str | None, backend: BackendConfig | None
) -> str | None:
    """Why `model` cannot be picked for `tier`, or None. A context window below the tier's budget
    is refused because a too-small window truncates silently (the system's worst failure mode).
    A free OpenRouter model under data_collection deny is refused because every call to it fails
    with "No endpoints found matching your data policy" (D-UI-32). Unknown context is allowed:
    OpenAI lists none, and refusing every model would leave nothing to pick."""
    if tier is not None and model.context_length is not None:
        budget = context_budget(tier)
        if model.context_length < budget:
            return f"context {model.context_length:,} is below this tier's {budget:,} tokens"
    free = model.free if isinstance(model, PickerModel) else is_free(model)
    if (
        free
        and backend is not None
        and backend.provider == "openrouter"
        and backend.hosted.data_collection == "deny"
    ):
        return "free models need training allowed under Settings > Hosted"
    return None


# ------------------------------------------------------------------------------------ the list
def catalog_backend(settings: Settings) -> tuple[str, BackendConfig] | None:
    """The external backend the hosted tiers use: the first assigned hosted tier's, else the
    frontier tier's. None when no external backend is assigned (nothing to list)."""
    externals = prov.external_backends(settings)
    for row in prov.assignment_status(settings):
        if row.external and row.backend in externals:
            return row.backend, externals[row.backend]
    return None


def _key_value(backend: BackendConfig) -> str:
    return backend.api_key or prov.secret_value(backend.api_key_env)


def _cache_key(name: str, backend: BackendConfig) -> tuple[str, str, str]:
    """(backend, base URL, a SHA-256 digest of the key): a changed or removed key never answers
    from a list fetched with another one. The raw key is never part of the cache key."""
    digest = hashlib.sha256(_key_value(backend).encode("utf-8")).hexdigest()
    return (name, backend.base_url.rstrip("/"), digest)


def cached(name: str, backend: BackendConfig) -> list[prov.ModelInfo] | None:
    hit = _cache.get(_cache_key(name, backend))
    if hit is None or time.monotonic() - hit[0] > CACHE_TTL_S:
        return None
    return list(hit[1])


def prime(name: str, backend: BackendConfig, models: list[prov.ModelInfo]) -> None:
    """Store a list fetched elsewhere (Connect) so the picker opens without another request."""
    _cache[_cache_key(name, backend)] = (time.monotonic(), list(models))


def clear_cache() -> None:
    _cache.clear()


async def fetch_models(
    name: str, backend: BackendConfig, *, refresh: bool = False
) -> list[prov.ModelInfo]:
    """The models Kestrel can ask for JSON (providers.models_fit_for_tier3: on OpenRouter every
    chat model, each with its JSON mode), from the cache when fresh.
    Raises ModelListError with a message fit to show; never logs the key. The key check comes
    first, so a key removed since the list was cached is reported, not answered from the cache."""
    if not (backend.api_key or prov.secret_present(backend.api_key_env)):
        raise ModelListError(
            f"No key is saved for {name} ({backend.api_key_env}). Connect under Settings > Hosted."
        )
    if not refresh:
        hit = cached(name, backend)
        if hit is not None:
            return hit
    try:
        models = prov.models_fit_for_tier3(await prov.list_models(backend), backend)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError,
            AttributeError) as exc:
        log.info("hosted_model_list_failed", backend=name, error=type(exc).__name__)
        raise ModelListError(
            f"The model list from {name} could not be fetched: {type(exc).__name__}: {exc}"
        ) from exc
    prime(name, backend, models)
    return list(models)


# ---------------------------------------------------------------------------------- the state
def _prefix(tier: str) -> str:
    return "llm.tiers" if tier == prov.FRONTIER_TIER else "llm.hosted_tiers"


async def _get(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.get(Setting, key)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def _unset(session: AsyncSession, key: str) -> None:
    row = await session.get(Setting, key)
    if row is not None:
        await session.delete(row)


async def merged_caps(
    session: AsyncSession, models: list[prov.ModelInfo]
) -> dict[str, Any]:
    """The stored capability map with `models` recorded (or refreshed) in it. Entries for other
    models are kept: a model id that no tier names any more costs one small dict entry and
    nothing else, and the client only ever looks up the model a tier runs."""
    caps = await _get(session, CAPS_KEY)
    for m in models:
        caps[m.id] = prov.model_caps(m).model_dump()
    return caps


async def read_state(session: AsyncSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """(defaults, overrides) as stored."""
    return await _get(session, DEFAULTS_KEY), await _get(session, OVERRIDES_KEY)


async def tier_rows(session: AsyncSession, settings: Settings) -> list[TierRow]:
    defaults, overrides = await read_state(session)
    out: list[TierRow] = []
    for st in prov.assignment_status(settings):
        ov = overrides.get(st.tier)
        # An override only stands while the tier still runs exactly that pick; a model changed
        # under Settings > Hosted > Advanced since then is what it says there, not an override.
        is_override = bool(
            ov and st.external and ov.get("model") == st.model and ov.get("backend") == st.backend
        )
        dflt = defaults.get(st.tier)
        out.append(
            TierRow(
                tier=st.tier,
                backend=st.backend,
                model=st.model if st.external else "",
                assigned=st.external,
                override=is_override,
                default_model=dflt.get("model") if isinstance(dflt, dict) else None,
                budget=context_budget(st.tier),
                paid=is_override and _override_paid(ov),
            )
        )
    return out


def _override_paid(ov: Any) -> bool:
    """Whether a stored override is billed. Save all records `free` from the provider's list;
    an override written without it (Settings > Hosted > Advanced) is paid unless the id is a
    `:free` variant, so an unknown price reads as billed, never as free."""
    if not isinstance(ov, dict):
        return False
    if isinstance(ov.get("free"), bool):
        return not ov["free"]
    return not prov.is_free_variant(str(ov.get("model") or ""))


def _label(tier: str) -> str:
    """A tier's plain name for messages the user reads (never "tier2" or "frontier")."""
    from kestrel.services.hosted_connect import tier_label

    return tier_label(tier)


@dataclass
class SaveResult:
    saved: list[str]
    refused: list[str]
    paid: list[str] = field(default_factory=list)  # tiers saved on a model that is billed
    free_only: bool = False  # Settings > Hosted has Free models only on

    @property
    def message(self) -> str:
        parts = []
        if self.saved:
            parts.append("Saved as override: " + "; ".join(self.saved) + ".")
        if self.paid:
            # A paid override is allowed ("mostly free, with a paid frontier"), never silent.
            scope = " although Free models only is on" if self.free_only else ""
            parts.append(
                f"Billed: a paid model is now used for {', '.join(_label(t) for t in self.paid)}"
                f"{scope}, so the provider charges for every call to it."
            )
        if self.refused:
            parts.append("Not saved: " + "; ".join(self.refused) + ".")
        return " ".join(parts) or "Nothing picked, so nothing changed."


async def save_overrides(
    session: AsyncSession,
    settings: Settings,
    backend_name: str,
    backend: BackendConfig,
    picks: dict[str, str],
    models: list[prov.ModelInfo],
) -> SaveResult:
    """Write each picked tier as an override. A pick must be an id in `models` (the fetched,
    schema-capable list) and pass block_reason for that tier; anything else is refused by name,
    never silently defaulted (hard rule 3). Tiers absent from `picks` are untouched. A paid pick
    is allowed even with Free models only on (a paid frontier over free tiers is intended); the
    result names it as billed. Commits."""
    async with STATE_LOCK:
        return await _save_overrides(session, settings, backend_name, backend, picks, models)


async def _save_overrides(
    session: AsyncSession,
    settings: Settings,
    backend_name: str,
    backend: BackendConfig,
    picks: dict[str, str],
    models: list[prov.ModelInfo],
) -> SaveResult:
    by_id = {m.id: m for m in models}
    defaults, overrides = await read_state(session)
    current = {st.tier: st for st in prov.assignment_status(settings)}
    result = SaveResult([], [], free_only=settings.llm.hosted_mode.free_only)
    saved_infos: list[prov.ModelInfo] = []
    for tier, model_id in picks.items():
        if tier not in ASSIGNABLE_TIERS:
            result.refused.append(f"{_label(tier)}: not a tier that runs online")
            continue
        info = by_id.get(model_id)
        if info is None:
            result.refused.append(
                f"{_label(tier)}: {model_id!r} is not in the model list from "
                f"{service_name(backend.base_url)}"
            )
            continue
        why = block_reason(info, tier, backend)
        if why:
            result.refused.append(f"{_label(tier)}: {model_id} refused, {why}")
            continue
        if tier not in defaults:
            # Record what ran before the first override, so Reset has something to restore.
            st = current.get(tier)
            defaults[tier] = (
                {"backend": st.backend, "model": st.model} if st and st.external else None
            )
        await _put(session, f"{_prefix(tier)}.{tier}.backend", backend_name)
        await _put(session, f"{_prefix(tier)}.{tier}.model", model_id)
        free = is_free(info)
        overrides[tier] = {
            "backend": backend_name, "model": model_id, "at": utcnow_iso(), "free": free
        }
        result.saved.append(f"{_label(tier)}: {model_id}" + ("" if free else " (paid)"))
        saved_infos.append(info)
        if not free:
            result.paid.append(tier)
    if saved_infos:
        # How to ask each picked model for JSON, and its context window (D-UI-48, D-LLM-3).
        await _put(session, CAPS_KEY, await merged_caps(session, saved_infos))
    await _put(session, DEFAULTS_KEY, defaults)
    await _put(session, OVERRIDES_KEY, overrides)
    await session.commit()
    log.info("hosted_overrides_saved", saved=len(result.saved), refused=len(result.refused))
    return result


async def reset_override(session: AsyncSession, tier: str) -> tuple[bool, str]:
    """Drop the tier's override and restore the recorded default (or unassign the tier when the
    default was "stays local"). Commits. Returns (ok, the message to show)."""
    if tier not in ASSIGNABLE_TIERS:
        return False, f"{tier!r} is not a hosted tier."
    async with STATE_LOCK:
        return await _reset_override(session, tier)


async def _reset_override(session: AsyncSession, tier: str) -> tuple[bool, str]:
    defaults, overrides = await read_state(session)
    if tier not in overrides:
        return False, f"{tier} has no override to reset."
    overrides.pop(tier)
    dflt = defaults.get(tier)
    if isinstance(dflt, dict) and dflt.get("model"):
        await _put(session, f"{_prefix(tier)}.{tier}.backend", dflt["backend"])
        await _put(session, f"{_prefix(tier)}.{tier}.model", dflt["model"])
        msg = f"{tier} is back on its default, {dflt['model']}."
    else:
        await _unset(session, f"{_prefix(tier)}.{tier}.backend")
        await _unset(session, f"{_prefix(tier)}.{tier}.model")
        msg = f"{tier} is back on its default: no hosted model, so it stays local."
    await _put(session, OVERRIDES_KEY, overrides)
    await session.commit()
    log.info("hosted_override_reset", tier=tier)
    return True, msg


@dataclass
class ConnectOverrides:
    """What Connect did with the standing overrides: `kept` is (tier, model, paid) per override
    put back, `dropped` is "tier model: why" per override cleared (the tier keeps Connect's
    default)."""

    kept: list[tuple[str, str, bool]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def paid(self) -> list[str]:
        """The kept tiers whose override is billed."""
        return [tier for tier, _model, paid in self.kept if paid]


async def apply_connect(
    session: AsyncSession,
    rows: dict[str, Any],
    backend_name: str,
    fit: list[prov.ModelInfo],
    backend: BackendConfig,
) -> ConnectOverrides:
    """Called by Connect, holding STATE_LOCK, before it writes `rows` (setup.hosted_rows).
    Records the new automatic picks as the defaults, then puts every standing override back into
    `rows` so Connect never clobbers it. `backend` is the backend as it will be after the write
    (its data_collection may change with this Connect), and every kept override is re-checked
    with block_reason against it. An override for another backend, for a model the provider no
    longer lists, or one that is now blocked cannot run: it is dropped, the tier keeps Connect's
    default, and the reason is named. A paid override is kept even when Connect picked from free
    models only, and named as paid. The caller commits."""
    defaults, overrides = await read_state(session)
    for tier in ASSIGNABLE_TIERS:
        key = f"{_prefix(tier)}.{tier}.model"
        if key in rows:
            defaults[tier] = {"backend": rows[f"{_prefix(tier)}.{tier}.backend"],
                              "model": rows[key]}
    by_id = {m.id: m for m in fit}
    out = ConnectOverrides()
    for tier, ov in list(overrides.items()):
        model = ov.get("model") if isinstance(ov, dict) else None
        info = by_id.get(model) if isinstance(model, str) else None
        if not isinstance(ov, dict) or ov.get("backend") != backend_name:
            why = "it is for another provider"
        elif info is None:
            why = "the provider no longer lists it"
        else:
            why = block_reason(info, tier, backend)
        if why or info is None:
            overrides.pop(tier)
            out.dropped.append(f"{tier} {model}: {why}")
            continue
        free = is_free(info)
        overrides[tier] = {**ov, "free": free}  # a new dict, so the JSON row is seen as changed
        rows[f"{_prefix(tier)}.{tier}.backend"] = backend_name
        rows[f"{_prefix(tier)}.{tier}.model"] = model
        out.kept.append((tier, str(model), not free))
    assigned = {rows.get(f"{_prefix(t)}.{t}.model") for t in ASSIGNABLE_TIERS}
    picked = [by_id[i] for i in sorted(x for x in assigned if isinstance(x, str) and x in by_id)]
    if picked:
        # Written with the rest of Connect's rows: how each assigned model takes JSON and its
        # context window, so the first run needs no model list (D-UI-48, D-LLM-3).
        rows[CAPS_KEY] = await merged_caps(session, picked)
    await _put(session, DEFAULTS_KEY, defaults)
    await _put(session, OVERRIDES_KEY, overrides)
    return out


async def caps_for_picks(
    session: AsyncSession,
    settings: Settings,
    changes: dict[str, tuple[str, str] | None],
) -> dict[str, Any]:
    """The stored capability map with every newly assigned model recorded. The in-process list
    cache expires and is empty after a restart, so a model missing from it is looked up in the
    provider's list fetched now with the key (metadata only, nothing about the operator). When
    that fails, or the provider does not list the model, it is recorded as json_mode "unknown",
    which the request builder sends as JSON mode with require_parameters off, never as a strict
    schema that may route nowhere (D-UI-48)."""
    caps = await _get(session, CAPS_KEY)
    for pick in changes.values():
        if pick is None:
            continue
        name, model_id = pick
        backend = settings.llm.backends.get(name)
        if backend is None or not backend.external:
            continue
        info: prov.ModelInfo | None = None
        listed = cached(name, backend)
        if listed is None:
            try:
                listed = await fetch_models(name, backend)
            except ModelListError as exc:
                log.info("hosted_caps_unknown", backend=name, error=str(exc)[:200])
                listed = []
        info = next((m for m in listed if m.id == model_id), None)
        if info is not None:
            caps[model_id] = prov.model_caps(info).model_dump()
        elif model_id not in caps:
            caps[model_id] = HostedModelCaps(json_mode="unknown").model_dump()
    return caps


async def note_manual_assignments(
    session: AsyncSession,
    settings_before: Settings,
    changes: dict[str, tuple[str, str] | None],
) -> None:
    """Settings > Hosted > Advanced wrote these assignments ((backend, model), or None for "stay
    local"). Keep the override state truthful: a pick that differs from the recorded default is
    an override (so Connect keeps it too), one that matches it or stays local is not. The caller
    holds STATE_LOCK across this and its commit."""
    defaults, overrides = await read_state(session)
    current = {st.tier: st for st in prov.assignment_status(settings_before)}
    await _put(session, CAPS_KEY, await caps_for_picks(session, settings_before, changes))
    for tier, pick in changes.items():
        if tier not in defaults:
            st = current.get(tier)
            defaults[tier] = (
                {"backend": st.backend, "model": st.model} if st and st.external else None
            )
        dflt = defaults.get(tier)
        same = isinstance(dflt, dict) and pick == (dflt.get("backend"), dflt.get("model"))
        if pick is None or same:
            overrides.pop(tier, None)
        else:
            overrides[tier] = {"backend": pick[0], "model": pick[1], "at": utcnow_iso()}
    await _put(session, DEFAULTS_KEY, defaults)
    await _put(session, OVERRIDES_KEY, overrides)
