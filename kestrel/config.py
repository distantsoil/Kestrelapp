"""Typed settings and the SPEC.md 4.6 precedence chain.

Precedence, highest first: setting-table row -> environment (KESTREL_*) -> YAML -> code default.
Re-read on every call so a change takes effect without a restart.

Bootstrap ordering (SPEC.md 4.6 / BUILD_PROMPT): paths and logging must resolve BEFORE the
database exists. paths.py depends on nothing here; log_level and any path-shaping key must come
only from environment and YAML, never from the database-override layer. The db_overrides argument
below is therefore the setting-table layer and must exclude bootstrap keys when Phase 1 wires it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from kestrel import paths
from kestrel.errors import UserFacingError


# ------------------------------------------------------------------------------- LLM settings
class HostedOptions(BaseModel):
    """Provider-specific request options for a hosted (BYOK) backend. Every field is a privacy or
    cost control that rides in the request body; none is a secret. SPEC.md 6.4 / 8.7."""

    # OpenRouter `provider.require_parameters`: route only to endpoints that honor every parameter
    # we send (the JSON schema above all). Without it a request can land on an endpoint that
    # silently ignores response_format and returns prose, which then quarantines.
    require_parameters: bool = True
    # OpenRouter `provider.data_collection`: "deny" excludes providers that store or train on
    # prompts. The default is the private one; the operator relaxes it deliberately.
    data_collection: Literal["allow", "deny"] = "deny"
    # OpenRouter `provider.zdr`: restrict routing to Zero Data Retention endpoints.
    zdr: bool = False
    # OpenRouter `reasoning.effort` for models that expose it. None sends nothing (model default).
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = (
        None
    )
    # OpenRouter sampling penalties against generation loops (D-LLM-2): a free model repeated one
    # enum value in an array until max_tokens. `repetition_penalty` is multiplicative (1.0 = off,
    # above 1.0 penalizes tokens already produced); `frequency_penalty` is additive (-2.0..2.0,
    # 0 = off). Both are documented per-request chat parameters (REFERENCES.md). Sent on the
    # OpenRouter backend only, and only to a model whose supported_parameters list the field
    # (or, when that is unknown, only when require_parameters is off for the request), so a
    # penalty never narrows routing to "no endpoint". None sends nothing. Kept mild: a strong
    # penalty also penalizes the quotes and commas every JSON reply repeats.
    repetition_penalty: float | None = 1.05
    frequency_penalty: float | None = None
    # The guard's repair after a looping reply (D-LLM-5): when the model's supported_parameters
    # lack repetition_penalty but list frequency_penalty, that repair alone sends this value.
    # Never on a first attempt, never to a model that does not list it, and require_parameters
    # is never relaxed for it (that could drop response_format). None sends nothing.
    repair_frequency_penalty: float | None = 0.3
    # Tier 1 triage is a short classification: on a hosted OpenRouter reasoning model whose
    # record says reasoning is optional, and with no effort set above or in the prompt, the FIRST
    # Tier 1 attempt asks for reasoning off (D-LLM-5: 18 of 43 calls spent the 1,200-token cap
    # thinking). False restores the model's default reasoning on that attempt.
    tier1_reasoning_off: bool = True


# How a hosted model is asked for JSON (D-UI-48): "schema" is a strict json_schema
# response_format; "json_object" is JSON mode with the schema in the system prompt; "prompt" is
# neither (the schema in the system prompt only, no JSON guarantee). Validation is identical for
# all three (hard rule 3). "unknown" is recorded when a model was assigned while its record could
# not be fetched: it is sent as "json_object" with require_parameters off, the mode that routes
# on any endpoint taking response_format, never as a strict schema that might route nowhere.
JsonMode = Literal["schema", "json_object", "prompt", "unknown"]


class HostedModelCaps(BaseModel):
    """What the provider's model list said about one hosted model, recorded when it is assigned
    (Connect, Models page overrides) so the request builder and the description trim can use it
    without a network call. Keyed by model id under `llm.hosted_model_caps`, so a model changed
    elsewhere simply has no entry and gets the backend defaults (a strict schema)."""

    json_mode: JsonMode = "schema"
    context_length: int | None = None
    # The provider's supported_parameters, None when it does not say.
    params: list[str] | None = None
    # The model record's reasoning.mandatory (OpenRouter): True when reasoning cannot be turned
    # off, so effort "none" would be rejected. None when the record does not say.
    reasoning_mandatory: bool | None = None


INPROCESS_ENGINE = "inprocess"


class BackendConfig(BaseModel):
    # Empty for the in-process engine, which has no endpoint.
    base_url: str = ""
    # "http" is every backend that answers over HTTP (Ollama, OpenRouter, the direct APIs).
    # "inprocess" is the bundled CPU embedding model (kestrel.llm.inprocess_embed, SPEC.md 8.1.1):
    # it serves the embed tier only, never sends anything anywhere, and needs no Ollama. The
    # shipped settings.yml defines it as the backend named `inprocess`.
    engine: Literal["http", "inprocess"] = "http"
    api_key: str | None = None
    api_key_env: str | None = None
    json_mode: Literal["json_schema", "json_object"] = "json_schema"
    thinking_control: Literal["reasoning_effort", "chat_template_kwargs", "none"] = (
        "reasoning_effort"
    )
    requires_num_ctx_modelfile: bool = False
    # Static headers sent with every request to this backend, e.g. OpenRouter's HTTP-Referer and
    # X-Title attribution headers. Never put a secret here; the API key comes from api_key_env.
    extra_headers: dict[str, str] = {}
    # True marks the ONLY kind of backend permitted to transmit operator data off the machine
    # (hard rule 1). Reachable through operator-confirmed Tier 3 escalation, or by any tier once
    # the operator has turned on Hosted mode (LLMSettings.hosted_mode, SPEC.md 6.4). Several
    # backends may be external (OpenRouter, OpenModel, Anthropic, OpenAI); a tier names at most one.
    external: bool = False
    # Wire protocol. "chat_completions" is /v1/chat/completions (Ollama, OpenRouter, any
    # OpenAI-compatible server). "responses" is the OpenAI Responses protocol (/v1/responses),
    # which is the only one openmodel.ai serves; its chat-completions endpoint was removed.
    # "messages" is the Anthropic Messages protocol (/v1/messages), which openmodel.ai also serves
    # (https://docs.openmodel.ai/en/docs/api-reference/messages); a model's `supported_protocols`
    # in its model list says which of the two it speaks, and the Settings page picks per model.
    protocol: Literal["chat_completions", "responses", "messages"] = "chat_completions"
    # Which provider's conventions to apply on top of the protocol (key check endpoint, model list
    # shape, privacy routing fields). "generic" sends nothing provider-specific. "anthropic" is
    # the Claude API direct (Messages protocol, x-api-key, output_config structured output, no
    # sampling parameters); "openai" is the OpenAI API direct (chat completions with
    # max_completion_tokens and a strict json_schema response_format).
    provider: Literal["generic", "openrouter", "openmodel", "anthropic", "openai"] = "generic"
    hosted: HostedOptions = HostedOptions()


class TierConfig(BaseModel):
    backend: str = "local"
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    # Per-request HTTP timeout for this tier's calls (seconds). None = the client default (180).
    timeout_s: float | None = None


# The Tier 1 description head (SPEC.md 6.2), stated in the disclosure. Equal to
# kestrel.pipeline.context.TIER1_HEAD_TOKENS (a test keeps the two the same).
HOSTED_TIER1_HEAD_TOKENS = 1200

# What each hostable tier transmits once it is routed to a hosted backend: the one source for the
# disclosure on Settings > Hosted, `kestrel setup --hosted`, and the acknowledgement record
# (HostedMode.disclosure_terms). `{t1}`/`{t2}` are the Tier 1 head and the Tier 2 ceiling.
HOSTED_TIER_SENDS: dict[str, str] = {
    "tier1": "title, organization, location, and the first {t1} tokens of the description",
    "tier2": (
        "the description slice (up to {t2} tokens in hosted mode), the organization context "
        "Kestrel has gathered, your confirmed labels used as examples, and your full profile; "
        "and, only when you click Assess on Profile > Documents, your resume text"
    ),
    "tier3": (
        "the opportunity, the research gathered on it, and your profile, for cover letters, "
        "engagement outlines, research summaries, contact finding and negotiation prep"
    ),
    "docsel": "your resume variant texts and the opportunity summary",
    "trigger": (
        "the public filing or feed item being classified for the consulting track (SEC EDGAR, "
        "RSS, PE portfolio pages)"
    ),
    "frontier": ("the Tier 2 prompt for one opportunity, only after you preview and confirm it"),
}


class HostedMode(BaseModel):
    """Hosted mode (SPEC.md 6.4): the operator's explicit, informed opt-in to run ANY tier on a
    hosted model through their own key. Off by default. A tier that names an external backend is
    only USED when `enabled` and `acknowledged_at` are both set; otherwise the client refuses it
    exactly as it always has (ExternalBackendError). Tier 3 escalation is unaffected: it carries
    its own per-item confirmation and does not need this."""

    enabled: bool = False
    # ISO timestamp written when the operator accepts the disclosure on Settings > Hosted. Kept
    # separate from `enabled` so a bare `enabled: true` in YAML cannot skip the acknowledgement.
    acknowledged_at: str | None = None
    # With confidential mode on, redact the current employer before a hosted send and REFUSE the
    # send when there is no redaction map (same rule as Tier 3). False means the operator has
    # chosen to send unredacted; it is not exposed in the UI and must be set deliberately.
    redact_confidential: bool = True
    # The description slice on a tier whose backend is external (D-LLM-3). The slice is sized to
    # the chosen model's context window (llm.hosted_model_caps) minus the prompt and the output
    # budget, never below `max_jd_tokens` (the floor) and never above `jd_ceiling_tokens` (the
    # cost bound the disclosure states). Unknown context: the local budget, at least the floor.
    # Tier 2 keeps the head and a tail window (comp and location often sit at the end).
    max_jd_tokens: int = 3000
    jd_ceiling_tokens: int = 16000
    # The "Free models only" choice made on the last Connect (Settings > Hosted or
    # `kestrel setup --hosted --free`). Recorded so the form shows the standing choice and the
    # operator can see, and reverse, it; the picks themselves are the per-tier rows.
    free_only: bool = False
    # What the disclosure said when it was accepted (disclosure_terms(): the Tier 1 head, the
    # Tier 2 ceiling, and the filled per-tier "sends" lines). Written with acknowledged_at. When
    # the current disclosure differs (a raised ceiling, a new item sent) the acknowledgement is
    # void: `active` is False and the client refuses every hosted send until the operator
    # accepts the new disclosure (Privacy: what is sent is stated before the operator confirms).
    acknowledged_terms: dict[str, Any] | None = None

    def disclosure_terms(self) -> dict[str, Any]:
        """The disclosure as it reads now, in comparable form."""
        t1 = min(HOSTED_TIER1_HEAD_TOKENS, self.max_jd_tokens)
        t2 = max(self.max_jd_tokens, self.jd_ceiling_tokens)
        return {
            "tier1_head_tokens": t1,
            "tier2_ceiling_tokens": t2,
            "sends": {k: v.format(t1=t1, t2=t2) for k, v in HOSTED_TIER_SENDS.items()},
        }

    @property
    def acknowledgement_current(self) -> bool:
        """The operator accepted THIS disclosure, not an earlier one."""
        return bool(self.acknowledged_at) and self.acknowledged_terms == self.disclosure_terms()

    @property
    def active(self) -> bool:
        return self.enabled and self.acknowledgement_current


RunMode = Literal["local", "hosted"]
MODES: tuple[str, ...] = ("local", "hosted")


class LLMSettings(BaseModel):
    backends: dict[str, BackendConfig]
    # The LOCAL tier set: the active hardware profile's derived models. What the pipeline reads
    # (`settings.llm.tiers[...]`) is this set, overlaid tier by tier with `hosted_tiers` when the
    # effective mode is "hosted" (load_settings / apply_mode, SPEC.md 6.4, D-HOSTED-2).
    tiers: dict[str, TierConfig]
    # The HOSTED tier set: which external backend and model each tier uses under mode "hosted".
    # A tier absent here stays local in either mode. `embed` is never hosted: it runs on Ollama
    # or on the in-process engine (the hosted_only profile routes it there in `llm.tiers`).
    # Written by Settings > Hosted and `kestrel setup --hosted`; never read directly by the
    # pipeline.
    hosted_tiers: dict[str, TierConfig] = {}
    # The switch: "local" runs every tier on this machine; "hosted" overlays hosted_tiers. A run
    # may override it for its own duration (Worker.run(mode=...), `kestrel run --hosted|--local`,
    # POST /api/run/now). "hosted" is only honored with hosted_mode acknowledged AND tier1 and
    # tier2 assigned; otherwise the effective mode is local (hosted_mode_problem says why).
    mode: RunMode = "local"
    ollama_native_url: str = "http://127.0.0.1:11434"
    hosted_mode: HostedMode = HostedMode()
    # Per hosted model id: how to ask it for JSON and its context window (HostedModelCaps).
    # One setting row holds the whole map (model ids contain dots, which a dotted key cannot).
    hosted_model_caps: dict[str, HostedModelCaps] = {}


# --------------------------------------------------------------------------- pipeline / scoring
class Concurrency(BaseModel):
    tier1: int = 4
    tier2: int = 2
    # Embedding is a small, fast model (nomic-embed-text) and does not contend with the tier
    # models for VRAM the way tier1/tier2 do, so it can run wider without causing swap thrash.
    # The in-process engine serializes its own runs, so a wider setting only queues there.
    embed: int = 4


class Pipeline(BaseModel):
    tier1_min_confidence: float = 0.6
    max_jd_tokens: int = 6000
    # Roles from a found company board that the smart filter did not keep are scored in the
    # background, at most this many a day (D-SRC-8). 0 turns the background scoring off.
    lane_daily_cap: int = 200


class EmploymentWeights(BaseModel):
    role_fit: float = 0.30
    domain_match: float = 0.20
    company_context: float = 0.20
    comp_logistics: float = 0.15
    trajectory: float = 0.15


class ConsultingWeights(BaseModel):
    engagement_fit: float = 0.30
    buyer_access: float = 0.20
    economics: float = 0.20
    delivery_feasibility: float = 0.15
    strategic_value: float = 0.15


class ScoringWeights(BaseModel):
    employment: EmploymentWeights = EmploymentWeights()
    consulting: ConsultingWeights = ConsultingWeights()


class Scoring(BaseModel):
    divergence_flag: float = 0.75
    weights: ScoringWeights = ScoringWeights()


class Thresholds(BaseModel):
    apply: float = 4.0
    consider: float = 3.0


class ConsultingSettings(BaseModel):
    min_trigger_confidence: float = 0.5
    max_trigger_age_days: int = 120


class Learning(BaseModel):
    min_labels_for_calibration: int = 30
    few_shot_k: int = 3
    # Retrieval benefits from far fewer examples than regression, so this bar (PROMPTS.md FEW_SHOT)
    # is deliberately lower than min_labels_for_calibration.
    min_labels_for_few_shot: int = 15
    min_labels_for_reranker: int = 200
    reranker_max_mae: float = 0.6
    reranker_uncertain_band: float = 0.4
    drift_threshold: float = 0.8


class Stories(BaseModel):
    merge_threshold: float = 0.88


class Docs(BaseModel):
    max_pages: int = 2


class Liveness(BaseModel):
    cadence_days: int = 3
    recheck_hours: int = 24  # a posting with one expired read is read again after this long


class Notify(BaseModel):
    enabled: bool = True
    high_score: float = 4.5
    ntfy_topic: str | None = None
    webhook_url: str | None = None
    toast: bool = True  # Windows toast via win11toast, default on
    # Per-item scored notifications MUST stay off by default: a tool that pings on every item gets
    # muted and then the useful notifications never land either (SPEC-INTERFACES 6).
    per_item_scored: bool = False
    # Per-event-kind toggles; a kind absent from the map is on. Every event is individually
    # toggleable (SPEC-INTERFACES 6). The delivery layer batches to at most one per minute.
    events: dict[str, bool] = {}
    batch_seconds: int = 60  # never more than one delivered notification per this window


class Research(BaseModel):
    searx_url: str | None = None


class MCP(BaseModel):
    enabled: bool = False
    port: int = 9110


class Server(BaseModel):
    host: str = "127.0.0.1"
    bind_all: bool = False
    # Hostnames at which the operator publishes Kestrel through a reverse tunnel (docs/WEBAPP.md:
    # Cloudflare Tunnel + Access). A request arriving through the tunnel reaches loopback but
    # carries the public hostname as Host and as Origin on HTMX POSTs, so both guards need to
    # know it. Exact hostnames only, never wildcards; https origins only.
    public_hostnames: list[str] = []

    @field_validator("public_hostnames")
    @classmethod
    def _exact_hostnames(cls, v: list[str]) -> list[str]:
        """Starlette's TrustedHost treats a leading `*` as a suffix wildcard, which would re-open
        DNS rebinding on the loopback bind where the LAN key is exempt. Refuse anything that is not
        a plain, lowercase hostname; a bad entry fails config load loudly rather than silently
        widening the guard."""
        out: list[str] = []
        for raw in v:
            h = (raw or "").strip().lower()
            if not h:
                continue
            if any(ch in h for ch in "*/:@? \t") or h.startswith(".") or h.endswith("."):
                raise ValueError(f"server.public_hostnames: {raw!r} is not an exact hostname")
            out.append(h)
        return out


class Ingest(BaseModel):
    # Descriptive User-Agent with a contact address (mandatory for SEC EDGAR; polite everywhere).
    user_agent: str = "Kestrel/1.0 (+https://github.com/kestrel-app; contact: operator@example.com)"
    default_rate_per_sec: float = 2.0
    workday_rate_per_sec: float = 1.0
    sec_rate_per_sec: float = 10.0
    detail_concurrency: int = 4
    request_timeout_s: float = 60.0
    respect_robots: bool = True


class Pursuits(BaseModel):
    # Default ghosting threshold until outcome data sets it empirically (SPEC-MODULES.md 5.6).
    ghosting_days: int = 21


UI_SCHEMES: tuple[str, ...] = ("indigo", "teal")


class Updates(BaseModel):
    """The update checker (SPEC-INTERFACES.md 1.5). It only asks; it never downloads."""

    enabled: bool = True
    # Literal rather than importing updater.FEED_URL: keep config free of service imports.
    # Must match kestrel.services.updater.FEED_URL (a test pins it).
    feed_url: str = "https://kestrelapp.co/latest.json"
    check_hours: int = 24


class UI(BaseModel):
    """Appearance (docs/UI_PROPOSAL.html). `scheme` is the color scheme the server renders as
    `data-scheme` on <html> for the first paint; the browser's own localStorage copy then wins.
    Light/dark/system is per browser only and never stored here."""

    scheme: Literal["indigo", "teal"] = "indigo"
    # Desktop window: closing it hides Kestrel to the tray (the worker keeps running) instead of
    # quitting. On by default; Settings > Appearance turns it off (D-UI-8).
    close_to_tray: bool = True


# --------------------------------------------------------------------------------- top-level
class _MissingBackendsSource(PydanticBaseSettingsSource):
    """The packaged `llm.backends` entries whose names the seeded settings.yml does not define."""

    def __init__(self, settings_cls, seeded: str, packaged: Path) -> None:
        super().__init__(settings_cls)
        mine = YamlConfigSettingsSource(settings_cls, yaml_file=seeded)()
        shipped = YamlConfigSettingsSource(settings_cls, yaml_file=str(packaged))()
        have = set((mine.get("llm") or {}).get("backends") or {})
        extra = {
            name: cfg
            for name, cfg in ((shipped.get("llm") or {}).get("backends") or {}).items()
            if name not in have
        }
        self._data = {"llm": {"backends": extra}} if extra else {}

    def get_field_value(self, field, field_name):  # pragma: no cover - __call__ is used
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict:
        return self._data


# Holds the resolved settings.yml path for the YAML source (set per load_settings call so the
# path stays dynamic and re-readable).
_yaml_path: dict[str, str | None] = {"path": None}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KESTREL_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    llm: LLMSettings
    concurrency: Concurrency = Concurrency()
    pipeline: Pipeline = Pipeline()
    scoring: Scoring = Scoring()
    thresholds: Thresholds = Thresholds()
    consulting: ConsultingSettings = ConsultingSettings()
    learning: Learning = Learning()
    stories: Stories = Stories()
    docs: Docs = Docs()
    liveness: Liveness = Liveness()
    notify: Notify = Notify()
    research: Research = Research()
    mcp: MCP = MCP()
    server: Server = Server()
    ingest: Ingest = Ingest()
    pursuits: Pursuits = Pursuits()
    ui: UI = UI()
    updates: Updates = Updates()

    # Top-level so Tier 0 rules can read `settings.max_posting_age_days` exactly as SPEC.md 6.1
    # writes it (its sibling is nested as `settings.consulting.max_trigger_age_days`).
    max_posting_age_days: int = 45
    confidential_mode: bool = False
    # Ignored: superseded by updates.feed_url. Old feeds were a different format, so this value
    # is deliberately not migrated.
    update_check_url: str | None = None
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence, first wins: init (setting-table row) > env > dotenv > YAML > field defaults.
        sources = [init_settings, env_settings, dotenv_settings]
        yaml_file = _yaml_path["path"]
        if yaml_file:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file))
        # Seeding never overwrites the operator's file, so a backend shipped in a later version
        # (a new hosted provider) is added from the packaged copy by name. Only whole missing
        # backends: nothing fills fields into an entry the seeded file has, and nothing else
        # (tiers included) comes back (D-CONFIG-LAYER).
        packaged = Path(str(paths.packaged_config() / "settings.yml"))
        if yaml_file and packaged.is_file() and str(packaged) != yaml_file:
            sources.append(_MissingBackendsSource(settings_cls, yaml_file, packaged))
        sources.append(file_secret_settings)
        return tuple(sources)


# The data-home `.env` is the ONLY place a secret may live (CLAUDE.md). pydantic-settings' dotenv
# source only feeds Settings fields, and an API key is not a field: the backend names its env var
# (`api_key_env`). The file is read here once per change. A secret (is_secret_name) is
# held in a private store and NEVER placed in the process environment, so no program Kestrel
# launches inherits it, and a same-named system variable (an OPENAI_API_KEY other tools use) is
# ignored unless KESTREL_USE_SYSTEM_KEYS=1; even then the key saved in Kestrel wins (D-BYOK-11).
# Any other name is configuration and goes to os.environ without overriding a real variable.
_dotenv_state: dict[str, object] = {
    "path": None, "mtime": None, "owned": set(), "secrets": {},
}
SECRET_SUFFIXES: tuple[str, ...] = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
# KESTREL_* and OLLAMA_* names are configuration read from the environment, never credentials.
CONFIG_PREFIXES: tuple[str, ...] = ("KESTREL_", "OLLAMA_")
USE_SYSTEM_KEYS_ENV = "KESTREL_USE_SYSTEM_KEYS"


def is_secret_name(name: str) -> bool:
    return name.upper().endswith(SECRET_SUFFIXES) and not name.upper().startswith(CONFIG_PREFIXES)


def use_system_keys() -> bool:
    import os

    return os.environ.get(USE_SYSTEM_KEYS_ENV, "").strip().lower() in ("1", "true", "yes")


def get_secret(name: str | None) -> str | None:
    """The secret saved in Kestrel's `.env`; the system environment only with the opt-out."""
    import os

    if not name:
        return None
    load_dotenv_into_environ()
    secrets: dict[str, str] = _dotenv_state["secrets"]  # type: ignore[assignment]
    if secrets.get(name):
        return secrets[name]
    return (os.environ.get(name) or None) if use_system_keys() else None


