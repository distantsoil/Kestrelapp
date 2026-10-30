"""Hardware profiles for the derived num_ctx models (SPEC.md 8.2; resources/config/models.yml).

A profile names the base model per tier for one class of machine, the context window baked into
each `kestrel-<tier>` model, and the concurrency the hardware sustains. This module:

  - loads models.yml via importlib.resources into a typed Pydantic model;
  - detects the machine (GPU vendor, VRAM, RAM, Apple silicon) with subprocess calls that NEVER
    raise: anything unknown resolves to `hosted_only` with a reason string;
  - recommends a profile for detected hardware (local only on a supported GPU, D-SETUP-6);
  - says whether Ollama is needed at all for the configured tiers (`ollama_needed`);
  - renders the Modelfile for a (tier, profile) pair, so `kestrel setup` and `kestrel models build`
    build from a profile instead of hand-edited files (the shipped Modelfile.* are the default
    profile rendered to disk, and a test keeps them byte-identical);
  - records the chosen profile in <config_dir>/hardware_profile.json;
  - gives `kestrel doctor` a fit check that WARNS (never FAILs) when the configured models will not
    fit the detected memory, naming the profile to switch to.

It must not import kestrel.doctor (doctor imports this module); the report is duck-typed.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kestrel import paths

TIERS: tuple[str, ...] = ("tier1", "tier2", "tier3", "docsel")
EMBED_TIER = "embed"
# The local profile for an operator who chooses Local where Hosted is recommended.
FALLBACK_PROFILE = "cpu_8gb"
# No local model at all: every tier on a hosted provider (SPEC.md 6.4 Hosted mode). Recommended
# for any machine without a supported GPU (D-SETUP-6), offered on every machine.
HOSTED_PROFILE = "hosted_only"
# The local-default floors (SPEC.md 8.3): below these the models do not run at a usable speed.
MIN_NVIDIA_VRAM_GB = 11.0
MIN_APPLE_UNIFIED_GB = 14.0
HOSTED_ADVICE = "use a hosted provider"
# The backend name of Ollama in settings.llm.backends.
LOCAL_BACKEND = "local"
ACTIVE_PROFILE_FILENAME = "hardware_profile.json"

# Status strings matching kestrel.doctor's constants (not imported: doctor imports this module).
_PASS, _WARN, _INFO = "PASS", "WARN", "INFO"


# =================================================================================== models.yml
class TierCtx(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier1: int
    tier2: int
    tier3: int
    docsel: int

    def for_tier(self, tier: str) -> int:
        return int(getattr(self, tier))


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature: float = 0.1
    top_p: float = 0.9
    repeat_penalty: float = 1.1
    seed: int = 7
    num_predict: TierCtx


class ProfileModels(BaseModel):
    """Base tag per tier. None means the profile has no local model for that tier (hosted_only)."""

    model_config = ConfigDict(extra="forbid")
    tier1: str | None = None
    tier2: str | None = None
    tier3: str | None = None
    docsel: str | None = None
    embed: str | None = None

    def for_tier(self, tier: str) -> str | None:
        value = getattr(self, tier)
        return str(value) if value else None

    def base_tags(self) -> list[str]:
        """Unique base tags in tier order (tier bases first, embed last), for `ollama pull`."""
        out: list[str] = []
        for tier in (*TIERS, EMBED_TIER):
            tag = self.for_tier(tier)
            if tag and tag not in out:
                out.append(tag)
        return out


class Concurrency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier1: int = Field(ge=1)
    tier2: int = Field(ge=1)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    memory_gb: float
    kind: Literal["cpu", "gpu", "mac", "hosted"]
    # Which engine runs the embed tier: Ollama (`local`, with models.embed as the tag to pull) or
    # the bundled CPU model (`inprocess`, SPEC.md 8.1.1). hosted_only uses inprocess so a BYOK
    # install needs nothing local beyond Kestrel itself.
    embed_backend: Literal["local", "inprocess"] = "local"
    models: ProfileModels
    num_ctx: TierCtx
    concurrency: Concurrency
    note: str = ""

    @property
    def hosted(self) -> bool:
        """True for a profile with no local model: nothing to pull, nothing to build."""
        return self.kind == "hosted" or not self.models.base_tags()


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_profile: str
    num_ctx: TierCtx
    guardrails: Guardrails
    model_sizes_gb: dict[str, float] = Field(default_factory=dict)
    profiles: dict[str, Profile]

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles))
            raise KeyError(f"unknown hardware profile {name!r}; known: {known}") from None

    def names(self) -> list[str]:
        return list(self.profiles)


_catalog: dict[str, ModelCatalog | None] = {"value": None}


def load_catalog() -> ModelCatalog:
    """The packaged models.yml, parsed once (importlib.resources, never __file__ arithmetic)."""
    cached = _catalog["value"]
    if cached is not None:
        return cached
    raw = (paths.packaged_config() / "models.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    cat = ModelCatalog.model_validate(data)
    for name, prof in cat.profiles.items():
        needs = (*TIERS, EMBED_TIER) if prof.embed_backend == "local" else TIERS
        if not prof.hosted and any(prof.models.for_tier(t) is None for t in needs):
            raise ValueError(f"models.yml profile {name}: a local profile must name every tier")
        if prof.embed_backend == "inprocess" and prof.models.embed is not None:
            raise ValueError(
                f"models.yml profile {name}: embed_backend inprocess takes no models.embed tag "
                "(the in-process model is bundled, not pulled)"
            )
        for tier in TIERS:
            if prof.num_ctx.for_tier(tier) < cat.num_ctx.for_tier(tier):
                raise ValueError(
                    f"models.yml profile {name}: num_ctx.{tier} {prof.num_ctx.for_tier(tier)} is "
                    f"below the shipped floor {cat.num_ctx.for_tier(tier)} (SPEC.md 8.2)"
                )
    if cat.default_profile not in cat.profiles:
        raise ValueError(f"models.yml default_profile {cat.default_profile!r} is not a profile")
    _catalog["value"] = cat
    return cat


def embed_route(profile: str, catalog: ModelCatalog | None = None) -> tuple[str, str] | None:
    """(backend, model) the embed tier should use under `profile`, or None to keep whatever
    llm.tiers.embed already says (the shipped settings.yml: Ollama nomic-embed-text). Only the
    in-process route is ever written: a local profile's embed tag is what settings.yml ships, and
    rewriting it would override an operator's own embed setting. A later move from BYOK to a GPU
    profile keeps the in-process engine (it runs on any machine); the doctor says so and names
    the setting that switches it (D-EMBED-3 review)."""
    cat = catalog or load_catalog()
    prof = cat.profile(profile)
    if prof.embed_backend == "inprocess":
        from kestrel.llm.inprocess_embed import BUNDLED

        return "inprocess", BUNDLED.model_id
    return None


# ============================================================================ hardware detection
class Hardware(BaseModel):
    """What was detected. Every field may be unknown; detection never raises."""

    model_config = ConfigDict(frozen=True)
    os: Literal["windows", "macos", "linux", "unknown"]
    gpu_vendor: Literal["nvidia", "apple"] | None = None
    vram_gb: float | None = None
    ram_gb: float | None = None
    apple_silicon: bool = False
    notes: tuple[str, ...] = ()

    def summary(self) -> str:
        bits = [self.os]
        if self.gpu_vendor == "nvidia" and self.vram_gb is not None:
            bits.append(f"NVIDIA GPU {self.vram_gb:g} GB VRAM")
        elif self.apple_silicon:
            bits.append("Apple silicon (unified memory)")
        else:
            bits.append("no supported GPU detected")
        bits.append(f"{self.ram_gb:g} GB RAM" if self.ram_gb is not None else "RAM unknown")
        return ", ".join(bits)


def no_console_window() -> dict:
    """Keyword arguments that stop a child process from opening a console window. A windowed
    PyInstaller app has no console, so Windows gives each console child (nvidia-smi,
    PowerShell) a fresh one: the empty black window the operator saw flash at launch
    (D-UI-27). Empty off Windows."""
    import sys

    if sys.platform.startswith("win"):
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def system_tool_env() -> dict:
    """Keyword arguments (`env=`) for starting a SYSTEM program from the app (notify-send, paplay,
    aplay): the environment of the person's session, not of the frozen app. A PyInstaller build on
    Linux points LD_LIBRARY_PATH at its own bundled libraries (the original is kept in
    LD_LIBRARY_PATH_ORIG), and a system program that loads those instead of its own can fail.
    Empty everywhere else: nothing changes when running from source, on Windows or on macOS."""
    import sys

    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return {}
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return {"env": env}


def _run(cmd: list[str], timeout: float = 10.0) -> str | None:
    """Run a detection command; None on any failure (missing binary, non-zero exit, timeout)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
            **no_console_window(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _os_name() -> Literal["windows", "macos", "linux", "unknown"]:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "unknown"


def _nvidia_vram_gb() -> float | None:
    out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    if not out:
        return None
    sizes = []
    for line in out.splitlines():
        token = line.strip().split(",")[0].strip()
        if token.replace(".", "", 1).isdigit():
            sizes.append(float(token))
    if not sizes:
        return None
    # The largest single card: Ollama loads a model onto one GPU before it splits across several.
    return round(max(sizes) / 1024, 1)


def _ram_gb(os_name: str) -> float | None:
    bytes_total: float | None = None
    if os_name == "macos":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.strip().isdigit():
            bytes_total = float(out.strip())
    elif os_name == "linux":
        text = _read_text("/proc/meminfo")
        m = re.search(r"^MemTotal:\s+(\d+)\s*kB", text or "", re.MULTILINE)
        if m:
            bytes_total = float(m.group(1)) * 1024
    elif os_name == "windows":
        out = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
            ]
        )
        if not (out and out.strip().isdigit()):
            out = _run(["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"])
            if out:
                digits = [t for t in out.split() if t.isdigit()]
                out = digits[0] if digits else None
        if out and out.strip().isdigit():
            bytes_total = float(out.strip())
    if bytes_total is None and hasattr(os, "sysconf"):
        try:
            bytes_total = float(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError, AttributeError):
            bytes_total = None
    if not bytes_total:
        return None
    return round(bytes_total / 1024**3, 1)


