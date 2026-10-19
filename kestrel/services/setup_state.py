"""State for Settings > System and the onboarding model step (D-UI-31; formerly the Setup page,
docs/UI_PROPOSAL.html Screen 5): `kestrel doctor` with buttons.

Two halves. `assess()` is read-only: given the resolved Settings and the raw `llm.mode` and
`setup.run_choice` rows, it computes every step's state (run mode choice, detected hardware,
Ollama reachable, hardware profile, base models present, derived models present, verified, hosted
key and acknowledgement, profile.yml present) without writing anything. It probes Ollama with a
short timeout and nothing else; the caller runs it in a thread.

App first (release round R1): every step a user must take has a button. Downloading the models
and building the derived ones are task rows run through Ollama's HTTP API
(kestrel.services.model_tasks), resumable and cancellable. Verify (`kestrel doctor`) still runs in
a background thread with a line log, in a module-level registry, one at a time. Installing Ollama
is not an action: it is a system change, so the Ollama step offers a Download Ollama link to the
official page and a Check again button. The exact terminal commands stay on each step for power
users, shown only under Show details. `set_run_mode` writes the setting rows; the hosted key and
the disclosure stay on Settings > Hosted (D-UI-3).

Hard rule 1 holds: every action acts only on the Ollama at the configured address (Ollama's
pull and create, `kestrel doctor`). That is this computer by default; when the address names
another host, the steps say "Ollama at <host>" and the buttons "Download on <host>" and "Build
on <host>", so nobody believes the models are landing on this computer (release round review,
finding 9).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import paths
from kestrel import setup as setup_cli
from kestrel.config import Settings, hosted_mode_problem
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.llm.client import OllamaClient

log = structlog.get_logger()

CHOICES: tuple[str, ...] = ("local", "hosted", "both")
CHOICE_KEY = "setup.run_choice"
MODE_KEY = "llm.mode"
OLLAMA_TIMEOUT_S = 2.0
OLLAMA_CONNECT_TIMEOUT_S = 0.5  # loopback: a live Ollama accepts in ms; Windows refuses in ~2 s
PROBE_TTL_UP_S = 5.0
PROBE_TTL_DOWN_S = 15.0
REPORT_FILENAME = "doctor_report.json"
SKIPPED_HOSTED = "skipped: Hosted"
LOCAL_FALLBACK_REASON = (
    "this machine has no supported GPU: expect slow local runs; Hosted is recommended"
)
# Shown above the steps when Hosted is recommended for a hardware reason and the operator chose
# Local or Both (D-SETUP-7). Leads with the decision; the reason line follows it on the page.
LOW_SPEC_WARNING = (
    "This machine can't run Kestrel's models at a usable speed. Use a hosted provider (bring "
    "your own key). Local mode stays available, but expect slow runs and partial results."
)
# No GPU reported is also what a failed nvidia-smi looks like on a capable machine.
NO_GPU_NOTE = "If this machine has an NVIDIA GPU, detection failed: choose its profile instead."
# When detection could not tell (memory unknown, or no GPU reported): say so rather than claim
# the machine is too small. A failed nvidia-smi on a capable machine lands here too.
UNKNOWN_HW_WARNING = (
    "Kestrel could not confirm this machine can run its models. If it has an NVIDIA GPU with "
    "12 GB or more, or Apple silicon with 16 GB or more, choose its profile; otherwise use a "
    "hosted provider (bring your own key)."
)
# The doctor rows that say the derived models really work (SPEC.md 8.2, CLAUDE.md traps).
VERIFY_ROWS: tuple[str, ...] = ("num_ctx", "needle", "thinking off", "schema honored")
# Which step an action's log belongs under.
ACTION_STEP: dict[str, str] = {"pull": "pull", "build": "build", "verify": "build"}
MAX_LOG_LINES = 400
# The official download page per OS; the Ollama step links it (opened in the system browser).
OLLAMA_DOWNLOAD: dict[str, str] = {
    "windows": f"{setup_cli.DOWNLOAD_URL}/windows",
    "macos": f"{setup_cli.DOWNLOAD_URL}/mac",
    "linux": f"{setup_cli.DOWNLOAD_URL}/linux",
}


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def ollama_remote_host(url: str | None) -> str | None:
    """The host name when the Ollama address is NOT this computer (localhost, 127.0.0.1, ::1),
    else None. Downloads and builds then run on that host, and the page says so."""
    from urllib.parse import urlsplit

    if not url:
        return None
    try:
        host = (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
    except ValueError:
        return None
    if host in _LOCAL_HOSTS or host.startswith("127."):
        return None
    return host


def ollama_download_url(os_name: str | None) -> str:
    return OLLAMA_DOWNLOAD.get(os_name or "", setup_cli.DOWNLOAD_URL)


# ======================================================================================= state
@dataclass
class StepState:
    key: str
    number: int
    title: str
    status: str  # ok | now | todo | warn | skip
    detail: str = ""  # the quiet line beside the title
    explanation: str = ""
    # The exact terminal commands for the same step, for power users: shown only under the
    # step's Show details toggle, never as the instruction (app first, release round R1).
    commands: list[str] = field(default_factory=list)
    action: str | None = None  # pull | build | verify: a button the page can run
    action_label: str = ""
    action_enabled: bool = True
    link: str | None = None
    link_label: str = ""
    link_external: bool = False  # opens in the system browser (the Download Ollama link)
    pill: str = ""  # a label instead of a button (ok, optional, skipped: Hosted)
    pill_class: str = "grey"


@dataclass
class SetupState:
    choice: str  # local | hosted | both
    choice_explicit: bool  # a setup.run_choice row exists
    hardware: profiles.Hardware
    hardware_line: str
    recommended: str
    recommended_reason: str
    ollama_version: str | None
    ollama_url: str
    profile: str
    profile_recorded: bool
    profile_reason: str
    base_tags: list[str]
    missing_tags: list[str]
    derived_names: list[str]
    missing_derived: list[str]
    verified: str  # ok | failed | none
    verified_detail: str
    hosted_keys: dict[str, bool]  # backend name -> key present
    hosted_acknowledged: bool
    hosted_problem: str | None
    profile_yml: bool
    first_run: bool
    steps: list[StepState] = field(default_factory=list)
    low_spec_warning: str | None = None  # LOW_SPEC_WARNING when it applies, else None

    @property
    def local_needed(self) -> bool:
        return self.choice != "hosted"

    @property
    def hosted_needed(self) -> bool:
        return self.choice != "local"


# ---------------------------------------------------------------------------- cached hardware
_hw_cache: dict[str, profiles.Hardware | None] = {"value": None}


def detected_hardware() -> profiles.Hardware:
    """Detection shells out to nvidia-smi and friends, so it runs once per process."""
    hw = _hw_cache["value"]
    if hw is None:
        hw = profiles.detect_hardware()
        _hw_cache["value"] = hw
    return hw


def forget_hardware() -> None:
    _hw_cache["value"] = None


# ----------------------------------------------------------------------------- ollama probe
def ollama_client(settings: Settings) -> OllamaClient:
    local = settings.llm.backends.get("local")
    base = local.base_url if local else ""
    key = (local.api_key if local else None) or "ollama"
    return OllamaClient(base, settings.llm.ollama_native_url, key)


_probe_cache: dict[str, Any] = {"at": 0.0, "url": "", "result": None}


def forget_ollama_probe() -> None:
    """Drop the cached probe. Process-global, so the test suite calls this between tests."""
    _probe_cache.update(at=0.0, url="", result=None)


def probe_ollama(settings: Settings, *, fresh: bool = False) -> tuple[str | None, list[str]]:
    """(version or None when nothing answers, the tags present), cached for a few seconds.

    On Windows a refused loopback connect is not instant: the stack retries the SYN and gives up
    after about two seconds. The Setup page, the Today page and their polls all probe, so with
    Ollama stopped every one of them paid those two seconds (the 0.2.0 click delay, D-UI-15).
    The connect timeout is therefore short (a running Ollama on loopback accepts in
    milliseconds) and the answer is cached, the down answer longer than the up one. Actions that
    need the truth now (pull, build) pass fresh=True."""
    url = settings.llm.ollama_native_url
    now = time.monotonic()
    cached = _probe_cache["result"]
    if not fresh and cached is not None and _probe_cache["url"] == url:
        ttl = PROBE_TTL_UP_S if cached[0] is not None else PROBE_TTL_DOWN_S
        if now - float(_probe_cache["at"]) < ttl:
            return cached
    client = ollama_client(settings)
    timeout = httpx.Timeout(OLLAMA_TIMEOUT_S, connect=OLLAMA_CONNECT_TIMEOUT_S)
    try:
        version = str(client.version(timeout=timeout) or "unknown")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        log.debug("setup_ollama_unreachable", error=str(exc))
        result: tuple[str | None, list[str]] = (None, [])
    else:
        try:
            tags = [str(t) for t in client.list_models(timeout=OLLAMA_TIMEOUT_S * 5)]
        except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as exc:
            log.debug("setup_ollama_tags_failed", error=str(exc))
            tags = []
        result = (version, tags)
    _probe_cache.update(at=time.monotonic(), url=url, result=result)
    return result


# --------------------------------------------------------------------------- doctor report
def report_path() -> Path:
    return paths.data_dir() / REPORT_FILENAME


def save_report(rep: Any) -> Path:
    """Persist a DoctorReport so the next assess() can say "verified" without re-probing."""
    paths.ensure_dirs()
    data = {
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "blocking": bool(rep.has_blocking_failure),
        "results": [asdict(r) for r in rep.results],
    }
    p = report_path()
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def load_report() -> dict[str, Any] | None:
    try:
        data = json.loads(report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("results"), list) else None


def _verification(report: dict[str, Any] | None) -> tuple[str, str]:
    """(ok | failed | none, detail) from the persisted doctor report's model rows."""
    if report is None:
        return "none", "not verified yet"
    rows = {
        str(r.get("name")): r for r in report["results"] if isinstance(r, dict) and r.get("name")
    }
    probed = [rows[n] for n in VERIFY_ROWS if n in rows]
    failed = [r for r in probed if r.get("status") == "FAIL"]
    when = str(report.get("ran_at", ""))[:16].replace("T", " ")
    if failed:
        first = failed[0]
        return "failed", f"{first.get('name')} failed on {when}: {first.get('detail', '')}"
    if not probed or all(r.get("status") == "INFO" for r in probed):
        return "none", "not verified yet"
    return "ok", f"verified {when}"