def secret_saved_in_kestrel(name: str | None) -> bool:
    load_dotenv_into_environ()
    return bool(name) and bool(_dotenv_state["secrets"].get(name))  # type: ignore[union-attr]


def system_secret_ignored(name: str | None) -> bool:
    """A same-named system variable exists and Kestrel is not using it."""
    import os

    return bool(name and os.environ.get(name)) and not use_system_keys()


def load_dotenv_into_environ() -> bool:
    """Load `<data home>/.env`: secrets into the private store, everything else into os.environ,
    where a variable already set by the real environment wins and is never touched; a variable
    this loader itself set is refreshed when the file changes, so rotating a key in `.env` takes
    effect without a restart. Returns True when the file was (re)read. Cheap on the hot path:
    stat only, unless the mtime moved."""
    import os

    from dotenv import dotenv_values

    path = paths.dotenv_path()
    owned: set[str] = _dotenv_state["owned"]  # type: ignore[assignment]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        for key in owned:
            os.environ.pop(key, None)
        owned.clear()
        _dotenv_state["secrets"] = {}
        _dotenv_state.update(path=str(path), mtime=None)
        return False
    if _dotenv_state["path"] == str(path) and _dotenv_state["mtime"] == mtime:
        return False
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    # Swapped in whole, so a reader on another thread never sees a half-filled store.
    _dotenv_state["secrets"] = {k: v for k, v in values.items() if is_secret_name(k) and v}
    values = {k: v for k, v in values.items() if not is_secret_name(k)}
    for key in owned - set(values):
        os.environ.pop(key, None)
    for key, value in values.items():
        if key in owned or key not in os.environ:
            os.environ[key] = value
            owned.add(key)
    owned.intersection_update(values)
    _dotenv_state.update(path=str(path), mtime=mtime)
    return True