def detect_hardware() -> Hardware:
    """Best-effort detection via subprocess calls (mocked in tests). Never raises."""
    notes: list[str] = []
    os_name = _os_name()
    if os_name == "unknown":
        notes.append(f"unrecognized platform {platform.system()!r}")
    ram = _ram_gb(os_name)
    if ram is None:
        notes.append("could not read total RAM")
    apple = False
    if os_name == "macos":
        arch = (_run(["uname", "-m"]) or platform.machine() or "").strip().lower()
        apple = arch in ("arm64", "aarch64")
        if not apple:
            notes.append("Intel Mac: Ollama runs on the CPU")
    vram = None if apple else _nvidia_vram_gb()
    if apple:
        return Hardware(
            os=os_name,
            gpu_vendor="apple",
            vram_gb=ram,
            ram_gb=ram,
            apple_silicon=True,
            notes=tuple(notes),
        )
    if vram is None:
        notes.append("nvidia-smi not found or reported no GPU")
    return Hardware(
        os=os_name,
        gpu_vendor="nvidia" if vram is not None else None,
        vram_gb=vram,
        ram_gb=ram,
        apple_silicon=False,
        notes=tuple(notes),
    )


def recommend_profile(hw: Hardware) -> tuple[str, str]:
    """(profile name, one-line reason). Never an error.

    A local profile is recommended only where Kestrel's models run at a usable speed: an NVIDIA
    GPU with at least `MIN_NVIDIA_VRAM_GB` of VRAM, or Apple silicon with at least
    `MIN_APPLE_UNIFIED_GB` of unified memory. Everything else gets `hosted_only` (D-SETUP-6):
    a smaller card, integrated graphics, CPU only, an AMD or Intel GPU (not detected; Ollama's
    ROCm and Vulkan paths are not verified here), or nothing detected at all. A tier-2 evaluation
    on a CPU takes minutes per job, so RAM alone never earns a local default. Every local profile
    stays available to choose (`kestrel setup --profile`, the Setup page); this is only the
    default, and hosted uses no local memory at all."""
    if hw.apple_silicon:
        ram = hw.ram_gb or 0.0
        if ram >= 60:
            return "mac_64gb", f"Apple silicon with {ram:g} GB unified memory"
        if ram >= 30:
            return "mac_32gb", f"Apple silicon with {ram:g} GB unified memory"
        if ram >= MIN_APPLE_UNIFIED_GB:
            return "mac_16gb", f"Apple silicon with {ram:g} GB unified memory"
        if hw.ram_gb is None:
            return HOSTED_PROFILE, "Apple silicon with unknown unified memory: " + HOSTED_ADVICE
        return (
            HOSTED_PROFILE,
            f"Apple silicon with {ram:g} GB unified memory: too little to run the models "
            "alongside the OS; " + HOSTED_ADVICE,
        )
    if hw.gpu_vendor == "nvidia" and hw.vram_gb is not None:
        vram = hw.vram_gb
        if vram >= 22:
            return "gpu_24gb", f"NVIDIA GPU with {vram:g} GB VRAM"
        if vram >= 15:
            return "gpu_16gb", f"NVIDIA GPU with {vram:g} GB VRAM"
        if vram >= MIN_NVIDIA_VRAM_GB:
            return "gpu_12gb", f"NVIDIA GPU with {vram:g} GB VRAM"
        return (
            HOSTED_PROFILE,
            f"NVIDIA GPU with {vram:g} GB VRAM: too little to hold the evaluation model; "
            + HOSTED_ADVICE,
        )
    if hw.ram_gb is None:
        return HOSTED_PROFILE, "no supported GPU detected and memory unknown; " + HOSTED_ADVICE
    return (
        HOSTED_PROFILE,
        f"no supported GPU detected ({hw.ram_gb:g} GB RAM): on the CPU an evaluation takes "
        "minutes per job; " + HOSTED_ADVICE,
    )


