"""Bring-your-own-key hosted providers (SPEC.md 6.4 / 8.7).

The operator may plug hosted models into Kestrel with a key they own. Two uses, both off until
turned on: Tier 3 escalation (per item, previewed and confirmed) and Hosted mode (any tier routed
to a hosted backend after an informed, recorded acknowledgement; see SPEC 6.4). Four providers are
known by name, and any OpenAI-compatible chat-completions server works as `generic`:

  - openrouter  -> https://openrouter.ai/api/v1   chat-completions protocol, key check at /key,
                   privacy routing via `provider` (data_collection, zdr, require_parameters).
  - openmodel   -> https://api.openmodel.ai/v1    Messages (Anthropic-style) or Responses protocol
                   per model (`supported_apis`); chat completions was removed. No key-inspection
                   endpoint, so the key is verified by listing models.
  - anthropic   -> https://api.anthropic.com/v1   the Claude API direct: Messages protocol with
                   x-api-key, structured output via output_config.format, no sampling parameters.
                   Key verified by listing models, which also reports context and capabilities.
  - openai      -> https://api.openai.com/v1      the OpenAI API direct: chat completions with a
                   strict json_schema response_format and max_completion_tokens. The model list
                   carries no capability or price data, so non-chat families are filtered by id.

What this module does NOT do (hard rule 1, CLAUDE.md Privacy): it never sends anything the
operator has not opted in to, and never writes a key anywhere except the data-home `.env`.

Everything here that touches the network is async and is called only from the Settings page, the
`kestrel provider` CLI, or `kestrel doctor`; never from the worker.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog

from kestrel import paths
from kestrel.config import BackendConfig, HostedModelCaps, Settings
from kestrel.errors import UserFacingError
from kestrel.llm.client import ANTHROPIC_VERSION, raise_for_provider_status

log = structlog.get_logger()

FRONTIER_TIER = "frontier"

# Backend name -> the provider it targets. These are the shipped external backends in
# settings.yml; the operator picks one for the frontier tier from the Settings page.
PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "backend": "frontier",
        "label": "OpenRouter",
        "env": "OPENROUTER_API_KEY",
        "key_prefix": "sk-or-",
        "console": "https://openrouter.ai/settings/keys",
        "privacy": "https://openrouter.ai/settings/privacy",
    },
    "openmodel": {
        "backend": "openmodel",
        "label": "OpenModel (openmodel.ai)",
        "env": "OPENMODEL_API_KEY",
        "key_prefix": "om-",
        "console": "https://console.openmodel.ai",
        "privacy": "https://www.openmodel.ai/privacy",
    },
    "anthropic": {
        "backend": "anthropic",
        "label": "Anthropic (Claude API)",
        "env": "ANTHROPIC_API_KEY",
        "key_prefix": "sk-ant-",
        "console": "https://platform.claude.com/settings/keys",
        "privacy": "https://www.anthropic.com/legal/privacy",
    },
    "openai": {
        "backend": "openai",
        "label": "OpenAI (API)",
        "env": "OPENAI_API_KEY",
        "key_prefix": "sk-",
        "console": "https://platform.openai.com/api-keys",
        "privacy": "https://platform.openai.com/docs/guides/your-data",
    },
}

# The reasoning-effort words each provider accepts, for the Settings > Hosted control. OpenRouter
# forwards all seven; the Claude API takes its five levels (never "none" or "minimal"); OpenAI
# takes none through high ("xhigh" and "max" would 400). openmodel.ai and generic backends have
# no wired control and get no row. The client applies the same lists when building a body.
EFFORT_LEVELS: dict[str, tuple[str, ...]] = {
    "openrouter": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "anthropic": ("low", "medium", "high", "xhigh", "max"),
    "openai": ("none", "minimal", "low", "medium", "high"),
}

# Suggested per-tier picks for providers whose model list carries no prices, so `kestrel setup
# --hosted` does not default to whatever sorts first by id (on Anthropic that is the most
# expensive model). Each id is used only when the provider actually lists it; otherwise the
# generic default (cheapest, then largest context, then id) applies. The frontier tier is set
# from Settings, not here.
SUGGESTED_PICKS: dict[str, dict[str, str]] = {
    "anthropic": {
        "tier1": "claude-haiku-5-5",
        "tier2": "claude-sonnet-5-5",
        "docsel": "claude-haiku-5-5",
    },
    "openai": {"tier1": "gpt-5-mini", "tier2": "gpt-5", "docsel": "gpt-5-mini"},
}

# OpenAI lists every model it serves under one endpoint, including families that cannot answer a
# chat completion at all. Ids containing one of these are left out of the pool; the rest are kept
# with capability unknown (never hidden on a guess, hard rule 4). The audio and search preview
# models do answer chat completions and stay in.
_OPENAI_NON_CHAT_MARKERS = (
    "embedding",
    "tts",
    "whisper",
    "transcribe",
    "dall-e",
    "image",
    "realtime",
    "moderation",
    "babbage",
    "davinci",
    "computer-use",
    "codex",
    "sora",
)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


@dataclass
class KeyStatus:
    ok: bool
    provider: str
    detail: str = ""
    label: str | None = None
    usage_usd: float | None = None
    limit_usd: float | None = None
    limit_remaining_usd: float | None = None
    is_free_tier: bool | None = None


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    context_length: int | None = None
    prompt_usd_per_m: float | None = None
    completion_usd_per_m: float | None = None
    structured_outputs: bool | None = None  # None = provider does not say
    protocols: list[str] = field(default_factory=list)
    created: int | None = None  # release time as a Unix timestamp, where the provider lists it
    # How this model is asked for JSON (config.JsonMode, D-UI-48): "schema" (structured_outputs
    # listed), "json_object" (only response_format), "prompt" (neither). None = not known.
    json_mode: str | None = None
    # The provider's supported_parameters for this model, None when it does not list them.
    supported_parameters: list[str] | None = None
    # OpenRouter's model reasoning record `mandatory`, None when it does not say.
    reasoning_mandatory: bool | None = None


# The picker's words for each JSON mode (D-UI-48).
JSON_MODE_LABELS: dict[str, str] = {
    "schema": "structured",
    "json_object": "JSON mode",
    "prompt": "no JSON guarantee",
    # Assigned while the model's record could not be fetched: sent as JSON mode (D-UI-48).
    "unknown": "JSON mode (capability unknown)",
}


def json_mode_from_parameters(params: list[str]) -> str:
    """OpenRouter's supported_parameters to a JSON mode: `structured_outputs` means a strict
    json_schema is honored; `response_format` alone means JSON mode (json_object); neither means
    the schema can only be asked for in the prompt (verified against the OpenRouter API
    reference, REFERENCES.md)."""
    if "structured_outputs" in params:
        return "schema"
    if "response_format" in params:
        return "json_object"
    return "prompt"


@dataclass
class ProviderStatus:
    """Everything the Settings page shows for the frontier role."""

    backend_name: str
    provider: str
    protocol: str
    base_url: str
    model: str
    env_name: str | None
    key_present: bool


# ------------------------------------------------------------------------------- secrets (.env)
def presets(settings: Settings) -> dict[str, dict[str, str]]:
    """Named services plus Other once its private address has been saved."""
    from kestrel.services.other_service import BACKEND, PRESET

    return {**PRESETS, **({"other": PRESET} if BACKEND in settings.llm.backends else {})}


def secret_present(env_name: str | None) -> bool:
    from kestrel.config import get_secret

    return bool(get_secret(env_name))


def secret_value(env_name: str | None) -> str:
    from kestrel.config import get_secret

    return get_secret(env_name) or ""


def masked(value: str) -> str:
    """Last four characters only, enough to recognize a key, never enough to use it."""
    if len(value) <= 8:
        return "****"
    return f"{'*' * 8}{value[-4:]}"


class SecretWriteError(UserFacingError, ValueError):
    """write_secret refused the name or value; the message is authored and holds no key."""


def write_secret(env_name: str, value: str) -> None:
    """Persist one secret to `<data home>/.env` and Kestrel's private key store. The file is the
    only place a key may live (CLAUDE.md); it is created 0600 where the platform honors modes. The
    value is never logged. An empty value removes the key."""
    if not _ENV_NAME_RE.match(env_name):
        raise SecretWriteError("refusing to write an unsafe env var name")
    if any(ch in value for ch in "\r\n"):
        raise SecretWriteError("a key cannot contain line breaks")
    from dotenv import set_key, unset_key

    from kestrel.config import _dotenv_state, is_secret_name

    if not is_secret_name(env_name):
        raise SecretWriteError(
            "that is not a key name (it must end in _KEY, _TOKEN, _SECRET or _PASSWORD)"
        )

    path = paths.dotenv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    # Kestrel's private store only; the process environment is never touched (D-BYOK-11).
    if value:
        set_key(str(path), env_name, value, quote_mode="never")
        _dotenv_state["secrets"] = {**_dotenv_state["secrets"], env_name: value}  # type: ignore[dict-item]
    else:
        if path.exists():
            unset_key(str(path), env_name)
        remaining = dict(_dotenv_state["secrets"])  # type: ignore[call-overload]
        remaining.pop(env_name, None)
        _dotenv_state["secrets"] = remaining
    _dotenv_state["mtime"] = None  # the next read re-parses the file
    _fsync(path)


def _fsync(path: Path) -> None:
    """Flush the rewritten key file (and, where the platform allows, its folder entry) to disk:
    python-dotenv's rewrite is atomic but does not fsync, so a power cut right after a save
    could otherwise lose the key. Best effort; a failure is logged, never raised."""
    try:
        with open(path, "rb+") as fh:
            os.fsync(fh.fileno())
        if os.name != "nt":  # Windows cannot open a directory for fsync
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        log.warning("key_file_fsync_failed", error=type(exc).__name__)


# ------------------------------------------------------------------------------ status helpers
def frontier_backend(settings: Settings) -> tuple[str, BackendConfig | None]:
    tcfg = settings.llm.tiers.get(FRONTIER_TIER)
    if tcfg is None:
        return "frontier", None
    return tcfg.backend, settings.llm.backends.get(tcfg.backend)


def external_backends(settings: Settings) -> dict[str, BackendConfig]:
    return {name: b for name, b in settings.llm.backends.items() if b.external}


def provider_status(settings: Settings) -> ProviderStatus:
    name, backend = frontier_backend(settings)
    tcfg = settings.llm.tiers.get(FRONTIER_TIER)
    if backend is None:
        return ProviderStatus(name, "generic", "chat_completions", "", "", None, False)
    return ProviderStatus(
        backend_name=name,
        provider=backend.provider,
        protocol=backend.protocol,
        base_url=backend.base_url,
        model=tcfg.model if tcfg else "",
        env_name=backend.api_key_env,
        key_present=bool(backend.api_key) or secret_present(backend.api_key_env),
    )


def _auth_headers(backend: BackendConfig) -> dict[str, str]:
    key = backend.api_key or ""
    if not key and backend.api_key_env:
        key = secret_value(backend.api_key_env)
    if backend.provider == "anthropic":
        # The Claude API takes the key in x-api-key (a bearer is read as an OAuth token).
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, **backend.extra_headers}
    return {"Authorization": f"Bearer {key}", **backend.extra_headers}


def _models_url(backend: BackendConfig) -> str:
    base = backend.base_url.rstrip("/")
    if backend.provider == "anthropic":
        # Paginated, 20 per page by default; the maximum page covers the whole catalog.
        return f"{base}/models?limit=1000"
    return f"{base}/models"


def openai_is_chat_model(model_id: str) -> bool:
    low = model_id.lower()
    return not any(marker in low for marker in _OPENAI_NON_CHAT_MARKERS)


def openrouter_is_chat_model(entry: dict) -> bool:
    """Whether one GET /models entry can answer /v1/chat/completions at all. Two kinds cannot and
    are dropped before any capability check (verified in testing, 2026-10-14): the `:batch`
    variant, which OpenRouter serves only through its batch endpoint and answers a chat request
    with HTTP 404 ("cannot be used with the chat/completions endpoint"), and a model whose
    `architecture.output_modalities` has no "text" (image, audio and music generators are listed
    beside the chat models, some of them priced at zero)."""
    model_id = str(entry.get("id") or "")
    if model_id.lower().endswith(":batch"):
        return False
    arch = entry.get("architecture")
    out = arch.get("output_modalities") if isinstance(arch, dict) else None
    if isinstance(out, list) and out and "text" not in [str(x).lower() for x in out]:
        return False
    return True


# The automatic hosted picks (kestrel.setup.default_picks) on a provider without suggested picks,
# OpenRouter above all. A price is a poor proxy for quality (the priciest affordable id can be a
# years-old model or a non-chat variant) and in a free-only pool every price is zero, so the
# generic rule prefers families by name, newest release first within a family. Order is the
# preference; a family absent from the pool is skipped. Matched as substrings of the lowercased
# id, so vendor prefixes ("deepseek/", "z-ai/glm", "moonshotai/kimi") and free variants both
# match. Kept short on purpose: this is a default the operator changes under Settings > Hosted >
# Advanced, not a leaderboard.
STRONG_FAMILIES: tuple[str, ...] = (
    "deepseek",
    "qwen",
    "glm",
    "kimi",
    "gpt-5",
    "claude",
    "gemini",
    "llama",
    "mistral",
    "gemma",
    "nemotron",
)
# Id tokens that mark a family's fast or small member, preferred for tier1 (triage) and docsel,
# plus any parameter-count token of 32b or less. A model with none of these is still eligible
# for those tiers; it just ranks after the fast ones. Whole tokens only (see _id_tokens).
FAST_TOKENS: frozenset[str] = frozenset(
    {"flash", "mini", "nano", "lite", "small", "haiku", "fast", "a3b"}
)
# Models that are never an automatic pick: reasoning-first or preview builds that are slow,
# bill thinking tokens, or disappear, and OpenRouter routing variants. Still listed; the
# operator can name them. Tokens match whole id tokens; suffixes match the `:variant` tail.
UNPICKED_TOKENS: frozenset[str] = frozenset({"r1", "reasoning", "thinking", "preview", "exp"})
UNPICKED_SUFFIXES: tuple[str, ...] = (":extended", ":online", ":nitro", ":floor", ":beta")


_ID_TOKEN = re.compile(r"[a-z0-9]+")
_SMALL_SIZE = re.compile(r"^(\d{1,2})b$")  # a parameter count token like 7b, 27b (not 70b+)


def _id_tokens(model_id: str) -> list[str]:
    """The id split on every non-alphanumeric: `google/gemini-2.5-flash:free` is
    [google, gemini, 2, 5, flash, free]. Markers match whole tokens, never substrings, so
    "mini" is not found inside "gemini" and "exp" not inside "expert"."""
    return _ID_TOKEN.findall(model_id.lower())


def family_rank(model_id: str) -> int:
    """Index into STRONG_FAMILIES of the first family the id names, len() when none."""
    low = model_id.lower()
    for i, fam in enumerate(STRONG_FAMILIES):
        if fam in low:
            return i
    return len(STRONG_FAMILIES)


def is_fast_variant(model_id: str) -> bool:
    tokens = _id_tokens(model_id)
    if any(t in FAST_TOKENS for t in tokens):
        return True
    for t in tokens:
        m = _SMALL_SIZE.match(t)
        if m and int(m.group(1)) <= 32:
            return True
    return False


def is_pickable(model_id: str) -> bool:
    low = model_id.lower()
    if any(low.endswith(suffix) or f"{suffix}:" in low for suffix in UNPICKED_SUFFIXES):
        return False
    return not any(t in UNPICKED_TOKENS for t in _id_tokens(model_id))


def is_free_variant(model_id: str) -> bool:
    return model_id.lower().endswith(":free")


def _f(x: object) -> float | None:
    try:
        return float(x) if x is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------ network calls
async def verify_key(
    backend: BackendConfig, *, http: httpx.AsyncClient | None = None, timeout: float = 20.0
) -> KeyStatus:
    """Confirm the configured key is accepted by the provider. OpenRouter has a dedicated endpoint
    that also returns usage and limits; OpenModel, Anthropic, OpenAI (and generic) are verified by
    listing models, which is the cheapest authenticated call and costs nothing."""
    provider = backend.provider
    if not (backend.api_key or secret_present(backend.api_key_env)):
        return KeyStatus(False, provider, f"{backend.api_key_env or 'api key'} is not set")
    client = http or httpx.AsyncClient()
    base = backend.base_url.rstrip("/")
    try:
        if provider == "openrouter":
            r = await client.get(f"{base}/key", headers=_auth_headers(backend), timeout=timeout)
            raise_for_provider_status(r)
            d = (r.json() or {}).get("data") or {}
            return KeyStatus(
                True,
                provider,
                "key accepted",
                label=d.get("label"),
                usage_usd=_f(d.get("usage")),
                limit_usd=_f(d.get("limit")),
                limit_remaining_usd=_f(d.get("limit_remaining")),
                is_free_tier=d.get("is_free_tier"),
            )
        r = await client.get(_models_url(backend), headers=_auth_headers(backend), timeout=timeout)
        raise_for_provider_status(r)
        n = len((r.json() or {}).get("data") or [])
        return KeyStatus(True, provider, f"key accepted; {n} models listed")
    except httpx.HTTPError as exc:
        return KeyStatus(False, provider, f"{type(exc).__name__}: {exc}")
    except RuntimeError as exc:  # ProviderError
        return KeyStatus(False, provider, str(exc))
    except (ValueError, AttributeError, TypeError) as exc:  # a 200 that is not JSON
        return KeyStatus(False, provider, f"unexpected provider response: {type(exc).__name__}")
    finally:
        if http is None:
            await client.aclose()


async def list_models(
    backend: BackendConfig, *, http: httpx.AsyncClient | None = None, timeout: float = 30.0
) -> list[ModelInfo]:
    """The provider's model catalog, normalized. Raises on transport/HTTP errors so the caller
    can show the provider's own message."""
    client = http or httpx.AsyncClient()
    try:
        r = await client.get(_models_url(backend), headers=_auth_headers(backend), timeout=timeout)
        raise_for_provider_status(r)
        data = (r.json() or {}).get("data") or []
    finally:
        if http is None:
            await client.aclose()
    out: list[ModelInfo] = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        info = ModelInfo(id=str(m["id"]), name=str(m.get("name") or m["id"]))
        if backend.provider == "anthropic":
            # GET /v1/models: display_name, max_input_tokens (the context window), and a
            # capabilities tree with `supported` at each leaf. No prices.
            info.name = str(m.get("display_name") or m["id"])
            ctx = m.get("max_input_tokens")
            info.context_length = int(ctx) if isinstance(ctx, int) else None
            caps = m.get("capabilities")
            so = caps.get("structured_outputs") if isinstance(caps, dict) else None
            if isinstance(so, dict) and isinstance(so.get("supported"), bool):
                info.structured_outputs = so["supported"]
        elif backend.provider == "openai":
            # GET /v1/models: id, created, owned_by only. Non-chat families are dropped here.
            if not openai_is_chat_model(info.id):
                continue
        elif backend.provider == "openrouter":
            if not openrouter_is_chat_model(m):
                continue
            info.context_length = m.get("context_length")
            created = m.get("created")
            info.created = int(created) if isinstance(created, int | float) else None
            pricing = m.get("pricing") or {}
            p, c = _f(pricing.get("prompt")), _f(pricing.get("completion"))
            info.prompt_usd_per_m = p * 1_000_000 if p is not None else None
            info.completion_usd_per_m = c * 1_000_000 if c is not None else None
            sp = m.get("supported_parameters")
            if isinstance(sp, list):
                # `structured_outputs` is a strict json_schema; `response_format` alone is JSON
                # mode, which takes no schema (a json_schema request with require_parameters
                # would be routed nowhere), so such a model is sent json_object with the schema
                # in the prompt; neither is prompt-only (D-UI-48).
                params = [str(x) for x in sp]
                info.supported_parameters = params
                info.structured_outputs = "structured_outputs" in params
                info.json_mode = json_mode_from_parameters(params)
            rec = m.get("reasoning")
            if isinstance(rec, dict) and isinstance(rec.get("mandatory"), bool):
                info.reasoning_mandatory = rec["mandatory"]
        else:
            # openmodel.ai lists the protocols a model speaks under `supported_apis` (verified
            # against docs.openmodel.ai, 2026-10-12); older text used `supported_protocols`.
            protos = m.get("supported_apis") or m.get("supported_protocols")
            if isinstance(protos, list):
                info.protocols = [str(x) for x in protos]
        out.append(info)
    return out


