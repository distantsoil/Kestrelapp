"""What the Models page (and Model check) shows, as plain words (D-UI-105, D-UI-147).

Pure view-building: no route, no template. Friendly model names, the Scoring steps rows, the
Health check rows and the hardware chips are computed here so the templates stay layout only and
the words have one home. Every rate carries its n and anything under five ratings reads "Too few
ratings yet (n)" (hard rule 6)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kestrel.analytics.models_history import ModelRow

MIN_N = 5
# About 500 words a page, about 1.35 tokens a word: a "page" for the Can read column.
TOKENS_PER_PAGE = 680

# The five scoring steps, in the order every role goes through them (SPEC-INTERFACES D-UI-105).
# key is the setting name; tier is the evaluation tier number (None: no model).
STEPS: tuple[dict[str, Any], ...] = (
    {"key": "rules", "tier": None, "code": "T0", "tone": "t0", "name": "Rules",
     "does": "Your filters: title, place, pay. No AI."},
    {"key": "tier1", "tier": 1, "code": "T1", "tone": "t1", "name": "Quick screen",
     "does": "Reads the start; sets aside clear misses."},
    {"key": "tier2", "tier": 2, "code": "T2", "tone": "t2", "name": "Rating",
     "does": "Reads the whole posting; scores the fit."},
    {"key": "docsel", "tier": None, "code": "Docs", "tone": "doc", "name": "Resume picking",
     "does": "Uses only your own resume lines."},
    {"key": "tier3", "tier": 3, "code": "T3", "tone": "t3", "name": "Deeper read",
     "does": "When you ask: checks role and company.", "asks": True},
)
STEP_BY_KEY = {s["key"]: s for s in STEPS}
CHANGEABLE = ("tier1", "tier2", "docsel", "tier3")

# What each step reads, in words (the budget numbers sit in the tooltip).
READS = {
    "tier1": "Start of posting",
    "tier2": "Whole posting",
    "docsel": "Posting and resume",
    "tier3": "Posting and notes",
}


def friendly_model(model: str | None, *, base: str | None = None) -> str:
    """A model as the operator knows it. Kestrel's own `kestrel-<step>` models show the model they
    are built from (`base`, from the hardware profile) or, with none known, "Kestrel's model for
    this step"; hosted ids drop the vendor prefix ("openai/gpt-4o-mini" is "gpt-4o-mini");
    `:latest` is dropped."""
    name = (model or "").strip()
    if not name:
        return "No model"
    if name.startswith("kestrel-"):
        return base or "Kestrel's model for this step"
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    return name.removesuffix(":latest")


def model_billions(tag: str | None) -> str:
    """"14 billion" from "qwen3:14b" or "qwen3:30b-a3b"; "" when the tag names no size."""
    m = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", (tag or "").lower())
    return f"{m.group(1)} billion" if m else ""


def pages(tokens: int) -> int:
    return max(1, round(tokens / TOKENS_PER_PAGE))


@dataclass
class Fit:
    label: str
    tone: str  # good | warn | muted | info
    n: int = 0


def fit_with_you(rows: list[ModelRow], tier: int | None, model: str, better: bool) -> Fit:
    """How well `model` at `tier` matches the operator's own calls, with its count. Under five
    labeled ratings it says so (hard rule 6)."""
    if tier is None:
        return Fit("Not needed", "muted")
    mine = [r for r in rows if r.tier == tier and r.model == model]
    n = max((r.n_labeled for r in mine), default=0)
    if n < MIN_N:
        return Fit(f"Too few ratings ({n})", "muted", n)
    best = max((r for r in mine if r.agreement_rate is not None),
               key=lambda r: r.agreement_rate or 0, default=None)
    if better:
        return Fit(f"Better match available ({n})", "info", n)
    if best is None or best.agreement_rate is None:
        return Fit(f"Too few ratings ({n})", "muted", n)
    pct = round(best.agreement_rate * 100)
    return Fit(f"{'Good' if pct >= 70 else 'Fair'} match, {pct}% of {n}",
               "good" if pct >= 70 else "warn", n)


# ------------------------------------------------------------------------------- health check
HEALTH_ROWS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # key, title, plain sentence when fine, doctor row-name prefixes that feed it
    ("engine", "AI engine running", "Ollama is running on this computer", ("Ollama reachable",)),
    ("models", "Models downloaded", "Everything the steps need is here", ("Models present",)),
    ("room", "Every step can read enough",
     "Tested by hiding a word near the end of a long posting", ("num_ctx ", "needle ")),
    ("thinking", "Thinking turned off", "Saves time on every role", ("thinking off ",)),
    ("format", "Answers in the right format", "Each step returns data Kestrel can read",
     ("schema honored ",)),
)


def health_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The five checks of the preflight in plain words, each pass, fail or not run yet.
    `report` is the persisted doctor report (setup_state.load_report)."""
    found: list[dict[str, Any]] = []
    results = [r for r in (report or {}).get("results", []) if isinstance(r, dict)]
    for key, title, ok_text, prefixes in HEALTH_ROWS:
        mine = [r for r in results if str(r.get("name", "")).startswith(prefixes)]
        failed = [r for r in mine if r.get("status") == "FAIL"]
        warned = [r for r in mine if r.get("status") == "WARN"]
        if not mine:
            state, detail = "none", "Not checked yet"
        elif failed:
            state, detail = "fail", _plain_detail(failed[0])
        elif warned:
            state, detail = "warn", _plain_detail(warned[0])
        else:
            state, detail = "ok", ok_text
        found.append({
            "key": key, "title": title, "state": state, "detail": detail,
            "tech": "; ".join(f"{r.get('name')}: {r.get('detail', '')}" for r in mine),
            "fixable": state in ("fail", "warn") and key in ("room", "thinking", "format"),
        })
    return found


