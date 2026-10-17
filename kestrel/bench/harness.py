"""The benchmark harness (SPEC.md 8.6; SPEC-INTERFACES.md 7 `kestrel bench`, also a UI page at
kestrel/web/pages/bench.py).

Runs the tier-2 evaluation prompt (P2 employment / P2C consulting -- the same prompt tier2.py runs
in production) against the operator's gold set, for one or more Ollama models at a FIXED prompt
version, or for one model across several prompt versions -- **exactly one variable changes per
comparison** (`validate_bench_args` rejects a call that would vary both). Reports, per model and
per track: first-attempt JSON validity rate, mean tokens in/out, mean latency, items/minute at the
configured concurrency, exact-verdict agreement with the gold set, mean absolute score error, and a
projected wall-clock for a 500-item run.

Structured output or failure (hard rule 3): every gold item goes through `llm.guard.run_prompt`,
never a raw client call. A parse failure is a datapoint (first_attempt_valid=False, possibly
quarantined), never a crash and never silently counted as valid.

The harness never calls a live job board, ATS, or SEC endpoint: each gold entry is already a fully
formed synthetic opportunity read from `evals/goldset.<track>.jsonl` (kestrel.paths.evals_dir()),
and the only network call this module makes is the model completion itself. No database session is
opened: the operator's real profile (kestrel.pipeline.profile.load_profile) is read from config, but
comp-corpus intelligence and org history are deliberately omitted (comp_intel=None, org=None) so a
bench run has zero dependency on the app database.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict

from kestrel import paths
from kestrel.config import Settings
from kestrel.db.enums import TRACKS
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_prompt
from kestrel.llm.prompts import PromptSpec, load_prompt
from kestrel.llm.schemas import P2CEval, P2Eval
from kestrel.pipeline import scoring
from kestrel.pipeline.context import build_tier2_values, render_user
from kestrel.pipeline.profile import load_profile

# The prompt id each track's gold set is benchmarked against (matches
# kestrel.pipeline.tier2._VERSION).
DEFAULT_PROMPT_VERSION: dict[str, str] = {
    "employment": "evaluate/v1",
    "consulting": "evaluate_consulting/v1",
}
# Consulting comp figures in the gold set are day rates; employment figures are annual salary.
_COMP_INTERVAL: dict[str, str] = {"employment": "year", "consulting": "day"}
_PROJECTED_N = 500
_MIN_SAMPLE = 5  # hard rule 6: fewer than five observations is stated as insufficient, never solid


# ============================================================================= errors
class BenchArgError(ValueError):
    """A `--models`/`--prompt-version` combination the harness refuses (SPEC.md 8.6: exactly one
    variable changes per comparison), or a prompt version bench cannot use."""


class GoldSetError(ValueError):
    """A gold-set file exists but a line failed to parse. Distinct from GoldSetMissing (absent
    file) so the CLI can report the two failure modes distinctly."""


class GoldSetMissing(FileNotFoundError):
    """No usable gold set for a track. Never crashes the harness -- the caller reports this
    message and moves on to the other track (a SIBLING build step may not have shipped this file,
    or a fresh checkout has not run `kestrel goldset regen` yet)."""

    def __init__(self, track: str, searched: Path) -> None:
        self.track = track
        self.searched = searched
        super().__init__(
            f"no gold set for track '{track}': {searched} does not exist. "
            f"Run `kestrel goldset regen --track {track}` once you have real labels, or ship the "
            f"evals/goldset.{track}.jsonl placeholder (SPEC.md 8.6)."
        )


# ============================================================================= gold set
class GoldEntry(BaseModel):
    """One line of evals/goldset.<track>.jsonl. Extra keys are ignored so the gold set (shipped
    placeholder or `kestrel goldset regen` output) can evolve without breaking the loader."""

    model_config = ConfigDict(extra="ignore")

    track: str
    title: str
    org: str
    description_md: str
    location: str = ""
    comp_min: float | None = None
    comp_max: float | None = None
    remote_type: str = "unknown"
    gold_verdict: str
    gold_score: float
    placeholder: bool = False


def load_goldset(track: str, *, gold_dir: Path | str | None = None) -> list[GoldEntry]:
    """Load evals/goldset.<track>.jsonl (one JSON object per line). `gold_dir` overrides the
    default location (kestrel.paths.evals_dir()) for tests. Raises GoldSetMissing if the file does
    not exist -- the caller must catch this, never let it crash a bench run -- and GoldSetError on
    a line that is not valid JSON or does not carry the required gold-entry fields."""
    directory = Path(gold_dir) if gold_dir is not None else paths.evals_dir()
    path = directory / f"goldset.{track}.jsonl"
    if not path.is_file():
        raise GoldSetMissing(track, path)

    entries: list[GoldEntry] = []
    with path.open(encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GoldSetError(f"{path}:{lineno}: invalid JSON ({exc})") from exc
            try:
                entries.append(GoldEntry.model_validate(data))
            except Exception as exc:  # pydantic.ValidationError, kept generic to avoid a 2nd import
                raise GoldSetError(f"{path}:{lineno}: {exc}") from exc
    return entries


# ============================================================================= variants
@dataclass(frozen=True)
class BenchVariant:
    """One thing being benchmarked for one track: a model at a prompt version. A comparison run is
    a list of variants that differ in EXACTLY one of (model, prompt_version)."""

    track: str
    model: str
    prompt_version: str

    @property
    def label(self) -> str:
        return f"{self.model} [{self.prompt_version}]"


def validate_bench_args(models: Sequence[str], prompt_versions: Sequence[str]) -> None:
    """SPEC.md 8.6: exactly one variable changes per comparison. Multiple models AND multiple
    prompt versions in the same call is rejected with a clear error; either alone (or neither, a
    single-model/default-version smoke run) is fine."""
    if not models:
        raise BenchArgError("--models is required: at least one Ollama model name to benchmark.")
    if len(models) > 1 and len(prompt_versions) > 1:
        raise BenchArgError(
            "Pass multiple --models OR multiple --prompt-version values, never both in the same "
            "run -- exactly one variable must change per comparison (SPEC.md 8.6)."
        )


# Models configured for a tier OTHER than the one bench measures. Benchmarking one of these and
# reporting its number beside a real tier-2 candidate is the exact error AFTERACTION 3.2 records:
# kestrel-tier1 is a fast triage model, and its 70% structured-output rate was read as a tier-1
# "quality signal" when it was really an off-label model on a placeholder gold set.
def off_tier_models(settings: Settings) -> dict[str, str]:
    """Model name -> the tier role it is actually configured for, for every non-tier2 role."""
    out: dict[str, str] = {}
    for role, tier in settings.llm.tiers.items():
        if role in ("tier2", "embed"):
            continue
        if tier.model:
            out.setdefault(tier.model, role)
    return out


@dataclass
class ModelWarning:
    """One reason a model's numbers are not comparable to a real tier-2 candidate."""

    model: str
    kind: str  # "off_tier" | "under_provisioned" | "unknown_context"
    detail: str