class HostedModeError(UserFacingError, ValueError):
    """Mode "hosted" was asked for explicitly but cannot run; str(exc) is the reason."""


# The tiers a hosted assignment must cover before mode "hosted" means anything: without both, a
# "hosted" run would score on local models while the page said hosted.
HOSTED_REQUIRED_TIERS: tuple[str, ...] = ("tier1", "tier2")


# The output budget a hosted tier gets when `llm.hosted_tiers.<tier>.max_tokens` is unset
# (D-LLM-1, SPEC.md 8.1). Under Hosted mode this is a floor under the prompt's own max_tokens,
# which is sized for the local models: a hosted reasoning model spends tokens thinking before it
# writes, and the local 300 (tier1) and 4000 (tier2) budgets quarantined every reply with
# finish_reason "length". A cap only bounds the bill; the provider charges what is written.
# Local tier budgets are untouched.
HOSTED_MAX_TOKENS: dict[str, int] = {
    "tier1": 1200, "trigger": 1200, "tier2": 8000, "tier3": 8000, "docsel": 4000,
}


HOSTED_DISCLOSURE_CHANGED = (
    "the Hosted mode disclosure changed since you accepted it (what is sent, or how much of a "
    "description); read it again and accept it under Settings > Hosted before any hosted send"
)