# -------------------------------------------------------------------------- run mode choice
def run_choice(settings: Settings, rows: dict[str, Any]) -> tuple[str, bool]:
    """The Local / Hosted / Both card. A stored choice wins; otherwise it is derived from the
    stored `llm.mode` (the effective mode in Settings falls back to local when hosted cannot run,
    so the row is read, not the resolved value) and whether hosted is configured."""
    stored = rows.get(CHOICE_KEY)
    if stored in CHOICES:
        return str(stored), True
    mode = rows.get(MODE_KEY) or settings.llm.mode
    if mode == "hosted":
        return "hosted", False
    configured = bool(settings.llm.hosted_mode.acknowledged_at) and any(
        bool(b.api_key) or prov.secret_present(b.api_key_env)
        for b in prov.external_backends(settings).values()
    )
    return ("both" if configured else "local"), False


def is_first_run(settings: Settings, rows: dict[str, Any] | None = None) -> bool:
    """No hardware profile recorded, no run mode chosen, no hosted acknowledgement: the app has
    never been set up, so the dashboard shows the Finish setup banner."""
    rows = rows or {}
    if profiles.load_active_profile() is not None:
        return False
    if MODE_KEY in rows or CHOICE_KEY in rows:
        return False
    return not settings.llm.hosted_mode.acknowledged_at