def ollama_needed(settings: Any) -> bool:
    """True iff any configured tier in `settings.llm.tiers` (the effective set, after the
    Local / Hosted overlay) runs on the Ollama backend, named `local`. Doctor, the Setup page,
    the Today page and the status rail use it to stop reporting Ollama as missing on a machine
    that does not use it (D-SETUP-8). No tier is special-cased: an embed tier on `local` needs
    Ollama like any other, and one moved to another backend does not.

    "Local" means any local model SERVER: the `local` backend, or any other backend that is not
    external and is reached over HTTP (an operator-added oMLX or vLLM server), so the num_ctx and
    needle checks are never skipped for a server that is not named `local`. An in-process engine
    (no BackendConfig, or a non-HTTP address) needs no server."""
    backends = settings.llm.backends
    for t in settings.llm.tiers.values():
        if t.backend == LOCAL_BACKEND:
            return True
        b = backends.get(t.backend)
        if (b is not None and not b.external and getattr(b, "engine", "http") == "http"
                and (b.base_url or "").lower().startswith("http")):
            return True
    return False


# ============================================================================ Modelfile rendering
_ROLES = {
    "tier1": "fast triage and trigger classification",
    "tier2": "deep six-block evaluation",
    "tier3": "escalation with research (own model, the decision log)",
    "docsel": "document slot selection",
}