def check_model_fitness(
    models: Sequence[str], settings: Settings, *, client: LLMClient | None = None
) -> list[ModelWarning]:
    """Flag models whose bench numbers must NOT be read as comparable (AFTERACTION 3.2).

    Two failure modes, both of which previously produced a number that looked like a result:
      * off-tier: the model is configured for a different role (tier1 triage, docsel, trigger).
        Its prompt budget and its job are different, so a lower structured-output rate says nothing
        about its fitness and nothing about tier 2.
      * under-provisioned: its effective num_ctx is below tier 2's computed budget, so the input is
        silently truncated and the evaluation is quietly wrong (the platform trap in CLAUDE.md).

    Returns warnings rather than raising: the operator may deliberately want to see an off-tier
    number, they just must not be allowed to mistake it for a comparable one. The CLI refuses to
    print a comparison table without surfacing these first.
    """
    warnings: list[ModelWarning] = []
    off_tier = off_tier_models(settings)
    required = None
    try:
        from kestrel.doctor import required_num_ctx

        required = required_num_ctx("tier2", settings)
    except Exception:  # noqa: BLE001 - a missing budget must not block the bench
        required = None

    for model in models:
        role = off_tier.get(model) or off_tier.get(model.split(":")[0])
        if role:
            warnings.append(
                ModelWarning(
                    model,
                    "off_tier",
                    f"configured as the {role} model, not a tier-2 candidate: its numbers are not "
                    f"comparable to a tier-2 model and must not be reported as if they were",
                )
            )
        if client is None or required is None:
            continue
        try:
            effective = client.effective_num_ctx(model)
        except Exception as exc:  # noqa: BLE001 - report, never abort the run
            warnings.append(
                ModelWarning(model, "unknown_context", f"could not read num_ctx: {exc}")
            )
            continue
        if effective is None:
            warnings.append(
                ModelWarning(
                    model,
                    "unknown_context",
                    "no explicit num_ctx: Ollama will apply the model default, which is far below "
                    "what tier 2 sends. Build a derived model with PARAMETER num_ctx (SPEC 8.2)",
                )
            )
        elif effective < required:
            warnings.append(
                ModelWarning(
                    model,
                    "under_provisioned",
                    f"num_ctx {effective} is below tier 2's budget of {required}: long inputs are "
                    f"silently truncated, so the evaluation is wrong rather than merely worse",
                )
            )
    return warnings