def models_fit_for_tier3(models: list[ModelInfo], backend: BackendConfig) -> list[ModelInfo]:
    """The models Kestrel can ask for a JSON reply. On OpenRouter that is every chat model
    (D-UI-48): one with `structured_outputs` gets a strict schema, one with only
    `response_format` gets JSON mode with the schema in the prompt, and one with neither is
    asked in the prompt alone and labeled "no JSON guarantee". Every reply is validated the same
    way regardless (hard rule 3), so a weak model fails into quarantine, not into the pipeline.
    Anthropic says per model whether it honors a schema and there is no JSON-mode fallback on
    the Messages protocol; OpenModel says which protocols a model speaks, and Tier 3 needs
    `responses`; OpenAI says nothing, so its (chat-only) list passes through. A model whose
    capability is unknown is kept, labeled unknown, never hidden."""
    if backend.provider == "openrouter":
        return list(models)
    if backend.provider == "anthropic":
        return [m for m in models if m.structured_outputs is not False]
    if backend.provider == "openmodel":
        return [m for m in models if not m.protocols or protocol_for_model(m) is not None]
    return models


def model_caps(m: ModelInfo) -> HostedModelCaps:
    """What the request builder and the description trim need to know about `m`, recorded at
    assignment time under `llm.hosted_model_caps` (config.HostedModelCaps)."""
    mode = (
        m.json_mode if m.json_mode in ("schema", "json_object", "prompt", "unknown") else "schema"
    )
    return HostedModelCaps(
        json_mode=mode,  # type: ignore[arg-type]
        context_length=m.context_length if isinstance(m.context_length, int) else None,
        params=list(m.supported_parameters) if m.supported_parameters is not None else None,
        reasoning_mandatory=m.reasoning_mandatory,
    )