# ------------------------------------------------------------------------------------ assess
def assess(settings: Settings, rows: dict[str, Any] | None = None) -> SetupState:
    """Every step's state, read-only. `rows` is the raw setting table for MODE_KEY and
    CHOICE_KEY (the caller reads them; this function touches no database). The only network
    call is the short Ollama probe, skipped under the Hosted choice; the caller runs this in a
    thread."""
    rows = rows or {}
    cat = profiles.load_catalog()
    hw = detected_hardware()
    recommended, reason = profiles.recommend_profile(hw)
    choice, explicit = run_choice(settings, rows)
    # Hosted only needs nothing from Ollama, and on a machine without it the probe cost every
    # onboarding and Settings render up to a second waiting on a refused connect (D-UI-40).
    version, tags = (None, []) if choice == "hosted" else probe_ollama(settings)
    active = profiles.load_active_profile()
    if active is not None and active.profile in cat.profiles:
        profile, recorded, profile_reason = active.profile, True, active.reason
    elif choice != "hosted" and cat.profile(recommended).hosted:
        # The operator chose to run locally on a machine where Hosted is the recommendation.
        # The hosted profile has nothing to pull or build, so the page would drive a local run
        # into "Pull 0 models"; the CPU profile is the local path that exists, with the caveat.
        profile, recorded, profile_reason = profiles.FALLBACK_PROFILE, False, LOCAL_FALLBACK_REASON
    else:
        profile, recorded, profile_reason = recommended, False, reason
    prof = cat.profile(profile)
    base_tags = prof.models.base_tags()
    missing_tags = [t for t in base_tags if not setup_cli._tag_present(t, tags)]
    derived = [profiles.derived_name(t) for t in profiles.TIERS]
    missing_derived = (
        [] if prof.hosted else [n for n in derived if not setup_cli._tag_present(n, tags)]
    )
    verified, verified_detail = _verification(load_report())
    hosted_keys = {
        name: bool(b.api_key) or prov.secret_present(b.api_key_env)
        for name, b in prov.external_backends(settings).items()
    }
    state = SetupState(
        choice=choice,
        choice_explicit=explicit,
        hardware=hw,
        hardware_line=hw.summary(),
        recommended=recommended,
        recommended_reason=reason,
        ollama_version=version,
        ollama_url=settings.llm.ollama_native_url,
        profile=profile,
        profile_recorded=recorded,
        profile_reason=profile_reason,
        base_tags=base_tags,
        missing_tags=missing_tags,
        derived_names=derived,
        missing_derived=missing_derived,
        verified=verified,
        verified_detail=verified_detail,
        hosted_keys=hosted_keys,
        hosted_acknowledged=bool(settings.llm.hosted_mode.acknowledged_at),
        hosted_problem=hosted_mode_problem(settings),
        profile_yml=(paths.config_dir() / "profile.yml").exists(),
        first_run=is_first_run(settings, rows),
        low_spec_warning=low_spec_warning(
            cat, recommended, choice, hw=hw,
            recorded_local=bool(recorded and not cat.profile(profile).hosted),
        ),
    )
    state.steps = build_steps(state, cat)
    return state