def _track_of_version(version: str) -> str:
    """Which track's schema a prompt version belongs to, so --prompt-version can be routed without
    guessing. Raises BenchArgError for an unknown version or one that is not a tier-2 evaluation
    prompt (P2/P2C) -- bench only ever compares evaluation prompts."""
    try:
        spec = load_prompt(version)
    except FileNotFoundError as exc:
        raise BenchArgError(f"unknown --prompt-version '{version}': no such prompt file.") from exc
    if spec.model_cls is P2Eval:
        return "employment"
    if spec.model_cls is P2CEval:
        return "consulting"
    raise BenchArgError(
        f"--prompt-version '{version}' is not a tier-2 evaluation prompt (P2/P2C); bench only "
        "compares evaluate/evaluate_consulting versions."
    )


def build_variants(
    models: Sequence[str],
    prompt_versions: Sequence[str] | None,
    *,
    tracks: Sequence[str] = TRACKS,
) -> dict[str, list[BenchVariant]]:
    """The variants to run, keyed by track. Raises BenchArgError via validate_bench_args /
    _track_of_version on a bad combination -- never silently drops an operator-specified axis."""
    prompt_versions = list(prompt_versions or [])
    validate_bench_args(models, prompt_versions)
    out: dict[str, list[BenchVariant]] = {t: [] for t in tracks}

    # Branch on which axis actually VARIES, not merely on whether a version was given: with 2+
    # models and a single fixed version, the models are the axis and every model must run (the
    # earlier bug chose the version branch on any prompt_versions and dropped models[1:]).
    if len(prompt_versions) > 1:
        # Comparing prompt versions on exactly one model (validated above).
        model = models[0]
        for pv in prompt_versions:
            track = _track_of_version(pv)
            if track in out:
                out[track].append(BenchVariant(track=track, model=model, prompt_version=pv))
    else:
        # Comparing models (0 or 1 explicit version). Apply a single explicit version to its own
        # track; every other track uses its default version. NEVER drop a model.
        explicit = prompt_versions[0] if prompt_versions else None
        explicit_track = _track_of_version(explicit) if explicit else None
        for t in tracks:
            pv = explicit if explicit_track == t else DEFAULT_PROMPT_VERSION.get(t)
            if pv is None:
                continue
            for m in models:
                out[t].append(BenchVariant(track=t, model=m, prompt_version=pv))
    return out


# ============================================================================= synthetic context
def _synthetic_opportunity(entry: GoldEntry) -> SimpleNamespace:
    """A bench gold entry rendered into exactly the attributes build_tier2_values reads off a real
    Opportunity row (kestrel.pipeline.context). Never touches the database."""
    return SimpleNamespace(
        track=entry.track,
        opp_type="posting",
        org_name=entry.org,
        title=entry.title,
        location_raw=entry.location or "(unspecified)",
        remote_type=entry.remote_type or "unknown",
        employment_type=None,
        url="(bench harness synthetic entry)",
        description_md=entry.description_md,
        comp_min=entry.comp_min,
        comp_max=entry.comp_max,
        comp_currency="USD",
        comp_interval=_COMP_INTERVAL.get(entry.track, "year"),
        date_posted=None,
        event_date=None,
        trigger_data=None,
        seen_count=1,
        first_seen_at="unknown",
        last_seen_at="unknown",
        version=1,
        ghost_score=0.0,
    )