def json_mode_label(m: ModelInfo) -> str:
    """The picker's word for how `m` returns JSON; unknown reads as structured (the default the
    request builder applies)."""
    return JSON_MODE_LABELS.get(m.json_mode or "schema", JSON_MODE_LABELS["schema"])


def free_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Models the provider prices at exactly zero for BOTH prompt and completion (OpenRouter's
    `:free` variants). Unknown pricing is NOT free: a model that does not state a price is kept
    out of the free list rather than offered as free and billed later."""
    return [
        m
        for m in models
        if m.prompt_usd_per_m is not None
        and m.completion_usd_per_m is not None
        and m.prompt_usd_per_m == 0
        and m.completion_usd_per_m == 0
    ]


# The tiers Hosted mode may route off-machine (SPEC.md 6.4), in the order the Settings table, the
# doctor line and `kestrel setup --hosted` list them. `tier3` is the local deep model behind the
# operator-initiated prompts (cover letters, engagement outlines, research, contacts, negotiation)
# and `trigger` the P10 filing/feed classifier; both run through the same redaction plan as tier1
# and tier2. `embed` is deliberately absent: embeddings never leave the machine (LLMClient.embed
# refuses an external backend); a machine without Ollama runs them in-process (SPEC.md 8.1.1).
HOSTED_TIERS: tuple[str, ...] = ("tier1", "tier2", "tier3", "docsel", "trigger", FRONTIER_TIER)


@dataclass
class TierStatus:
    """One row of the Settings > Hosted per-tier table and of the doctor's Hosted mode line."""

    tier: str
    backend: str
    model: str
    external: bool
    provider: str = "generic"
    env_name: str | None = None
    key_present: bool = True  # local backends need no key
    # True when a hosted send from this tier would actually happen: external AND hosted mode
    # is on and acknowledged (or the tier is the frontier, which has its own per-item confirm).
    in_use: bool = False
    # assignment_status only: False when the tier has no entry in llm.hosted_tiers, i.e. it
    # stays local even under mode "hosted".
    assigned: bool = True