def low_spec_warning(
    cat: profiles.ModelCatalog, recommended: str, choice: str, *,
    hw: profiles.Hardware | None = None, recorded_local: bool = False,
) -> str | None:
    """The Setup page warning: Hosted is the recommendation (which recommend_profile makes only
    for a hardware reason) and the operator's choice runs local models (Local or Both). Not
    shown when the operator already recorded a local profile (an upgrade, or a deliberate pick
    on hardware Kestrel does not detect, such as an AMD card). Uncertain detection gets its own
    wording, never the claim that the machine is too small."""
    if choice == "hosted" or recorded_local or not cat.profile(recommended).hosted:
        return None
    if hw is not None and hw.ram_gb is None:
        return UNKNOWN_HW_WARNING
    if hw is not None and hw.gpu_vendor is None and not hw.apple_silicon:
        return LOW_SPEC_WARNING + " " + NO_GPU_NOTE
    return LOW_SPEC_WARNING


def _models_line(prof: profiles.Profile) -> str:
    bits = []
    for tier, label in (
        ("tier1", "Tier 1"),
        ("tier2", "Tier 2"),
        ("docsel", "document selection"),
        ("embed", "embeddings"),
    ):
        tag = prof.models.for_tier(tier)
        if tag:
            bits.append(f"{label} {tag}")
    return " · ".join(bits)


def _join_sentences(*parts: str) -> str:
    """Sentences joined with ". "; an empty part (a profile with no local models) adds nothing,
    so the text never starts with a stray period."""
    return " ".join(p.rstrip(". ") + "." for p in parts if p.strip())