def _build_messages(
    spec: PromptSpec, entry: GoldEntry, profile: Any, settings: Settings
) -> list[dict]:
    opp = _synthetic_opportunity(entry)
    values = build_tier2_values(
        opp=opp,
        org=None,
        profile=profile,
        source_name="bench-harness",
        max_jd_tokens=settings.pipeline.max_jd_tokens,
        few_shot="",  # no operator labels to draw few-shot examples from for a synthetic gold item
        comp_intel=None,  # no DB comp corpus; the harness never touches the app database
    )
    user = render_user(
        spec.user,
        values,
        trigger_event=False,
        posted=True,
        org_signals="(no accumulated signals on this organization yet)",
    )
    return [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]


# ============================================================================= results
@dataclass
class ItemResult:
    index: int
    ok: bool
    first_attempt_valid: bool
    quarantined: bool
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    verdict: str | None
    score: float | None
    gold_verdict: str
    gold_score: float
    verdict_match: bool | None
    abs_score_error: float | None
    error: str | None = None


@dataclass
class VariantReport:
    track: str
    model: str
    prompt_version: str
    n_items: int
    n_parsed: int
    n_quarantined: int
    first_attempt_validity_rate: float
    mean_prompt_tokens: float | None
    mean_completion_tokens: float | None
    mean_latency_ms: float | None
    items_per_minute: float | None
    exact_verdict_agreement_rate: float | None
    mean_absolute_score_error: float | None
    sufficient_for_agreement: bool
    projected_minutes_for_500: float | None
    wall_clock_seconds: float
    items: list[ItemResult] = field(default_factory=list)


@dataclass
class BenchReport:
    variants: list[VariantReport]
    skipped_tracks: dict[str, str] = field(default_factory=dict)


ClientFactory = Callable[[Settings], Any]


def _default_client_factory(settings: Settings) -> LLMClient:
    return LLMClient(settings)


# ============================================================================= per-item run
async def _run_item(
    client: Any, spec: PromptSpec, entry: GoldEntry, profile: Any, *, index: int
) -> ItemResult:
    messages = _build_messages(spec, entry, profile, client.settings)
    result = await run_prompt(client, spec, messages)  # never raises (hard rule 3)

    verdict = score = None
    verdict_match = abs_err = None
    if result.ok and result.value is not None:
        value = result.value
        dim_scores = {
            name: getattr(value.dimensions, name).score for name in value.dimensions.model_fields
        }
        blockers = [{"kind": b.kind, "detail": b.detail} for b in value.blockers]
        work_auth = getattr(value, "work_auth_signal", None)
        requires_sponsorship = bool(getattr(profile, "requires_sponsorship", False))
        post = scoring.post_process(
            track=entry.track,
            dimension_scores=dim_scores,
            holistic=value.holistic,
            model_verdict=value.verdict,
            blockers=blockers,
            legitimacy_status=value.legitimacy.status,
            work_auth_signal=work_auth,
            requires_sponsorship=requires_sponsorship,
            settings=client.settings,
        )
        verdict = post.verdict
        score = post.displayed_score
        verdict_match = verdict == entry.gold_verdict
        abs_err = abs(score - entry.gold_score)

    return ItemResult(
        index=index,
        ok=result.ok,
        first_attempt_valid=result.ok and not result.repaired,
        quarantined=result.quarantined,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
        verdict=verdict,
        score=score,
        gold_verdict=entry.gold_verdict,
        gold_score=entry.gold_score,
        verdict_match=verdict_match,
        abs_score_error=abs_err,
        error=result.error,
    )


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _summarize(
    variant: BenchVariant, items: list[ItemResult], wall_seconds: float
) -> VariantReport:
    n = len(items)
    n_first_valid = sum(1 for it in items if it.first_attempt_valid)
    n_quarantined = sum(1 for it in items if it.quarantined)
    parsed = [it for it in items if it.verdict is not None]
    n_parsed = len(parsed)
    matches = sum(1 for it in parsed if it.verdict_match)
    errs = [it.abs_score_error for it in parsed if it.abs_score_error is not None]

    items_per_minute = (n / (wall_seconds / 60.0)) if wall_seconds > 0 and n else None
    return VariantReport(
        track=variant.track,
        model=variant.model,
        prompt_version=variant.prompt_version,
        n_items=n,
        n_parsed=n_parsed,
        n_quarantined=n_quarantined,
        first_attempt_validity_rate=(n_first_valid / n) if n else 0.0,
        mean_prompt_tokens=_mean([float(it.prompt_tokens) for it in items]),
        mean_completion_tokens=_mean([float(it.completion_tokens) for it in items]),
        mean_latency_ms=_mean([float(it.latency_ms) for it in items]),
        items_per_minute=items_per_minute,
        exact_verdict_agreement_rate=(matches / n_parsed) if n_parsed else None,
        mean_absolute_score_error=_mean(errs) if errs else None,
        sufficient_for_agreement=(n_parsed >= _MIN_SAMPLE),
        projected_minutes_for_500=(_PROJECTED_N / items_per_minute) if items_per_minute else None,
        wall_clock_seconds=wall_seconds,
        items=items,
    )