def assignment_status(settings: Settings) -> list[TierStatus]:
    """One row per hostable tier for Settings > Hosted > Hosted assignments: what the tier runs
    on UNDER MODE "hosted" (settings.llm.hosted_tiers), whatever mode is in effect now. The
    frontier tier is always hosted and lives in llm.tiers, so its row reads from there."""
    hosted_on = settings.llm.mode == "hosted" and settings.llm.hosted_mode.active
    out: list[TierStatus] = []
    for tier in HOSTED_TIERS:
        if tier == FRONTIER_TIER:
            tcfg = settings.llm.tiers.get(tier)
        else:
            tcfg = settings.llm.hosted_tiers.get(tier)
        if tcfg is None:
            out.append(TierStatus(tier, "local", "", False, assigned=False))
            continue
        backend = settings.llm.backends.get(tcfg.backend)
        if backend is None or not backend.external:
            out.append(TierStatus(tier, tcfg.backend, tcfg.model, False, assigned=False))
            continue
        out.append(
            TierStatus(
                tier=tier,
                backend=tcfg.backend,
                model=tcfg.model,
                external=True,
                provider=backend.provider,
                env_name=backend.api_key_env,
                key_present=bool(backend.api_key) or secret_present(backend.api_key_env),
                in_use=hosted_on or tier == FRONTIER_TIER,
            )
        )
    return out


