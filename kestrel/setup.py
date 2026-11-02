"""`kestrel setup`: guided first-run setup for Windows, macOS and Linux (SPEC-INTERFACES.md 7).

One readable line per step, the exact command each step would run, and nothing executed without
confirmation unless `--yes`. `--dry-run` prints every step and runs nothing. No stack traces: a
step that fails prints its one-line reason, and the exit code is non-zero when a blocking step
(Ollama unavailable, a pull or build failing, a blocking doctor failure) did not complete.

Steps: detect hardware and pick a profile; find or install Ollama; pull the profile's base models;
render the profile's Modelfiles under <config_dir>/models and `ollama create` each derived model;
record the profile in <config_dir>/hardware_profile.json; run doctor; seed config and say what is
next. Hard rule 1 is untouched: everything here acts on this machine only.

The hosted path (`--hosted`, the `hosted_only` profile, or the interactive choice when Ollama is
missing) replaces the Ollama, pull and build steps with one: pick a hosted provider (OpenRouter,
OpenModel, Anthropic or OpenAI), store
the operator's own key in `.env`, verify it, list the schema-capable models, choose one per tier,
show the same Hosted mode disclosure the Settings page shows and require `yes`, then write the
same setting-table rows the Settings page writes (D-SETUP-4). Nothing is pulled or built, and the
only network calls are the provider's key check and model list, which carry no operator data.

Exposed as `setup_app` (a Typer with one default command) for cli.py to mount as `setup`.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import typer

from kestrel import paths
from kestrel.config import HOSTED_TIER_SENDS, HostedMode
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.llm.profiles import no_console_window

OK, FAIL, SKIP, RUN, WARN = "[ OK ]", "[FAIL]", "[SKIP]", "[ -- ]", "[WARN]"
DEFAULT_NATIVE_URL = "http://127.0.0.1:11434"
OLLAMA_START_TIMEOUT_S = 60.0

INSTALL_COMMANDS: dict[str, str] = {
    "windows": "winget install --id Ollama.Ollama -e",
    "macos": "brew install --cask ollama",
    "linux": "curl -fsSL https://ollama.com/install.sh | sh",
}
DOWNLOAD_URL = "https://ollama.com/download"

# The three answers when Ollama is missing. The hosted one is a real choice, not a fallback.
CHOICE_INSTALL, CHOICE_HOSTED, CHOICE_QUIT = "install", "hosted", "quit"
HOSTED_HINT = (
    "no GPU, or short on memory? `kestrel setup --hosted` runs every tier on OpenRouter or "
    "OpenModel under your own key; nothing to install or download"
)

# The tiers the hosted path routes, in the order rows are written: every tier the Settings page
# offers (providers.HOSTED_TIERS), because a hosted-only machine has no local model to run any
# of them on (D-SETUP-4). Each follows the pick of its parent tier.
HOSTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("tier1", "tier1"),
    ("tier2", "tier2"),
    ("tier3", "tier2"),
    ("docsel", "docsel"),
    ("trigger", "tier1"),
    (prov.FRONTIER_TIER, "tier2"),
)
PICK_TIERS: tuple[str, ...] = ("tier1", "tier2", "docsel")
_PICK_ROLES = {
    "tier1": "fast triage (also the trigger classifier)",
    "tier2": "the deep evaluation (also deeper reads)",
    "docsel": "document slot selection",
}
# A model priced above this per million prompt tokens is never a default pick; the operator can
# still name it with --model-tier2.
STRONG_PRICE_CAP_USD_PER_M = 10.0
TABLE_ROWS = 40

# What each hostable tier transmits once it is routed to a hosted backend: the config-layer source
# (config.HOSTED_TIER_SENDS), shared with kestrel.web.pages.settings._TIER_SENDS.
_TIER_SENDS: dict[str, str] = HOSTED_TIER_SENDS
# The per-model capability row (services.hosted_models.CAPS_KEY).
CAPS_ROW = "llm.hosted_model_caps"

# The Hosted mode disclosure, word for word the warnbox in
# kestrel/web/templates/partials/hosted_status.html (a test reads the template and compares).
# `{tier1}` and friends are the filled _TIER_SENDS lines.
HOSTED_DISCLOSURE = (
    "Read before turning this on. Hosted mode sends opportunity and profile text to a provider "
    "you choose, under your own key, automatically.\n"
    "What leaves the machine, per hosted tier:\n"
    "Tier 1 sends {tier1}.\n"
    "Tier 2 sends {tier2}.\n"
    "Tier 3 work sends {tier3}.\n"
    "Document selection sends {docsel}.\n"
    "Trigger classification sends {trigger}.\n"
    "Deeper reads are unchanged: they send {frontier}.\n"
    "The provider's own retention rules apply to everything sent; Kestrel cannot recall it.\n"
    "OpenRouter free models may train on your inputs unless the data collection option below is "
    "set to deny, and with deny on there may be no free model left to route to.\n"
    "Your current employer is redacted only when confidential mode is on and you have chosen it "
    "(Settings > Privacy and access: Confidential mode). With confidential mode on and no employer "
    "chosen, the send is refused and the item is held in quarantine, not rejected.\n"
    "Keys stay on this machine and are never sent anywhere but the provider."
)
# What the hosted-only setup adds on top of the Settings page's own flow.
HOSTED_ONLY_ADDENDUM = (
    "Hosted-only setup also routes two tiers the Settings page leaves local by default, because "
    "this machine has no local model to run them on: `tier3` and `trigger` (their sends are listed "
    "above). Both appear in the Settings > Hosted table and can be set back to local there."
)
# `--yes` runs the install, pull and build steps unasked (D-SETUP-1); it never stands in for the
# Hosted mode disclosure, which is an informed opt-in, not a step. This flag is the only
# non-interactive way to accept it.
ACCEPT_FLAG = "--accept-hosted-disclosure"
YES_IS_NOT_ACCEPTANCE = (
    f"--yes does not accept the Hosted mode disclosure above; rerun without --yes to type yes, "
    f"or pass {ACCEPT_FLAG} to accept it; nothing was turned on and no row written"
)
EMBED_NOTE = (
    "Embeddings: on this machine with the bundled in-process model; nothing about them is sent "
    "anywhere"
)
FREE_CAP_NOTE = (
    "OpenRouter free models carry a daily request cap per account (higher once the account holds "
    "credit); a batch beyond the cap waits for the next day."
)
FREE_TRAIN_NOTE = (
    "Free models may train on your inputs unless OpenRouter data collection is set to deny (the "
    "shipped default, Settings > Hosted changes it), and with deny on there may be no free model "
    "left to route to."
)

setup_app = typer.Typer(
    help="Guided first-run setup: hardware profile, Ollama or a hosted provider, preflight.",
    invoke_without_command=True,
    no_args_is_help=False,
)

_sleep = time.sleep  # patched in tests so the Ollama wait does not stall


@dataclass
class HostedOptions:
    """The `--hosted` switches: provider, free-only, and the per-tier model overrides."""

    provider: str | None = None
    free: bool = False
    model: str | None = None
    model_tier1: str | None = None
    model_tier2: str | None = None
    model_docsel: str | None = None
    # The explicit, non-interactive acceptance of the Hosted mode disclosure (ACCEPT_FLAG).
    accept_disclosure: bool = False

    def forced(self, tier: str) -> str | None:
        specific = {
            "tier1": self.model_tier1,
            "tier2": self.model_tier2,
            "docsel": self.model_docsel,
        }.get(tier)
        return specific or self.model


@dataclass
class SetupState:
    """Everything the steps share, plus the switches that govern prompting and execution."""

    yes: bool = False
    dry_run: bool = False
    native_url: str = DEFAULT_NATIVE_URL
    os_name: str = "unknown"
    hardware: profiles.Hardware | None = None
    profile: str = ""
    reason: str = ""
    hosted: bool = False
    hosted_opts: HostedOptions = field(default_factory=HostedOptions)
    failed: list[str] = field(default_factory=list)

    def echo(self, status: str, text: str) -> None:
        typer.echo(f"{status}  {text}")

    def would(self, cmd: str) -> None:
        self.echo(RUN, f"would run: {cmd}")

    def fail(self, step: str, reason: str) -> None:
        self.failed.append(step)
        self.echo(FAIL, f"{step}: {reason}")

    def confirm(self, question: str, default: bool) -> bool:
        """Ask unless --yes; a dry run never asks and never answers yes."""
        if self.dry_run:
            return False
        if self.yes:
            return True
        return typer.confirm(question, default=default)

    def run(self, cmd: list[str], *, stream: bool = False, timeout: float | None = None) -> int:
        """Run a command; returns the exit code (non-zero on any failure, never raises)."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=not stream,
                text=True,
                timeout=timeout,
                check=False,
                **no_console_window(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.echo(FAIL, f"{cmd[0]}: {type(exc).__name__}: {exc}")
            return 1
        if proc.returncode != 0 and not stream and proc.stderr:
            self.echo(FAIL, proc.stderr.strip().splitlines()[-1][:200])
        return proc.returncode


# ------------------------------------------------------------------------------- ollama probes
def ollama_version(native_url: str, timeout: float = 3.0) -> str | None:
    try:
        r = httpx.get(f"{native_url.rstrip('/')}/api/version", timeout=timeout)
        r.raise_for_status()
        return str(r.json().get("version", "") or "unknown")
    except (httpx.HTTPError, ValueError):
        return None


def ollama_tags(native_url: str, timeout: float = 10.0) -> list[str] | None:
    try:
        r = httpx.get(f"{native_url.rstrip('/')}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [str(m.get("name", "")) for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def _tag_present(tag: str, tags: list[str]) -> bool:
    return tag in tags or (":" not in tag and f"{tag}:latest" in tags)


def _wait_for_ollama(st: SetupState, timeout_s: float | None = None) -> str | None:
    deadline = time.monotonic() + (OLLAMA_START_TIMEOUT_S if timeout_s is None else timeout_s)
    while True:
        ver = ollama_version(st.native_url)
        if ver or time.monotonic() >= deadline:
            return ver
        _sleep(2.0)


# -------------------------------------------------------------------------------------- steps
def step_hardware(st: SetupState, requested: str | None, hosted: bool = False) -> None:
    cat = profiles.load_catalog()
    hw = profiles.detect_hardware()
    st.hardware = hw
    st.os_name = hw.os
    recommended, reason = profiles.recommend_profile(hw)
    st.echo(OK, f"Hardware: {hw.summary()}")
    if hosted and requested and requested != profiles.HOSTED_PROFILE:
        st.fail("Profile", f"--hosted and --profile {requested} conflict; pass one of them")
        return
    if hosted:
        requested = profiles.HOSTED_PROFILE
    if requested:
        if requested not in cat.profiles:
            st.fail("Profile", f"unknown profile {requested!r}; known: {', '.join(cat.names())}")
            return
        flag = "--hosted" if hosted else "--profile"
        st.profile, st.reason = requested, f"chosen with {flag} ({reason})"
        st.echo(OK, f"Profile: {requested} (requested; recommended was {recommended})")
        st.hosted = cat.profile(requested).hosted
        return
    st.echo(OK, f"Recommended profile: {recommended} ({reason})")
    if recommended != profiles.HOSTED_PROFILE:
        st.echo(RUN, HOSTED_HINT)
    elif hw.gpu_vendor is None and not hw.apple_silicon:
        # No GPU reported is also what a failed nvidia-smi looks like (D-SETUP-6 review). Say so
        # before --yes follows the hosted recommendation, so a capable machine is not stranded.
        st.echo(WARN, "No supported GPU was detected. If this machine has an NVIDIA GPU, "
                      "detection failed: check that `nvidia-smi` runs, or rerun with "
                      "--profile gpu_16gb (or your card's profile) to set up locally.")
    choice = recommended
    if not (st.yes or st.dry_run):
        typer.echo("       profiles: " + ", ".join(cat.names()))
        while True:
            choice = typer.prompt("Profile to use", default=recommended).strip()
            if choice in cat.profiles:
                break
            typer.echo(f"       unknown profile {choice!r}; pick one of the names above")
    st.profile, st.reason = choice, reason
    prof = cat.profile(choice)
    st.hosted = prof.hosted
    st.echo(OK, f"Profile: {choice}: {prof.description}")


def _ollama_missing_choice(st: SetupState, install: str | None) -> str:
    """Ask what to do about a missing Ollama: install it, go hosted, or quit. `--yes` installs
    (the documented meaning of --yes, D-SETUP-1) and a dry run reports the install command."""
    if st.yes or st.dry_run:
        return CHOICE_INSTALL if install else CHOICE_QUIT
    typer.echo("       Ollama is not installed. Choose:")
    if install:
        typer.echo(f"         1) Install Ollama ({install})")
    else:
        typer.echo(f"         1) Quit and install Ollama from {DOWNLOAD_URL}")
    typer.echo(
        "         2) Use a hosted provider instead (OpenRouter, OpenModel, Anthropic or OpenAI)"
    )
    typer.echo("         3) Quit")
    while True:
        answer = typer.prompt("Choice", default="1").strip().lower()
        if answer in ("1", "install"):
            return CHOICE_INSTALL if install else CHOICE_QUIT
        if answer in ("2", "hosted"):
            return CHOICE_HOSTED
        if answer in ("3", "quit", "q"):
            return CHOICE_QUIT
        typer.echo("       answer 1, 2 or 3")


def _go_hosted(st: SetupState, why: str) -> None:
    st.hosted = True
    st.profile, st.reason = profiles.HOSTED_PROFILE, why
    st.echo(OK, f"Profile: {profiles.HOSTED_PROFILE} ({why})")


def step_ollama(st: SetupState) -> bool:
    """True when Ollama answers (or would, in a dry run); False blocks the model steps."""
    binary = shutil.which("ollama")
    ver = ollama_version(st.native_url)
    if ver:
        st.echo(OK, f"Ollama {ver} running at {st.native_url}")
        return True
    if binary:
        st.echo(OK, f"Ollama installed at {binary} but not running")
        cmd = "ollama serve"
        if st.dry_run:
            st.would(cmd)
            return True
        if st.confirm("Start Ollama now?", default=True):
            try:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    **no_console_window(),
                )
            except OSError as exc:
                st.fail("Ollama", f"could not start: {exc}")
                return False
            ver = _wait_for_ollama(st)
            if ver:
                st.echo(OK, f"Ollama {ver} running at {st.native_url}")
                return True
        st.fail("Ollama", "not running; start the Ollama app (or `ollama serve`) and rerun")
        return False
    install = INSTALL_COMMANDS.get(st.os_name)
    st.echo(FAIL, f"Ollama not found on PATH and nothing answers at {st.native_url}")
    if install:
        st.echo(RUN, f"install command for {st.os_name}: {install}  (or {DOWNLOAD_URL})")
    choice = _ollama_missing_choice(st, install)
    if choice == CHOICE_HOSTED:
        _go_hosted(st, "chosen because Ollama is not installed")
        return False
    if install is None or choice == CHOICE_QUIT:
        where = f"`{install}`" if install else DOWNLOAD_URL
        st.fail(
            "Ollama",
            f"not installed; install it with {where} and rerun `kestrel setup`, or run "
            "`kestrel setup --hosted`",
        )
        return False
    if st.dry_run:
        st.would(install)
        return True
    # Installing software is a system change: default No; --yes skips the prompt.
    if not st.confirm("Install Ollama now with that command?", default=False):
        st.fail("Ollama", f"not installed; run `{install}` yourself, then rerun `kestrel setup`")
        return False
    rc = st.run(["sh", "-c", install] if st.os_name != "windows" else ["cmd", "/c", install])
    if rc != 0:
        st.fail("Ollama", f"install command exited {rc}; install from {DOWNLOAD_URL} and rerun")
        return False
    st.echo(OK, "Ollama installed; waiting for it to start (up to 60 s)")
    ver = _wait_for_ollama(st)
    if not ver:
        st.fail("Ollama", "installed but not answering yet; start it and rerun `kestrel setup`")
        return False
    st.echo(OK, f"Ollama {ver} running at {st.native_url}")
    return True


def step_pull(st: SetupState) -> bool:
    prof = profiles.load_catalog().profile(st.profile)
    present = ollama_tags(st.native_url) or []  # a read-only GET; fine in a dry run too
    ok = True
    for tag in prof.models.base_tags():
        cmd = f"ollama pull {tag}"
        if _tag_present(tag, present):
            st.echo(SKIP, f"{tag} already pulled")
            continue
        if st.dry_run:
            st.would(cmd)
            continue
        if not st.confirm(f"Pull {tag} (this downloads several GB)?", default=True):
            st.fail("Pull", f"{tag} skipped; run `{cmd}` later")
            ok = False
            continue
        typer.echo(f"       running: {cmd}")
        rc = st.run(["ollama", "pull", tag], stream=True)
        if rc == 0:
            st.echo(OK, f"{tag} pulled")
        else:
            st.fail("Pull", f"`{cmd}` exited {rc}")
            ok = False
    return ok


def modelfiles_dir() -> Path:
    return paths.config_dir() / "models"


def step_build(st: SetupState) -> bool:
    dest = modelfiles_dir()
    if st.dry_run:
        for tier in profiles.DERIVED_TIERS:
            st.would(f"ollama create {profiles.derived_name(tier)} -f {dest / f'Modelfile.{tier}'}")
        return True
    try:
        written = profiles.write_modelfiles(st.profile, dest)
    except (OSError, KeyError, ValueError) as exc:
        st.fail("Modelfiles", f"could not write under {dest}: {exc}")
        return False
    st.echo(OK, f"Modelfiles for {st.profile} written to {dest}")
    ok = True
    for mf in written:
        tier = mf.name.split(".", 1)[1]
        name = profiles.derived_name(tier)
        cmd = f"ollama create {name} -f {mf}"
        if not st.confirm(f"Build {name}?", default=True):
            st.fail("Build", f"{name} skipped; run `{cmd}` later")
            ok = False
            continue
        rc = st.run(["ollama", "create", name, "-f", str(mf)])
        if rc == 0:
            st.echo(OK, f"{name} built")
        else:
            st.fail("Build", f"`{cmd}` exited {rc}")
            ok = False
    return ok


# ------------------------------------------------------------------------------- hosted path
def _settings() -> Any:
    from kestrel.config import get_settings

    return get_settings()


def tier_sends(settings: Any) -> dict[str, str]:
    """The filled per-tier 'sends' lines, computed exactly as the Settings page computes them."""
    # One source with the acknowledgement record (config.HostedMode.disclosure_terms), so
    # the text shown is the text an acknowledgement is checked against.
    return dict(settings.llm.hosted_mode.disclosure_terms()["sends"])


def hosted_disclosure(settings: Any) -> str:
    return HOSTED_DISCLOSURE.format(**tier_sends(settings))


def _price(m: prov.ModelInfo) -> float | None:
    if m.prompt_usd_per_m is None or m.completion_usd_per_m is None:
        return None
    return m.prompt_usd_per_m + m.completion_usd_per_m


def _fmt_price(x: float | None) -> str:
    return "?" if x is None else f"{x:.2f}"


def model_table(models: list[prov.ModelInfo], free_ids: set[str], limit: int = TABLE_ROWS) -> str:
    """id, context, $/M in and out, free marker; cheapest first, then by id."""
    rows = sorted(models, key=lambda m: (_price(m) is None, _price(m) or 0.0, m.id))
    width = min(60, max((len(m.id) for m in rows), default=10))
    lines = [f"       {'model id':<{width}}  {'context':>9}  {'$/M in':>8}  {'$/M out':>8}  free"]
    for m in rows[:limit]:
        ctx = f"{m.context_length:,}" if m.context_length else "?"
        lines.append(
            f"       {m.id[:width]:<{width}}  {ctx:>9}  {_fmt_price(m.prompt_usd_per_m):>8}  "
            f"{_fmt_price(m.completion_usd_per_m):>8}  {'free' if m.id in free_ids else ''}"
        )
    if len(rows) > limit:
        lines.append(f"       ... and {len(rows) - limit} more; any listed id works with --model")
    return "\n".join(lines)


def default_picks(
    pool: list[prov.ModelInfo], provider: str = "generic"
) -> dict[str, prov.ModelInfo]:
    """One model per PICK_TIERS from the schema-capable pool (D-SETUP-5). A provider with
    suggested picks (providers.SUGGESTED_PICKS) uses them for every tier whose suggestion it
    actually lists; the rest fall through to the generic rule:

    - tier2 (the deep evaluation): the first family in providers.STRONG_FAMILIES present in the
      pool, newest release first within it, larger context breaking ties; priced models above
      STRONG_PRICE_CAP per million prompt tokens are never a default. A pool naming no known
      family falls back to the priciest affordable model (price as a weak quality proxy).
    - tier1 and docsel (triage, slot selection): a fast or small member of any known family
      (providers.FAST_MARKERS) ahead of every full-size model, then the family order, then the
      cheaper price, then recency.
    - Reasoning-first, preview and routing-variant ids (providers.UNPICKED_MARKERS) are skipped
      while anything else is available; the operator can still name them.

    Price is deliberately not the quality signal: the priciest affordable id on OpenRouter has
    been a years-old model or a non-chat variant, and in a free-only pool every price is zero."""
    if not pool:
        return {}
    # An automatic default is a model that honors a strict schema whenever the pool has one;
    # JSON-mode and prompt-only models (D-UI-48) are the operator's own pick on the Models page.
    pool = [m for m in pool if m.json_mode in (None, "schema")] or pool
    by_id = {m.id: m for m in pool}
    picks = {
        tier: by_id[model_id]
        for tier, model_id in prov.SUGGESTED_PICKS.get(provider, {}).items()
        if model_id in by_id
    }
    if len(picks) == len(PICK_TIERS):
        return picks
    candidates = [m for m in pool if prov.is_pickable(m.id)] or list(pool)
    ids = {m.id for m in pool}

    def free_twin(m: prov.ModelInfo) -> int:
        # 1 when this is the `:free` variant of a paid id also in the pool: the paid one wins
        # (the free endpoint is rate-limited and may train on inputs). A free model with no
        # paid twin, or a free-only pool, is not penalized.
        return 1 if prov.is_free_variant(m.id) and m.id[: -len(":free")] in ids else 0

    affordable = [
        m for m in candidates if (m.prompt_usd_per_m or 0.0) <= STRONG_PRICE_CAP_USD_PER_M
    ] or candidates

    def strong_key(m: prov.ModelInfo) -> tuple:
        return (
            prov.family_rank(m.id),
            free_twin(m),
            -(m.created or 0),
            -(m.context_length or 0),
            m.id,
        )

    def fast_key(m: prov.ModelInfo) -> tuple:
        return (
            0 if prov.is_fast_variant(m.id) else 1,
            prov.family_rank(m.id),
            free_twin(m),
            _price(m) is None,
            _price(m) or 0.0,
            -(m.created or 0),
            -(m.context_length or 0),
            m.id,
        )

    known = [m for m in affordable if prov.family_rank(m.id) < len(prov.STRONG_FAMILIES)]
    if known:
        strong = min(known, key=strong_key)
        cheap = min(known, key=fast_key)
    else:
        strong = max(affordable, key=lambda m: (_price(m) or 0.0, m.context_length or 0, m.id))
        cheap = min(affordable, key=fast_key)
    return {**{"tier1": cheap, "tier2": strong, "docsel": cheap}, **picks}


def pick_reason(tier: str, m: prov.ModelInfo, free_ids: set[str]) -> str:
    """One short clause per pick for the Connect message: price (or free), and context."""
    if m.id in free_ids:
        price = "free"
    elif m.prompt_usd_per_m is None or m.completion_usd_per_m is None:
        price = "price not listed"
    else:
        price = f"${m.prompt_usd_per_m:.2f}/{m.completion_usd_per_m:.2f} per M in/out"
    ctx = f", {m.context_length // 1000}k context" if m.context_length else ""
    return f"{tier} {m.id} ({price}{ctx})"


def choose_models(
    st: SetupState, pool: list[prov.ModelInfo], backend: Any
) -> dict[str, prov.ModelInfo] | None:
    """One model per PICK_TIERS: the flag wins, else the default, else (interactively) the prompt.
    A model not in `pool` is refused by name, never silently defaulted away (hard rule 3: the pool
    is the schema-capable list). On OpenModel every pick must speak one protocol, since the
    backend has exactly one."""
    by_id = {m.id: m for m in pool}
    picks = default_picks(pool, backend.provider)
    for tier in PICK_TIERS:
        forced = st.hosted_opts.forced(tier)
        if forced:
            if forced not in by_id:
                limit = "free, " if st.hosted_opts.free else ""
                st.fail(
                    "Hosted",
                    f"--model for {tier}: {forced!r} is not in the provider's {limit}"
                    "schema-capable list above",
                )
                return None
            picks[tier] = by_id[forced]
            continue
        if st.yes or st.dry_run:
            continue
        while True:
            answer = typer.prompt(
                f"Model for {tier} ({_PICK_ROLES[tier]})", default=picks[tier].id
            ).strip()
            if answer in by_id:
                picks[tier] = by_id[answer]
                break
            typer.echo(f"       {answer!r} is not in the list above; pick a listed id")
    if backend.provider == "openmodel":
        try:
            shared_protocol(picks)
        except ValueError as exc:
            st.fail("Hosted", str(exc))
            return None
    return picks


def shared_protocol(picks: dict[str, prov.ModelInfo]) -> str | None:
    """The one wire protocol every OpenModel pick speaks (`messages` preferred, as
    providers.protocol_for_model prefers it), None when no pick lists protocols (keep the
    configured one), ValueError when the picks share none: the backend has one protocol."""
    listed = [set(m.protocols) for m in picks.values() if m.protocols]
    if not listed:
        return None
    common = set.intersection(*listed) & {"messages", "responses"}
    if not common:
        named = ", ".join(f"{t}: {m.id} ({'/'.join(m.protocols) or '?'})" for t, m in picks.items())
        raise ValueError(
            f"OpenModel has one protocol per backend; pick models that share one ({named})"
        )
    return "messages" if "messages" in common else "responses"


def _embed_inprocess(st: Any) -> bool:
    """Whether this run routes embeddings in-process, so the note about it is only shown then."""
    return embed_rows(getattr(st, "profile", None)).get("llm.tiers.embed.backend") == "inprocess"


def embed_rows(profile: str | None) -> dict[str, Any]:
    """The `llm.tiers.embed` rows a hardware profile implies (D-EMBED-3): the hosted_only profile
    runs embeddings in-process, so a BYOK install never needs Ollama. Empty for a profile that
    keeps the shipped Ollama embed tier, and for no or an unknown profile. Written to the LOCAL
    tier set because embeddings never go hosted; the in-process engine serves both modes."""
    if not profile:
        return {}
    try:
        route = profiles.embed_route(profile)
    except KeyError:
        return {}
    if route is None:
        return {}
    backend, model = route
    return {"llm.tiers.embed.backend": backend, "llm.tiers.embed.model": model}


def hosted_rows(
    backend_name: str,
    backend: Any,
    picks: dict[str, prov.ModelInfo],
    now: str,
    *,
    free_only: bool = False,
    profile: str | None = None,
    terms: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The setting-table rows, the same keys Settings > Hosted writes (hosted_mode_toggle,
    hosted_switch, hosted_save_tiers, hosted_save_model) plus tier3 and trigger (HOSTED_ROUTES).
    Hosted assignments go under `llm.hosted_tiers` (the hosted tier set; D-HOSTED-2) and the
    switch `llm.mode` is set to hosted; the frontier tier is hosted in either mode and stays
    under `llm.tiers`. The local tier set is otherwise never touched, so switching back to local
    later needs nothing rebuilt; the one exception is `profile` hosted_only, whose embed tier
    moves to the in-process engine (embed_rows), because that machine has no Ollama to fall
    back to."""
    rows: dict[str, Any] = {
        "llm.hosted_mode.enabled": True,
        "llm.hosted_mode.acknowledged_at": now,
        # What the operator accepted (HostedMode.disclosure_terms): a later change to the
        # disclosure voids this acceptance and pauses hosted sends until it is accepted again.
        "llm.hosted_mode.acknowledged_terms": (
            terms if terms is not None else HostedMode().disclosure_terms()
        ),
        "llm.hosted_mode.free_only": free_only,
        "llm.mode": "hosted",
        # How each picked model takes JSON and its context window, from the list it was picked
        # from, so the request builder never guesses a strict schema for a model that lacks one
        # (D-UI-48). _write_rows merges this into the stored map.
        CAPS_ROW: {m.id: prov.model_caps(m).model_dump() for m in picks.values()},
    }
    for tier, pick_tier in HOSTED_ROUTES:
        prefix = "llm.tiers" if tier == prov.FRONTIER_TIER else "llm.hosted_tiers"
        rows[f"{prefix}.{tier}.backend"] = backend_name
        rows[f"{prefix}.{tier}.model"] = picks[pick_tier].id
    if backend.provider == "openmodel":
        protocol = shared_protocol(picks)
        if protocol:
            rows[f"llm.backends.{backend_name}.protocol"] = protocol
    rows.update(embed_rows(profile))
    return rows


async def _write_rows(rows: dict[str, Any]) -> None:
    """Upsert setting rows on a fresh engine (setup runs before the app and owns no loop)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import make_async_engine
    from kestrel.db.models import Setting

    engine = make_async_engine(paths.db_path())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for key, value in rows.items():
                row = await session.get(Setting, key)
                if key == CAPS_ROW and row is not None and isinstance(row.value, dict):
                    value = {**row.value, **value}  # keep the entries for other models
                if row is None:
                    session.add(Setting(key=key, value=value))
                else:
                    row.value = value
                    row.updated_at = utcnow_iso()
            await session.commit()
    finally:
        await engine.dispose()


def _pick_provider(st: SetupState) -> str | None:
    requested = st.hosted_opts.provider
    if requested:
        if requested not in prov.PRESETS:
            st.fail("Hosted", f"unknown provider {requested!r}; known: {', '.join(prov.PRESETS)}")
            return None
        return requested
    if st.yes or st.dry_run:
        return "openrouter"
    names = list(prov.PRESETS)
    while True:
        answer = typer.prompt(f"Provider ({' or '.join(names)})", default="openrouter").strip()
        if answer in prov.PRESETS:
            return answer
        typer.echo(f"       pick one of {', '.join(names)}")


def _obtain_key(st: SetupState, label: str, env: str) -> bool:
    """Keep a key already in Kestrel's .env, or ask for one and write it. Never
    echoed, never logged."""
    if prov.secret_present(env):
        st.echo(OK, f"{env} is already set")
        if st.yes or st.confirm("Keep the existing key?", default=True):
            return True
    elif st.yes:
        st.fail(
            "Hosted",
            f"{env} is not set; add it to {paths.dotenv_path()} or run without --yes to be asked",
        )
        return False
    key = typer.prompt(
        f"{label} API key (input hidden)", hide_input=True, default="", show_default=False
    ).strip()
    if not key:
        st.fail("Hosted", "no key entered")
        return False
    try:
        prov.write_secret(env, key)
    except ValueError as exc:
        st.fail("Hosted", str(exc))
        return False
    st.echo(OK, f"Key written to {paths.dotenv_path()} as {env} (never logged, never shown again)")
    return True


def step_hosted(st: SetupState) -> bool:
    """The hosted-only path. True when the rows were written (or would be, in a dry run)."""
    settings = _settings()
    provider = _pick_provider(st)
    if provider is None:
        return False
    preset = prov.PRESETS[provider]
    backend_name, label = preset["backend"], preset["label"]
    backend = settings.llm.backends.get(backend_name)
    if backend is None or not backend.external or not backend.api_key_env:
        st.fail("Hosted", f"backend {backend_name!r} is not an external backend in settings.yml")
        return False
    env = backend.api_key_env
    base = backend.base_url.rstrip("/")
    st.echo(OK, f"Provider: {label} ({backend_name} -> {base}); key from {env}")
    if st.dry_run:
        st.would(f"prompt for the {label} API key and write it to {paths.dotenv_path()} as {env}")
        check = f"{base}/key" if provider == "openrouter" else f"{base}/models"
        st.would(f"GET {check} (verify the key; sends nothing about you)")
        st.would(f"GET {base}/models (list the schema-capable models, print the table)")
        st.would("choose a model per tier: " + ", ".join(PICK_TIERS) + " (--model overrides)")
        st.would("print the Hosted mode disclosure and require `yes`")
        st.would("kestrel db upgrade")
        routed = ", ".join(t for t, _ in HOSTED_ROUTES)
        embed = embed_rows(st.profile)
        st.would(
            "write setting rows: llm.hosted_mode.enabled, llm.hosted_mode.acknowledged_at, "
            f"llm.mode=hosted, llm.hosted_tiers.<tier>.backend and .model for {routed} "
            "(frontier under llm.tiers)"
            + (
                f", llm.tiers.embed -> {embed['llm.tiers.embed.backend']}/"
                f"{embed['llm.tiers.embed.model']}"
                if embed
                else ""
            )
        )
        if _embed_inprocess(st):
            st.echo(RUN, EMBED_NOTE)
        return True
    if not _obtain_key(st, label, env):
        return False
    status = asyncio.run(prov.verify_key(backend))
    if not status.ok:
        st.fail("Hosted", f"{label} rejected the key: {status.detail}")
        return False
    bits = [f"{label}: key accepted"]
    if status.label:
        bits.append(f"label {status.label}")
    if status.limit_remaining_usd is not None:
        bits.append(f"${status.limit_remaining_usd:.2f} of credit left")
    if status.is_free_tier:
        bits.append("free tier (daily caps apply)")
    st.echo(OK, "; ".join(bits))
    try:
        models = asyncio.run(prov.list_models(backend))
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
        st.fail("Hosted", f"could not list models: {type(exc).__name__}: {exc}")
        return False
    # The setup table and its default picks are the models that honor a strict schema; a
    # JSON-mode or prompt-only model (D-UI-48) is the operator's own pick on the Models page.
    fit = [m for m in prov.models_fit_for_tier3(models, backend)
           if m.json_mode in (None, "schema")]
    free = prov.free_models(fit)
    free_ids = {m.id for m in free}
    pool = free if st.hosted_opts.free else fit
    if not pool:
        detail = (
            "no free model can honor a JSON schema here (only OpenRouter reports prices, and "
            "data collection deny can exclude every free model)"
            if st.hosted_opts.free
            else "the provider lists no model that can honor a JSON schema"
        )
        st.fail("Hosted", detail)
        return False
    what = "free, schema-capable" if st.hosted_opts.free else "schema-capable"
    st.echo(OK, f"{len(pool)} {what} models on {label} ({len(free)} free)")
    typer.echo(model_table(pool, free_ids))
    if provider == "openrouter":
        typer.echo(f"       {FREE_CAP_NOTE}")
    if st.hosted_opts.free or (provider == "openrouter" and free):
        typer.echo(f"       {FREE_TRAIN_NOTE}")
    picks = choose_models(st, pool, backend)
    if picks is None:
        return False
    for tier, pick_tier in HOSTED_ROUTES:
        st.echo(OK, f"{tier}: {backend_name}/{picks[pick_tier].id}")
    typer.echo("")
    typer.echo(hosted_disclosure(settings))
    typer.echo(HOSTED_ONLY_ADDENDUM)
    typer.echo("")
    if st.hosted_opts.accept_disclosure:
        st.echo(OK, f"Disclosure accepted with {ACCEPT_FLAG}")
    elif st.yes:
        st.fail("Hosted", YES_IS_NOT_ACCEPTANCE)
        return False
    else:
        answer = typer.prompt("Type yes to turn on Hosted mode with these tiers", default="no")
        if answer.strip().lower() != "yes":
            st.fail("Hosted", "disclosure not accepted; nothing was turned on and no row written")
            return False
    from kestrel.db import migrate
    from kestrel.db.base import utcnow_iso

    now = utcnow_iso()
    rows = hosted_rows(
        backend_name,
        backend,
        picks,
        now,
        free_only=bool(st.hosted_opts.free),
        profile=st.profile,
        terms=settings.llm.hosted_mode.disclosure_terms(),
    )
    try:
        migrate.run_migrations()
        asyncio.run(_write_rows(rows))
    except Exception as exc:  # one line; the DB file and its reason are the operator's to fix
        st.fail("Hosted", f"could not write {paths.db_path()}: {type(exc).__name__}: {exc}")
        return False
    st.echo(OK, f"Hosted mode on (acknowledged {now}); {len(rows)} setting rows written")
    st.echo(OK, "Run mode: hosted (Settings > Hosted switches back to local models at any time)")
    if _embed_inprocess(st):
        st.echo(RUN, EMBED_NOTE)
    return True


def step_record(st: SetupState) -> None:
    p = profiles.active_profile_path()
    if st.dry_run:
        st.would(f"write {p} (profile {st.profile})")
        return
    try:
        profiles.save_active_profile(
            st.profile, st.reason, st.hardware, profiles.plan_bases(st.profile)
        )
    except OSError as exc:
        st.fail("Record profile", f"could not write {p}: {exc}")
        return
    st.echo(OK, f"Profile {st.profile} recorded in {p}")


def step_doctor(st: SetupState) -> None:
    if st.dry_run:
        st.would("kestrel doctor")
        return
    from kestrel import doctor as doctor_mod

    try:
        rep = doctor_mod.run_doctor()
    except Exception as exc:  # doctor never raises by design; this is the last line of defense
        st.fail("Doctor", f"{type(exc).__name__}: {exc}")
        return
    typer.echo(doctor_mod.format_report(rep))
    if rep.has_blocking_failure:
        st.fail("Doctor", "a blocking preflight check failed (see the report above)")
    else:
        st.echo(OK, "Doctor: no blocking failures")


def step_seed(st: SetupState) -> None:
    cfg = paths.config_dir()
    if st.dry_run:
        st.would(f"seed default config into {cfg}")
    else:
        try:
            written = paths.seed_config()
        except OSError as exc:
            st.fail("Config", f"could not seed {cfg}: {exc}")
            return
        st.echo(OK, f"Config seeded: {', '.join(written) if written else 'already present'}")
    profile_yml = cfg / "profile.yml"
    st.echo(
        OK if profile_yml.exists() else RUN,
        f"Operator profile: {profile_yml}"
        + ("" if profile_yml.exists() else " (copy profile.example.yml there and edit it)"),
    )


def _native_url() -> str:
    try:
        from kestrel.config import get_settings

        return get_settings().llm.ollama_native_url
    except Exception:  # config problems are doctor's to report; setup still needs a URL
        return DEFAULT_NATIVE_URL


def _run_step(st: SetupState, name: str, fn: Any) -> Any:
    try:
        return fn()
    except typer.Abort:  # Ctrl-C or end of input at a prompt
        st.fail(name, "aborted at a prompt; rerun `kestrel setup` to continue")
        return False
    except Exception as exc:  # one line, no trace: the operator is not an engineer
        st.fail(name, f"{type(exc).__name__}: {exc}")
        return False


def run_setup(
    profile: str | None,
    yes: bool,
    dry_run: bool,
    hosted: bool = False,
    hosted_opts: HostedOptions | None = None,
) -> int:
    """The whole flow; returns the exit code. Never raises on a step failure.

    Local: Hardware, Ollama, Pull models, Build models, Record profile, Doctor, Config.
    Hosted (--hosted, the hosted_only profile, or the choice when Ollama is missing): Hardware,
    Hosted provider, Record profile, Doctor, Config."""
    st = SetupState(
        yes=yes,
        dry_run=dry_run,
        native_url=_native_url(),
        hosted_opts=hosted_opts or HostedOptions(),
    )
    typer.echo("Kestrel setup" + (" (dry run: nothing will be executed)" if dry_run else ""))
    typer.echo("=" * 60)
    _run_step(st, "Hardware", lambda: step_hardware(st, profile, hosted))
    blocked = False  # Ollama unavailable or a model step failed: no live doctor probes
    if not st.profile:
        for name in ("Ollama", "Pull models", "Build models", "Record profile", "Doctor"):
            st.echo(SKIP, f"{name}: skipped, no profile chosen")
    else:
        if not st.hosted:
            ollama_ok = _run_step(st, "Ollama", lambda: step_ollama(st))
            if not st.hosted:
                blocked = ollama_ok is False
                for name, fn in (
                    ("Pull models", lambda: step_pull(st)),
                    ("Build models", lambda: step_build(st)),
                ):
                    if blocked:
                        st.echo(SKIP, f"{name}: skipped, Ollama is not available")
                        continue
                    if _run_step(st, name, fn) is False:
                        blocked = True  # never probe half-built models
        if st.hosted:
            hosted_ok = _run_step(st, "Hosted", lambda: step_hosted(st))
            blocked = hosted_ok is not True
        if blocked and st.hosted:
            st.echo(SKIP, "Record profile: skipped, hosted setup did not finish")
        else:
            _run_step(st, "Record profile", lambda: step_record(st))
        if blocked:
            why = "hosted setup did not finish" if st.hosted else "Ollama is not available"
            st.echo(SKIP, f"Doctor: skipped, {why}")
        else:
            _run_step(st, "Doctor", lambda: step_doctor(st))
    _run_step(st, "Config", lambda: step_seed(st))
    typer.echo("=" * 60)
    if st.failed:
        typer.echo(f"Setup did not finish: {', '.join(dict.fromkeys(st.failed))}. Fix and rerun.")
        return 1
    typer.echo("Setup complete. Next: `kestrel serve` (or launch the desktop app).")
    return 0


@setup_app.callback(invoke_without_command=True)
def setup(
    profile: str | None = typer.Option(
        None, "--profile", help="Use this hardware profile instead of the detected one."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run every step without asking."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print every step and the commands it would run; run nothing."
    ),
    hosted: bool = typer.Option(
        False,
        "--hosted",
        help="No local model: run every tier on a hosted provider under your own key.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="With --hosted: openrouter (default), openmodel, anthropic or openai.",
    ),
    free: bool = typer.Option(
        False, "--free", help="With --hosted: offer only models priced at zero (OpenRouter)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="With --hosted: this provider model id for every tier."
    ),
    model_tier1: str | None = typer.Option(
        None, "--model-tier1", help="With --hosted: the tier1 (and trigger) model id."
    ),
    model_tier2: str | None = typer.Option(
        None, "--model-tier2", help="With --hosted: the tier2 (and tier3, frontier) model id."
    ),
    model_docsel: str | None = typer.Option(
        None, "--model-docsel", help="With --hosted: the document selection model id."
    ),
    accept_hosted_disclosure: bool = typer.Option(
        False,
        ACCEPT_FLAG,
        help=(
            "With --hosted: accept the Hosted mode disclosure without the prompt (what each "
            "tier sends is still printed). --yes never accepts it for you."
        ),
    ),
) -> None:
    """Guided setup: detect hardware, install or find Ollama (or set up a hosted provider), pull
    and build models, run doctor."""
    opts = HostedOptions(
        provider=provider,
        free=free,
        model=model,
        model_tier1=model_tier1,
        model_tier2=model_tier2,
        model_docsel=model_docsel,
        accept_disclosure=accept_hosted_disclosure,
    )
    raise typer.Exit(code=run_setup(profile, yes, dry_run, hosted=hosted, hosted_opts=opts))