def render_modelfile(tier: str, profile: str, catalog: ModelCatalog | None = None) -> str:
    """The Modelfile for `kestrel-<tier>` under a profile: FROM <base> plus num_ctx and guardrails.
    Deterministic text so the shipped reference files can be diffed against it."""
    if tier not in TIERS:
        raise KeyError(f"unknown tier {tier!r}; derived models exist for {', '.join(TIERS)}")
    cat = catalog or load_catalog()
    prof = cat.profile(profile)
    base = prof.models.for_tier(tier)
    if base is None:
        raise ValueError(
            f"profile {profile!r} has no local model for {tier}; it runs on a hosted provider "
            "(nothing to build)"
        )
    g = cat.guardrails
    lines = [
        f"# kestrel-{tier} ({profile}): {_ROLES[tier]}. SPEC.md 8.2.",
        "# Generated by kestrel.llm.profiles.render_modelfile from resources/config/models.yml;",
        "# do not hand-edit. Ollama's /v1 endpoint cannot set num_ctx per request, so it is baked",
        "# in here or long inputs are silently truncated. A request that sets temperature or",
        "# max_tokens wins over the PARAMETER lines below; they are the floor for one that does",
        "# not.",
        f"FROM {base}",
        f"PARAMETER num_ctx {prof.num_ctx.for_tier(tier)}",
        f"PARAMETER num_predict {g.num_predict.for_tier(tier)}",
        f"PARAMETER temperature {g.temperature:g}",
        f"PARAMETER top_p {g.top_p:g}",
        f"PARAMETER repeat_penalty {g.repeat_penalty:g}",
        f"PARAMETER seed {g.seed}",
    ]
    return "\n".join(lines) + "\n"