def tier_status(settings: Settings) -> list[TierStatus]:
    """Backend, model, and key status per hostable tier, in HOSTED_TIERS order, as the tiers
    are EFFECTIVELY configured (settings.llm.tiers after the mode overlay): what doctor and the
    pipeline see. For the hosted assignments themselves see assignment_status."""
    active = settings.llm.hosted_mode.active
    out: list[TierStatus] = []
    for tier in HOSTED_TIERS:
        tcfg = settings.llm.tiers.get(tier)
        if tcfg is None:
            continue
        backend = settings.llm.backends.get(tcfg.backend)
        external = bool(backend and backend.external)
        if backend is None or not external:
            out.append(TierStatus(tier, tcfg.backend, tcfg.model, False))
            continue
        out.append(
            TierStatus(
                tier=tier,
                backend=tcfg.backend,
                model=tcfg.model,
                external=True,
                provider=backend.provider,
                env_name=backend.api_key_env,
                key_present=bool(backend.api_key) or secret_present(backend.api_key_env),
                in_use=active or tier == FRONTIER_TIER,
            )
        )
    return out


def protocol_for_model(model: ModelInfo) -> str | None:
    """Which wire protocol to use for an openmodel.ai model: `messages` when it speaks it,
    else `responses`, else None (chat completions no longer exists there). A model that lists no
    protocols returns None, so the caller keeps the configured protocol."""
    if "messages" in model.protocols:
        return "messages"
    if "responses" in model.protocols:
        return "responses"
    return None