def _plain_detail(row: dict[str, Any]) -> str:
    text = str(row.get("remediation") or row.get("detail") or "").strip()
    return (text[:140] + "…") if len(text) > 140 else (text or "This check did not pass")


def too_small_tiers(report: dict[str, Any] | None) -> set[str]:
    """Steps whose model failed the room-to-read test (num_ctx or the hidden word)."""
    out: set[str] = set()
    for r in (report or {}).get("results", []):
        if not isinstance(r, dict) or r.get("status") != "FAIL":
            continue
        name = str(r.get("name", ""))
        m = re.search(r"(tier\d|docsel)", name)
        if m and name.startswith(("num_ctx ", "needle ")):
            out.add(m.group(1))
    return out


# ------------------------------------------------------------------------------- the computer
def hardware_chips(hw: Any) -> list[str]:
    """Detected hardware as short chips: system, graphics, memory."""
    os_name = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
        getattr(hw, "os", ""), "This computer")
    chips = [os_name]
    if getattr(hw, "gpu_vendor", None) == "nvidia" and hw.vram_gb is not None:
        chips.append(f"NVIDIA graphics card, {hw.vram_gb:g} GB")
    elif getattr(hw, "apple_silicon", False):
        chips.append("Apple silicon")
    else:
        chips.append("No supported graphics card")
    if getattr(hw, "ram_gb", None) is not None:
        chips.append(f"{hw.ram_gb:g} GB memory")
    return chips


def fit_tag(size_gb: float | None, hw: Any) -> tuple[str, str]:
    """(label, tone) for a local model against the machine's memory: Fits well, Tight, Too big."""
    mem = getattr(hw, "vram_gb", None) or (
        getattr(hw, "ram_gb", None) if getattr(hw, "apple_silicon", False) else None
    )
    if size_gb is None:
        return "Size unknown", "muted"
    if mem is None:
        ram = getattr(hw, "ram_gb", None)
        if ram is None:
            return "Size unknown", "muted"
        # CPU only: it runs from memory, slowly.
        return ("Fits, but slow" if size_gb * 1.3 <= ram else "Too big"), (
            "warn" if size_gb * 1.3 <= ram else "bad")
    if size_gb * 1.2 <= mem:
        return "Fits well", "good"
    if size_gb <= mem:
        return "A tight fit", "warn"
    return "Too big for this computer", "bad"


def run_where(choice: str, tier: str, online_cfg: bool) -> bool:
    """Whether a step runs online: under Online every AI step does, under Both only Deeper read,
    and the embedding model always stays local. `online_cfg` is what the step's backend says."""
    if tier in ("rules", "embed"):
        return False
    if choice == "hosted":
        return True
    if choice == "both":
        return tier == "tier3" or online_cfg
    return online_cfg