def derived_name(tier: str) -> str:
    return f"kestrel-{tier}"


def write_modelfiles(profile: str, dest: Path, catalog: ModelCatalog | None = None) -> list[Path]:
    """Render every tier's Modelfile for a profile into `dest` (Modelfile.<tier>)."""
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for tier in TIERS:
        p = dest / f"Modelfile.{tier}"
        p.write_text(render_modelfile(tier, profile, catalog), encoding="utf-8")
        written.append(p)
    return written


def shipped_modelfile(tier: str) -> str:
    """The packaged reference Modelfile text (the default profile rendered to disk)."""
    return (paths.packaged_models() / f"Modelfile.{tier}").read_text(encoding="utf-8")


# ============================================================================== active profile
class ActiveProfile(BaseModel):
    """What `kestrel setup` chose, persisted as <config_dir>/hardware_profile.json so doctor and
    the Settings page can show it. A JSON file, not a setting-table row, because setup runs before
    the database exists and the file is readable without a migration."""

    profile: str
    reason: str = ""
    chosen_at: str = ""
    hardware: dict[str, Any] = Field(default_factory=dict)


def active_profile_path() -> Path:
    return paths.config_dir() / ACTIVE_PROFILE_FILENAME


def save_active_profile(name: str, reason: str, hw: Hardware | None = None) -> Path:
    paths.ensure_dirs()
    rec = ActiveProfile(
        profile=name,
        reason=reason,
        chosen_at=datetime.now(UTC).isoformat(timespec="seconds"),
        hardware=hw.model_dump() if hw else {},
    )
    p = active_profile_path()
    p.write_text(json.dumps(rec.model_dump(), indent=2), encoding="utf-8")
    return p


def load_active_profile() -> ActiveProfile | None:
    p = active_profile_path()
    try:
        return ActiveProfile.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


# ================================================================================ doctor fit check
_BYTES_PER_PARAM = {
    "Q2": 0.45,
    "Q3": 0.5,
    "Q4": 0.6,
    "Q5": 0.72,
    "Q6": 0.85,
    "Q8": 1.07,
    "F16": 2.0,
    "BF16": 2.0,
    "F32": 4.0,
}