def build_steps(st: SetupState, cat: profiles.ModelCatalog) -> list[StepState]:
    """Steps 2 to 6 of Settings > System (step 1, the run mode, is the card row above them).
    Local steps are "skipped: Hosted" under the Hosted choice; the hosted step is optional under
    Local. Who the operator is belongs to the onboarding wizard and the Profile page (D-UI-31),
    so there is no profile step here."""
    prof = cat.profile(st.profile)
    host = ollama_remote_host(st.ollama_url)
    where = host or "this computer"
    install = setup_cli.INSTALL_COMMANDS.get(st.hardware.os)
    local = st.local_needed
    ollama_ok = st.ollama_version is not None
    steps: list[StepState] = []

    # 2. Ollama
    if not local:
        steps.append(_skipped(2, "ollama", "Ollama is running"))
    elif ollama_ok:
        steps.append(
            StepState(
                "ollama", 2, f"Ollama at {host}" if host else "Ollama is running", "ok",
                detail=f"version {st.ollama_version} at {st.ollama_url}",
                explanation=f"Ollama runs the AI models on {where}. Kestrel checks for it "
                "and never installs it for you.",
                pill="ok", pill_class="apply",
            )
        )
    else:
        cmds = [install] if install else []
        cmds.append("ollama serve")
        not_running = (
            f"Ollama at {host} is the free program that runs the AI models there. Make sure "
            f"it is open on {host} and reachable from this computer, then press Check again. "
            "Kestrel never installs software for you."
            if host else
            "Ollama is the free program that runs the AI models on this "
            "computer. Press Download Ollama, install it like any other program, open it, "
            "then press Check again. Kestrel never installs software for you."
        )
        steps.append(
            StepState(
                "ollama", 2, f"Ollama at {host} is not answering" if host
                else "Ollama is not running", "warn",
                detail=f"nothing answers at {st.ollama_url}",
                explanation=not_running,
                commands=cmds,
                action="check", action_label="Check again",
                # Installing on another host is that host's business, not this computer's.
                link=None if host else ollama_download_url(st.hardware.os),
                link_label="" if host else "Download Ollama",
                link_external=not host,
                pill="needs you", pill_class="review",
            )
        )

    # 3. Hardware profile
    if not local:
        steps.append(_skipped(3, "profile", f"Hardware profile: {prof.description}"))
    else:
        steps.append(
            StepState(
                "profile", 3, f"Hardware profile: {prof.description}",
                "ok" if st.profile_recorded else "todo",
                detail=prof.description,
                explanation=_join_sentences(
                    _models_line(prof),
                    f"Recorded: {st.profile_reason}."
                    if st.profile_recorded
                    else f"Recommended because {st.profile_reason}; it is recorded when the "
                    "derived models are built, or pick another below.",
                ),
                pill="recorded" if st.profile_recorded else "",
                pill_class="apply",
            )
        )

    # 4. Pull the base models
    n_total = len(st.base_tags)
    n_missing = len(st.missing_tags)
    if not local:
        steps.append(_skipped(4, "pull", "Pull the base models"))
    elif n_missing == 0 and ollama_ok:
        steps.append(
            StepState(
                "pull", 4, "Pull the base models", "ok",
                detail=f"{n_total} of {n_total} present",
                explanation="Every base model this profile needs is in Ollama.",
                pill="ok", pill_class="apply",
            )
        )
    else:
        present = n_total - n_missing if ollama_ok else 0
        sizes = cat.model_sizes_gb
        gb = sum(sizes.get(t, 0.0) for t in st.missing_tags)
        about = f"About {gb:g} GB to download once. " if gb else ""
        steps.append(
            StepState(
                "pull", 4, "Pull the base models", "todo",
                detail=f"{present} of {n_total} present" if ollama_ok else "needs Ollama",
                explanation=f"{about}Press the button and Kestrel asks Ollama"
                f"{' at ' + host if host else ''} to download each one"
                f"{' there' if host else ''}, with progress here. You can close Kestrel: the "
                "download picks up where it stopped the next time Kestrel opens.",
                commands=[f"ollama pull {t}" for t in st.missing_tags],
                action="pull",
                action_label=(
                    (f"Download on {host}" if host
                     else f"Pull {n_missing} model{'s' if n_missing != 1 else ''}")
                    if n_missing
                    else "Nothing to pull"
                ),
                action_enabled=ollama_ok and n_missing > 0,
            )
        )

    # 5. Build the derived models and verify
    title5 = "Build the derived models and verify the context window"
    why5 = (
        "Sets up Kestrel's own copy of each model ("
        + ", ".join(st.derived_names)
        + ") with room for a whole job posting, then checks that a long posting really fits. "
        "Skipping this is the most common cause of quietly wrong scores."
    )
    if not local:
        steps.append(_skipped(5, "build", title5))
    elif st.missing_derived:
        built = len(st.derived_names) - len(st.missing_derived)
        steps.append(
            StepState(
                "build", 5, title5, "todo",
                detail=f"{built} of {len(st.derived_names)} built" if ollama_ok else "needs Ollama",
                explanation=why5,
                commands=[
                    f"ollama create {n} -f {setup_cli.modelfiles_dir() / ('Modelfile.' + n[8:])}"
                    for n in st.missing_derived
                ],
                action="build", action_label=f"Build on {host}" if host else "Build",
                action_enabled=ollama_ok and not st.missing_tags,
            )
        )
    elif st.verified == "ok":
        steps.append(
            StepState(
                "build", 5, title5, "ok", detail=st.verified_detail, explanation=why5,
                action="verify", action_label="Verify again",
                action_enabled=ollama_ok,
            )
        )
    else:
        steps.append(
            StepState(
                "build", 5, title5, "warn" if st.verified == "failed" else "todo",
                detail=st.verified_detail,
                explanation=why5 + " The models are built; press Verify to run the check.",
                commands=["kestrel doctor"],
                action="verify", action_label="Verify", action_enabled=ollama_ok,
            )
        )

    # 6. Hosted provider and key
    keys = [n for n, ok in st.hosted_keys.items() if ok]
    key_line = ("key present for " + ", ".join(keys)) if keys else "no key yet"
    if st.hosted_needed:
        hosted_ready = bool(keys) and st.hosted_problem is None
        status6 = "ok" if hosted_ready else "todo"
        bits = [key_line]
        if keys and st.hosted_problem:
            bits.append(st.hosted_problem)
        detail6 = "; ".join(bits)
        pill6 = "ok" if hosted_ready else "required"
    else:
        status6 = "ok" if keys else "todo"
        detail6 = key_line
        pill6 = "ok" if keys else "optional"
    steps.append(
        StepState(
            "hosted", 6, "Hosted provider and key", status6,
            detail=detail6,
            explanation="On Settings > Hosted: pick a provider (OpenRouter first: free models "
            "available; then OpenModel, Anthropic or OpenAI), paste your key, read what each "
            "tier sends, and connect. Kestrel tests the key and picks a model per tier (a "
            "free-only filter is available). "
            + (
                "Required for Hosted and Both."
                if st.hosted_needed
                else "Optional for Local: it enables deeper reads only."
            ),
            link="/settings#hosted",
            link_label="Add a key" if not keys else "Settings > Hosted",
            pill=pill6, pill_class="apply" if pill6 == "ok" else "grey",
        )
    )

    # The first incomplete step with something to click is the one to do now. The profile step
    # has neither (it is recorded by the build), so it never holds the marker.
    for step in steps:
        if step.status == "todo" and (step.action or step.link):
            step.status = "now"
            break
    return steps