async def _run_variant(
    make_client: ClientFactory,
    base_settings: Settings,
    variant: BenchVariant,
    entries: list[GoldEntry],
    profile: Any,
    concurrency: int,
) -> VariantReport:
    settings_v = base_settings.model_copy(deep=True)
    settings_v.llm.tiers["tier2"] = settings_v.llm.tiers["tier2"].model_copy(
        update={"model": variant.model}
    )
    client = make_client(settings_v)
    spec = load_prompt(variant.prompt_version)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(i: int, entry: GoldEntry) -> ItemResult:
        async with semaphore:
            return await _run_item(client, spec, entry, profile, index=i)

    t0 = time.perf_counter()
    items = list(await asyncio.gather(*(_bounded(i, e) for i, e in enumerate(entries))))
    wall = time.perf_counter() - t0
    return _summarize(variant, items, wall)


# ============================================================================= orchestration
async def run_bench(
    *,
    settings: Settings,
    models: Sequence[str],
    prompt_versions: Sequence[str] | None = None,
    tracks: Sequence[str] = TRACKS,
    gold_dir: Path | str | None = None,
    make_client: ClientFactory = _default_client_factory,
) -> BenchReport:
    """Run every (model, gold entry) pair for every requested track and report per-variant metrics
    (SPEC.md 8.6). Raises BenchArgError for a bad --models/--prompt-version combination (a config
    problem, surfaced before any model is called); a missing gold set for one track is recorded in
    `BenchReport.skipped_tracks` and the run continues with whatever tracks DO have a gold set.
    """
    variants_by_track = build_variants(models, prompt_versions, tracks=tracks)

    reports: list[VariantReport] = []
    skipped: dict[str, str] = {}
    for track in tracks:
        variants = variants_by_track.get(track, [])
        if not variants:
            continue
        try:
            entries = load_goldset(track, gold_dir=gold_dir)
        except GoldSetMissing as exc:
            skipped[track] = str(exc)
            continue
        if not entries:
            skipped[track] = f"gold set for '{track}' is empty."
            continue

        profile = load_profile(track)
        concurrency = getattr(settings.concurrency, "tier2", 2)
        for variant in variants:
            reports.append(
                await _run_variant(make_client, settings, variant, entries, profile, concurrency)
            )
    return BenchReport(variants=reports, skipped_tracks=skipped)


# ============================================================================= reporting
def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "n/a"


def _fmt_num(x: float | None, digits: int = 0) -> str:
    return f"{x:.{digits}f}" if x is not None else "n/a"


def render_table(reports: list[VariantReport]) -> str:
    """The comparison table (SPEC.md 8.6): one row per (track, model, prompt_version). Sample
    sizes under five are always labeled insufficient, never presented as a solid comparison
    (hard rule 6)."""
    if not reports:
        return "(no bench variants ran)"

    headers = [
        "track", "model", "prompt", "n", "valid%", "tok_in", "tok_out", "latency_ms",
        "items/min", "agree%", "MAE", "proj_500(min)",
    ]
    rows: list[list[str]] = []
    for r in reports:
        agree = _fmt_pct(r.exact_verdict_agreement_rate) if r.sufficient_for_agreement else (
            f"insufficient (n={r.n_parsed})"
        )
        mae = (
            _fmt_num(r.mean_absolute_score_error, 2)
            if r.sufficient_for_agreement
            else "insufficient"
        )
        rows.append([
            r.track, r.model, r.prompt_version, str(r.n_items),
            _fmt_pct(r.first_attempt_validity_rate),
            _fmt_num(r.mean_prompt_tokens), _fmt_num(r.mean_completion_tokens),
            _fmt_num(r.mean_latency_ms), _fmt_num(r.items_per_minute, 1),
            agree, mae, _fmt_num(r.projected_minutes_for_500, 1),
        ])

    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)