def _parse_param_count(text: str) -> float | None:
    """'14.8B' -> 14.8 (billions); '137M' -> 0.137."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([BMK])\s*", text or "", re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1))
    return {"B": n, "M": n / 1e3, "K": n / 1e6}[m.group(2).upper()]


def estimate_size_gb(details: dict[str, Any]) -> float | None:
    """Weight size from /api/show `details` (parameter_size + quantization_level)."""
    params = _parse_param_count(str(details.get("parameter_size", "")))
    if params is None:
        return None
    quant = str(details.get("quantization_level", "Q4")).upper()
    per = next((v for k, v in _BYTES_PER_PARAM.items() if quant.startswith(k)), 0.6)
    return round(params * per, 1)


def memory_budget_gb(hw: Hardware) -> tuple[float | None, str]:
    """(usable GB for model weights, what it is). GPU: VRAM. Apple: ~3/4 of unified memory (what
    macOS lets the GPU address). CPU: ~3/4 of RAM. None when nothing was detected."""
    if hw.apple_silicon and hw.ram_gb:
        return round(hw.ram_gb * 0.75, 1), "unified memory available to the GPU"
    if hw.gpu_vendor == "nvidia" and hw.vram_gb:
        return hw.vram_gb, "VRAM"
    if hw.ram_gb:
        return round(hw.ram_gb * 0.75, 1), "system RAM (CPU inference)"
    return None, "unknown"


def _tier_for_model(settings: Any, model: str) -> str | None:
    for tier in TIERS:
        if settings.llm.tiers[tier].model == model:
            return tier
    return None


def check_model_fit(rep: Any, settings: Any, client: Any) -> None:
    """doctor check: do the configured local models fit the detected memory? WARN, never FAIL.

    Size comes from /api/show details.parameter_size when Ollama answers, else from models.yml's
    size table for the active (or default) profile's base. Each derived model needs its weights
    plus KV-cache headroom (~20% + 1 GB) inside the budget; the largest tier is what decides."""
    cat = load_catalog()
    hw = detect_hardware()
    budget, what = memory_budget_gb(hw)
    active = load_active_profile()
    profile_name = active.profile if active else cat.default_profile
    try:
        prof = cat.profile(profile_name)
    except KeyError:
        prof = cat.profile(cat.default_profile)
    recommended, why = recommend_profile(hw)
    if budget is None:
        rep.add(
            "Model fit",
            _INFO,
            f"profile {profile_name}; hardware not detected ({'; '.join(hw.notes) or 'unknown'})",
        )
        return
    models: dict[str, str] = {}
    for tier in TIERS:
        t = settings.llm.tiers[tier]
        if settings.llm.backends[t.backend].external:
            continue
        models.setdefault(t.model, tier)
    if not models:
        rep.add("Model fit", _INFO, f"profile {profile_name}: every tier is hosted; nothing to fit")
        return
    worst: tuple[float, str] | None = None
    unsized: list[str] = []
    for model, tier in models.items():
        size: float | None = None
        try:
            size = estimate_size_gb(client.show(model).get("details") or {})
        except Exception:  # Ollama down or model not built: fall back to the profile table
            size = None
        if size is None:
            base = prof.models.for_tier(tier)
            size = cat.model_sizes_gb.get(base) if base else None
        if size is None:
            unsized.append(model)
            continue
        if worst is None or size > worst[0]:
            worst = (size, model)
    if worst is None:
        rep.add("Model fit", _INFO, "no model sizes available; " + ", ".join(unsized))
        return
    size, model = worst
    need = round(size * 1.2 + 1.0, 1)
    detail = f"{model} ~{size:g} GB weights, needs ~{need:g} GB; {budget:g} GB {what}"
    if need <= budget:
        rep.add("Model fit", _PASS, f"profile {profile_name}: {detail}")
        return
    switch = (
        f"switch to profile `{recommended}` ({why}): run `kestrel setup --profile {recommended}`"
        if recommended != profile_name
        else "pick a smaller base model in models.yml and rebuild (`kestrel setup`)"
    )
    rep.add(
        "Model fit",
        _WARN,
        f"profile {profile_name}: {detail}; it will not fit and will spill to CPU or fail to load",
        switch,
    )