def _skipped(number: int, key: str, title: str) -> StepState:
    return StepState(key, number, title, "skip", explanation=SKIPPED_HOSTED, pill=SKIPPED_HOSTED)


# ===================================================================================== actions
class ActionBusy(RuntimeError):
    """Another action is still running; the route answers 409."""


@dataclass
class Action:
    name: str
    status: str = "idle"  # idle | running | done | failed
    lines: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    thread: threading.Thread | None = None

    def log(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        with _lock:
            # A progress bar repaints the same line with a new percentage; keep one copy.
            if self.lines and "%" in line and self.lines[-1].split()[:2] == line.split()[:2]:
                self.lines[-1] = line
            else:
                self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def snapshot(self) -> dict[str, Any]:
        with _lock:
            return {
                "name": self.name,
                "status": self.status,
                "lines": list(self.lines),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "step": ACTION_STEP.get(self.name, ""),
            }

    def join(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


_registry: dict[str, Action] = {}
_lock = threading.Lock()


def get_action(name: str) -> Action | None:
    with _lock:
        return _registry.get(name)


def running_action() -> Action | None:
    with _lock:
        return next((a for a in _registry.values() if a.status == "running"), None)


def latest_action() -> Action | None:
    """The running action, else the most recently started one (its log stays under its step)."""
    with _lock:
        if not _registry:
            return None
        return max(_registry.values(), key=lambda a: a.started_at)


def reset_actions() -> None:
    with _lock:
        _registry.clear()


def start_action(name: str, work: Callable[[Action], None]) -> Action:
    """Run `work(action)` in a daemon thread. Exactly one action runs at a time."""
    with _lock:
        busy = next((a for a in _registry.values() if a.status == "running"), None)
        if busy is not None:
            raise ActionBusy(f"{busy.name} is still running")
        act = Action(name=name, status="running", started_at=utcnow_iso())
        _registry[name] = act

    def runner() -> None:
        try:
            work(act)
        except Exception as exc:  # the thread must report, never die silently
            act.error = f"{type(exc).__name__}: {exc}"
            act.log(f"failed: {act.error}")
            log.warning("setup_action_failed", action=name, error=act.error)
            act.status = "failed"
        else:
            act.status = "done"
        finally:
            forget_ollama_probe()  # a pull or build changed the tags; the next assess() asks
            act.finished_at = utcnow_iso()

    act.thread = threading.Thread(target=runner, name=f"setup-{name}", daemon=True)
    act.thread.start()
    return act


def verify() -> Action:
    """`kestrel doctor`, the full preflight including the needle test; the report is persisted so
    the Build step can say "verified" afterwards."""

    def work(act: Action) -> None:
        from kestrel import doctor as doctor_mod

        act.log("running kestrel doctor (the needle test takes a minute per tier)")
        rep = doctor_mod.run_doctor()
        for line in doctor_mod.format_report(rep).splitlines():
            act.log(line)
        act.log(f"report saved to {save_report(rep)}")
        if rep.has_blocking_failure:
            raise RuntimeError("a blocking preflight check failed (see the report above)")

    return start_action("verify", work)


# ---------------------------------------------------------------------------------- settings
async def _set(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


HOSTED_PENDING = "Hosted mode will apply once step 6 is complete"


def _record_hosted_profile() -> None:
    """No local model to pull or build: record the hosted profile so doctor and Settings know,
    as `kestrel setup --hosted` does. An existing local profile is left alone. File I/O and
    possibly nvidia-smi, so the async caller runs it in a thread."""
    if profiles.load_active_profile() is None:
        profiles.save_active_profile(
            profiles.HOSTED_PROFILE, "chosen on the Setup page", detected_hardware()
        )


async def set_run_mode(session: AsyncSession, choice: str, settings: Settings) -> str:
    """Write the card choice, and `llm.mode`: local for Local and Both; hosted for Hosted only
    when `hosted_mode_problem` reports none (the same rule as the Settings switch and the CLI),
    otherwise the mode row is left as it is and the page says hosted applies once step 6 is
    complete. Returns the message for the page. The caller commits."""
    if choice not in CHOICES:
        raise ValueError(f"unknown run choice {choice!r}; expected one of {', '.join(CHOICES)}")
    await _set(session, CHOICE_KEY, choice)
    if choice == "local":
        await _set(session, MODE_KEY, "local")
        return "Kestrel runs on local models. Steps 2 to 5 apply."
    if choice == "both":
        await _set(session, MODE_KEY, "local")
        return "Local models for daily runs, a hosted set ready to switch to. Steps 2 to 7 apply."
    await asyncio.to_thread(_record_hosted_profile)
    problem = hosted_mode_problem(settings)
    if problem is None:
        await _set(session, MODE_KEY, "hosted")
        return "Kestrel runs on hosted models."
    return f"{HOSTED_PENDING} under Settings > Hosted: {problem}."


async def apply_pending_hosted_mode(
    session: AsyncSession, settings: Settings, rows: dict[str, Any]
) -> bool:
    """The Hosted card was chosen before step 6 was done, so `llm.mode` was not written then.
    Once `hosted_mode_problem` reports none, write it. Returns True when a row was written; the
    caller commits."""
    if rows.get(CHOICE_KEY) != "hosted" or rows.get(MODE_KEY) == "hosted":
        return False
    if hosted_mode_problem(settings) is not None:
        return False
    await _set(session, MODE_KEY, "hosted")
    return True


def record_profile(name: str, reason: str = "") -> Path:
    """The Change button on step 3: record a profile from the catalog by name."""
    cat = profiles.load_catalog()
    cat.profile(name)  # KeyError with the known names when unknown
    return profiles.save_active_profile(
        name, reason or "chosen on the Setup page", detected_hardware()
    )