def hosted_mode_problem(settings: Settings) -> str | None:
    """Why mode "hosted" cannot be used right now, or None when it can. One rule for the Settings
    switch, the CLI flags, the run queue and load-time resolution, so they never disagree."""
    hm = settings.llm.hosted_mode
    if not hm.acknowledged_at:
        return "the Hosted mode disclosure has not been acknowledged under Settings > Hosted"
    if not hm.acknowledgement_current:
        return HOSTED_DISCLOSURE_CHANGED
    if not hm.enabled:
        return "Hosted mode is turned off under Settings > Hosted"
    missing = [t for t in HOSTED_REQUIRED_TIERS if t not in settings.llm.hosted_tiers]
    if missing:
        return (
            f"no hosted model is assigned for {' and '.join(missing)} "
            "(Settings > Hosted > Hosted assignments)"
        )
    return None


def apply_mode(settings: Settings, mode: str | None = None) -> Settings:
    """Resolve the effective mode and overlay `hosted_tiers` onto `tiers` when it is "hosted".

    `mode` is an explicit request (a run, a CLI flag) and wins over `settings.llm.mode`; an
    explicit "hosted" that cannot run raises HostedModeError with the reason, while a stored
    "hosted" that cannot run falls back to local (the Settings page and doctor show the reason)
    rather than failing every settings load. The overlay is tier by tier and additive: a tier
    present in hosted_tiers takes its backend and model from there, keeping the local tier's
    other fields except max_tokens, which falls to HOSTED_MAX_TOKENS when the hosted entry sets
    none (D-LLM-1); a tier absent from hosted_tiers stays exactly as it is. `settings.llm.mode`
    is left holding the EFFECTIVE mode."""
    requested = mode if mode is not None else settings.llm.mode
    if requested not in MODES:
        raise ValueError(f"unknown run mode {requested!r}; expected one of {', '.join(MODES)}")
    if requested == "hosted":
        problem = hosted_mode_problem(settings)
        if problem is not None:
            if mode is not None:
                raise HostedModeError(problem)
            settings.llm.mode = "local"
            return settings
        for tier, hosted in settings.llm.hosted_tiers.items():
            local = settings.llm.tiers.get(tier)
            overlay = hosted.model_dump(exclude_unset=True)
            if overlay.get("max_tokens") is None and tier in HOSTED_MAX_TOKENS:
                # The local budget is sized for a non-reasoning local model; a hosted model,
                # often a reasoning one, needs its own (D-LLM-1, SPEC.md 8.1).
                overlay["max_tokens"] = HOSTED_MAX_TOKENS[tier]
            settings.llm.tiers[tier] = (
                local.model_copy(update=overlay)
                if local is not None
                # No local entry (a user settings.yml that omits the tier): the hosted entry
                # still takes the hosted budget default, not None (the client default).
                else hosted.model_copy(update=overlay)
            )
    settings.llm.mode = requested  # type: ignore[assignment]
    return settings


def load_settings(*, db_overrides: dict | None = None, mode: str | None = None) -> Settings:
    """Build a fresh Settings, applying the full precedence chain, then resolve the run mode
    (apply_mode). db_overrides is the setting-table layer (highest); never carries bootstrap
    keys. `mode` overrides the stored `llm.mode` for this Settings object only."""
    load_dotenv_into_environ()
    path = paths.config_file("settings.yml")
    _yaml_path["path"] = str(path) if path else None
    return apply_mode(Settings(**(db_overrides or {})), mode)


def get_settings() -> Settings:
    """Re-read settings on every call (SPEC.md 4.6: a change takes effect with no restart)."""
    return load_settings()
