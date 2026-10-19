"""The benchmark harness (SPEC.md 8.6). Fully offline: a FakeBenchClient (same `.settings` +
async `.complete()` -> ChatResult shape as kestrel.llm.client.LLMClient) stands in for Ollama, so
metric computation (first-attempt validity, verdict agreement, mean absolute score error,
items/minute, the 500-item projection) is verified against a tiny in-memory gold list with no
network call and no live job board, ATS, or model."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kestrel.bench.harness import (
    BenchArgError,
    BenchVariant,
    GoldEntry,
    GoldSetError,
    GoldSetMissing,
    build_variants,
    goldset_label,
    load_goldset,
    render_table,
    run_bench,
    validate_bench_args,
)
from kestrel.config import load_settings
from kestrel.llm.client import ChatResult

GOOD_MODEL = "bench-good"
WRONG_MODEL = "bench-wrong"
FLAKY_MODEL = "bench-flaky"
BAD_MODEL = "bench-bad"

_MARK_RE = re.compile(r"\[MARK:([\d.]+):(\w+)\]")


# ============================================================================= fixture data
def _write_goldset(gold_dir: Path, track: str, rows: list[dict]) -> None:
    path = gold_dir / f"goldset.{track}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _employment_rows() -> list[dict]:
    return [
        {
            "track": "employment", "title": "VP of IT", "org": "Acme Health",
            "description_md": "[MARK:4.5:apply] A VP of IT role at a nonprofit health system.",
            "location": "Remote, US", "comp_min": 200000, "comp_max": 230000,
            "remote_type": "remote", "gold_verdict": "apply", "gold_score": 4.5,
            "placeholder": True,
        },
        {
            "track": "employment", "title": "IT Director", "org": "Beta Manufacturing",
            "description_md": "[MARK:1.5:pass] An IT director role that is a poor fit.",
            "location": "Columbus, OH", "comp_min": 120000, "comp_max": 140000,
            "remote_type": "hybrid", "gold_verdict": "pass", "gold_score": 1.5,
            "placeholder": True,
        },
    ]


def _consulting_rows() -> list[dict]:
    return [
        {
            "track": "consulting", "title": "Fractional CIO", "org": "Hearthstone Capital",
            "description_md": "[MARK:4.6:pursue] A post-acquisition IT integration engagement.",
            "location": "Remote, US", "comp_min": 1500, "comp_max": 1800,
            "remote_type": "remote", "gold_verdict": "pursue", "gold_score": 4.6,
            "placeholder": True,
        },
        {
            "track": "consulting", "title": "Fractional CTO", "org": "Wrayburn Health",
            "description_md": "[MARK:1.8:pass] A poorly scoped, speculative engagement.",
            "location": "Nashville, TN", "comp_min": 1200, "comp_max": 1400,
            "remote_type": "onsite", "gold_verdict": "pass", "gold_score": 1.8,
            "placeholder": True,
        },
    ]


@pytest.fixture
def gold_dir(tmp_path: Path) -> Path:
    _write_goldset(tmp_path, "employment", _employment_rows())
    _write_goldset(tmp_path, "consulting", _consulting_rows())
    return tmp_path


# ============================================================================= fake body builders
def _dim(score: float) -> dict:
    return {"score": score, "why": "reasoned"}


def _valid_employment_body(score: float, verdict: str) -> str:
    dim = _dim(score)
    return json.dumps({
        "blocks": {
            "a_role_summary": {"what_this_actually_is": "An IT leadership role."},
            "b_fit": {"matches": [], "gaps": []},
            "c_level": {"relative_level": "at", "scope_read": "runs the function"},
            "d_comp": {
                "estimate_confidence": "medium", "estimate_basis": "corpus",
                "vs_floor": "above_floor", "geo_adjustment_risk": False,
            },
            "e_angle": {"lead_with": "track record", "jd_citations": []},
            "f_interview": [],
        },
        "dimensions": {
            "role_fit": dim, "domain_match": dim, "company_context": dim,
            "comp_logistics": dim, "trajectory": dim,
        },
        "holistic": score,
        "verdict": verdict,
        "headline_reason": "fit assessment",
        "blockers": [],
        "legitimacy": {"status": "clean", "confidence": 0.9, "signals": []},
        "work_auth_signal": "not_mentioned",
    })


def _valid_consulting_body(score: float, verdict: str) -> str:
    dim = _dim(score)
    return json.dumps({
        "blocks": {
            "a_situation": {
                "stated": "s", "inferred": "i", "speculative": "sp", "timing_read": "early",
            },
            "b_thesis": {
                "engagement": "post-acquisition integration", "service_lines": [], "phases": [],
                "estimated_days": 30, "estimated_weeks": 6,
            },
            "c_buyers": {
                "influencers": [], "blockers": [], "access_difficulty": "moderate",
                "access_path": "warm intro",
            },
            "d_economics": {
                "confidence": "medium", "basis": "corpus", "renewal_likelihood": "medium",
                "vs_floor": "above_floor",
            },
            "e_approach": {
                "opening_move": "note the trigger", "to_whom": "CFO", "referencing": "the filing",
                "channel": "warm_intro", "why_now_line": "window is open",
            },
            "f_delivery": {"solo_feasible": True, "gaps": [], "effort_shape": "2 days/week"},
            "g_risk": {"status": "clean", "signals": []},
        },
        "dimensions": {
            "engagement_fit": dim, "buyer_access": dim, "economics": dim,
            "delivery_feasibility": dim, "strategic_value": dim,
        },
        "holistic": score,
        "verdict": verdict,
        "headline_reason": "clear need, reachable buyer",
        "blockers": [],
        "legitimacy": {"status": "clean", "confidence": 0.9, "signals": []},
    })


def _valid_body(consulting: bool, score: float, verdict: str) -> str:
    return _valid_consulting_body(score, verdict) if consulting else _valid_employment_body(
        score, verdict
    )


class FakeBenchClient:
    """Drop-in for LLMClient: same `.settings` and async `.complete()` -> ChatResult shape. Behavior
    varies by model name (read off `self.settings.llm.tiers[tier].model`, exactly how the real
    harness routes a bench variant), so a multi-model comparison produces genuinely different
    metrics with no network call. The gold entry each call answers is identified by a `[MARK:score:
    verdict]` token the test embeds in that entry's description_md, which survives rendering into
    the prompt's `{description_truncated_to_max_jd_tokens}` placeholder."""

    def __init__(self, settings) -> None:
        self.settings = settings

    async def complete(
        self, *, tier, messages, schema=None, temperature=None, max_tokens=None,
        schema_name: str = "response", timeout: float = 180.0, allow_external: bool = False,
    ) -> ChatResult:
        model = self.settings.llm.tiers[tier].model
        text = "\n".join(m.get("content", "") for m in messages)
        is_repair = any(m.get("role") == "assistant" for m in messages)
        consulting = "consulting" in schema_name
        m = _MARK_RE.search(text)
        marked_score, marked_verdict = (float(m.group(1)), m.group(2)) if m else (3.0, "consider")

        if model == BAD_MODEL:
            content = "not valid json, sorry"
        elif model == FLAKY_MODEL:
            content = "still not valid json" if not is_repair else _valid_body(
                consulting, marked_score, marked_verdict
            )
        elif model == WRONG_MODEL:
            # Deliberately wrong regardless of the marker: exercises disagreement + nonzero MAE.
            content = _valid_body(consulting, 2.5, "pass")
        else:  # GOOD_MODEL (or any other name): answer exactly what the gold entry expects
            content = _valid_body(consulting, marked_score, marked_verdict)

        return ChatResult(
            content=content, prompt_tokens=111, completion_tokens=222, latency_ms=15, model=model
        )


def _make_client(settings):
    return FakeBenchClient(settings)


# ============================================================================= validate_bench_args
def test_validate_bench_args_requires_at_least_one_model():
    with pytest.raises(BenchArgError):
        validate_bench_args([], [])


def test_validate_bench_args_rejects_both_axes_varying():
    with pytest.raises(BenchArgError):
        validate_bench_args(["a", "b"], ["evaluate/v1", "evaluate/v2"])


@pytest.mark.parametrize(
    "models,versions",
    [(["a"], []), (["a", "b"], []), (["a"], ["evaluate/v1", "evaluate/v2"]), (["a"], ["v1"])],
)
def test_validate_bench_args_allows_one_axis_varying(models, versions):
    validate_bench_args(models, versions)  # must not raise


# ============================================================================= build_variants
def test_build_variants_default_fans_models_across_both_tracks():
    variants = build_variants(["a", "b"], None)
    assert variants["employment"] == [
        BenchVariant(track="employment", model="a", prompt_version="evaluate/v1"),
        BenchVariant(track="employment", model="b", prompt_version="evaluate/v1"),
    ]
    assert variants["consulting"] == [
        BenchVariant(track="consulting", model="a", prompt_version="evaluate_consulting/v1"),
        BenchVariant(track="consulting", model="b", prompt_version="evaluate_consulting/v1"),
    ]


def test_build_variants_routes_prompt_versions_to_their_own_track():
    variants = build_variants(["m"], ["evaluate/v1", "evaluate_consulting/v1"])
    assert variants["employment"] == [
        BenchVariant(track="employment", model="m", prompt_version="evaluate/v1")
    ]
    assert variants["consulting"] == [
        BenchVariant(track="consulting", model="m", prompt_version="evaluate_consulting/v1")
    ]


def test_build_variants_single_version_keeps_every_model():
    # Regression: comparing 2+ models at ONE fixed version is a MODEL comparison. The version is
    # not the varying axis, so both models must run (an earlier bug kept only models[0]). The
    # explicit version applies to its own track; the other track uses its default.
    variants = build_variants(["a", "b"], ["evaluate/v1"])
    assert variants["employment"] == [
        BenchVariant(track="employment", model="a", prompt_version="evaluate/v1"),
        BenchVariant(track="employment", model="b", prompt_version="evaluate/v1"),
    ]
    assert variants["consulting"] == [
        BenchVariant(track="consulting", model="a", prompt_version="evaluate_consulting/v1"),
        BenchVariant(track="consulting", model="b", prompt_version="evaluate_consulting/v1"),
    ]


def test_build_variants_unknown_prompt_version_raises():
    with pytest.raises(BenchArgError):
        build_variants(["m"], ["not/a/real/version"])


def test_build_variants_rejects_non_evaluation_prompt_version():
    with pytest.raises(BenchArgError):
        build_variants(["m"], ["triage/v1"])  # a real prompt, but not a tier-2 evaluation prompt


# ============================================================================= load_goldset
def test_load_goldset_missing_file_raises_clear_error(tmp_path: Path):
    with pytest.raises(GoldSetMissing) as exc_info:
        load_goldset("employment", gold_dir=tmp_path)
    message = str(exc_info.value)
    assert "no gold set" in message
    assert "employment" in message


def test_load_goldset_malformed_line_raises_gold_set_error(tmp_path: Path):
    path = tmp_path / "goldset.employment.jsonl"
    path.write_text("{not valid json\n", encoding="utf-8")
    with pytest.raises(GoldSetError):
        load_goldset("employment", gold_dir=tmp_path)


def test_load_goldset_parses_the_tiny_fixture(gold_dir: Path):
    entries = load_goldset("employment", gold_dir=gold_dir)
    assert len(entries) == 2
    assert all(isinstance(e, GoldEntry) for e in entries)
    assert entries[0].gold_verdict == "apply"
    assert entries[1].gold_score == 1.5


def test_load_goldset_reads_the_real_shipped_placeholder_files():
    """Sanity cross-check against the actual evals/goldset.<track>.jsonl shipped in the repo
    (SPEC.md 8.6): the harness's loader must accept the same shape the sibling gold-set build step
    produces, not a private assumption of this test file."""
    real_evals_dir = Path(__file__).resolve().parent.parent / "evals"
    if not (real_evals_dir / "goldset.employment.jsonl").exists():
        pytest.skip("shipped evals/goldset.employment.jsonl not present in this checkout")
    entries = load_goldset("employment", gold_dir=real_evals_dir)
    assert len(entries) == 20
    assert all(e.placeholder for e in entries)


# ============================================================================= run_bench metrics
async def test_run_bench_good_model_perfect_agreement_and_zero_mae(gold_dir: Path):
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[GOOD_MODEL], gold_dir=gold_dir, make_client=_make_client,
    )
    assert not report.skipped_tracks
    assert len(report.variants) == 2  # one per track
    by_track = {r.track: r for r in report.variants}

    emp = by_track["employment"]
    assert emp.model == GOOD_MODEL
    assert emp.prompt_version == "evaluate/v1"
    assert emp.n_items == 2
    assert emp.n_parsed == 2
    assert emp.first_attempt_validity_rate == 1.0
    assert emp.exact_verdict_agreement_rate == 1.0
    assert emp.mean_absolute_score_error == 0.0
    assert emp.sufficient_for_agreement is False  # n_parsed=2 < 5 (hard rule 6)
    assert emp.items_per_minute is not None and emp.items_per_minute > 0
    assert emp.projected_minutes_for_500 is not None and emp.projected_minutes_for_500 > 0
    assert emp.mean_prompt_tokens == 111
    assert emp.mean_completion_tokens == 222

    con = by_track["consulting"]
    assert con.prompt_version == "evaluate_consulting/v1"
    assert con.exact_verdict_agreement_rate == 1.0
    assert con.mean_absolute_score_error == 0.0


async def test_run_bench_wrong_model_partial_agreement_and_nonzero_mae(gold_dir: Path):
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[WRONG_MODEL], gold_dir=gold_dir, make_client=_make_client,
    )
    emp = next(r for r in report.variants if r.track == "employment")
    # WRONG always answers holistic=2.5/"pass": disagrees with the "apply" gold entry (4.5),
    # agrees by coincidence with the "pass" gold entry (1.5) -> 1/2 exact-verdict agreement.
    assert emp.exact_verdict_agreement_rate == 0.5
    assert emp.mean_absolute_score_error == pytest.approx((abs(2.5 - 4.5) + abs(2.5 - 1.5)) / 2)


async def test_run_bench_flaky_model_low_first_attempt_validity_but_recovers(gold_dir: Path):
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[FLAKY_MODEL], gold_dir=gold_dir, make_client=_make_client,
    )
    emp = next(r for r in report.variants if r.track == "employment")
    assert emp.first_attempt_validity_rate == 0.0  # every attempt-1 is invalid JSON
    assert emp.n_parsed == 2  # but the one repair attempt (hard rule 3) recovers both
    assert emp.exact_verdict_agreement_rate == 1.0
    assert emp.n_quarantined == 0


async def test_run_bench_bad_model_quarantines_never_crashes(gold_dir: Path):
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[BAD_MODEL], gold_dir=gold_dir, make_client=_make_client,
    )
    emp = next(r for r in report.variants if r.track == "employment")
    assert emp.first_attempt_validity_rate == 0.0
    assert emp.n_parsed == 0
    assert emp.n_quarantined == emp.n_items == 2
    assert emp.exact_verdict_agreement_rate is None
    assert emp.mean_absolute_score_error is None
    assert emp.sufficient_for_agreement is False


async def test_run_bench_multi_model_comparison_in_one_call(gold_dir: Path):
    """The `kestrel bench --models a,b` shape: two models, prompt version fixed at each track's
    default, in a single run -- exactly the axis SPEC.md 8.6 allows to vary."""
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[GOOD_MODEL, BAD_MODEL], gold_dir=gold_dir,
        make_client=_make_client,
    )
    assert len(report.variants) == 4  # 2 tracks x 2 models
    models_seen = {(r.track, r.model) for r in report.variants}
    assert models_seen == {
        ("employment", GOOD_MODEL), ("employment", BAD_MODEL),
        ("consulting", GOOD_MODEL), ("consulting", BAD_MODEL),
    }


async def test_run_bench_missing_gold_set_for_one_track_is_reported_not_raised(tmp_path: Path):
    """Only the consulting gold set exists; employment must be reported as skipped, never crash
    the whole bench run (the sibling gold-set build step may not have shipped both files yet)."""
    _write_goldset(tmp_path, "consulting", _consulting_rows())
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[GOOD_MODEL], gold_dir=tmp_path, make_client=_make_client,
    )
    assert "employment" in report.skipped_tracks
    assert "no gold set" in report.skipped_tracks["employment"]
    assert any(r.track == "consulting" for r in report.variants)
    assert not any(r.track == "employment" for r in report.variants)


async def test_run_bench_bad_args_raise_before_any_model_call(gold_dir: Path):
    settings = load_settings()
    with pytest.raises(BenchArgError):
        await run_bench(
            settings=settings, models=["a", "b"], prompt_versions=["evaluate/v1", "evaluate/v2"],
            gold_dir=gold_dir, make_client=_make_client,
        )


# ============================================================================= render_table
async def test_render_table_flags_insufficient_sample_and_lists_models(gold_dir: Path):
    settings = load_settings()
    report = await run_bench(
        settings=settings, models=[GOOD_MODEL, WRONG_MODEL], gold_dir=gold_dir,
        make_client=_make_client,
    )
    table = render_table(report.variants)
    assert GOOD_MODEL in table
    assert WRONG_MODEL in table
    assert "insufficient" in table  # n=2 per variant, under the hard-rule-6 floor of 5


def test_render_table_handles_no_variants():
    assert render_table([]) == "(no bench variants ran)"


# ============================================================================= answer-key lookup
def _row(title: str) -> dict:
    return {
        "track": "employment", "title": title, "org": "Acme", "description_md": "d",
        "gold_verdict": "keep", "gold_score": 80,
    }


def _point_evals_dir(monkeypatch, directory: Path) -> None:
    from kestrel import paths

    monkeypatch.setattr(paths, "evals_dir", lambda: directory)


def test_regenerated_goldset_wins_over_the_placeholder(tmp_path, monkeypatch):
    _point_evals_dir(monkeypatch, tmp_path)
    _write_goldset(tmp_path, "employment", [_row("placeholder")])
    (tmp_path / "goldset.employment.regenerated.jsonl").write_text(
        json.dumps(_row("mine")) + "\n", encoding="utf-8"
    )
    assert [e.title for e in load_goldset("employment")] == ["mine"]
    assert goldset_label("employment") == "your labels"


def test_placeholder_in_evals_dir_is_used_when_nothing_regenerated(tmp_path, monkeypatch):
    _point_evals_dir(monkeypatch, tmp_path)
    _write_goldset(tmp_path, "employment", [_row("placeholder")])
    assert [e.title for e in load_goldset("employment")] == ["placeholder"]
    assert goldset_label("employment") == "the shipped examples"


def test_packaged_goldset_is_used_when_evals_dir_is_empty(tmp_path, monkeypatch):
    """A frozen install: the data folder holds no gold set, so the copy inside the app is read."""
    _point_evals_dir(monkeypatch, tmp_path)
    for track in ("employment", "consulting"):
        entries = load_goldset(track)
        assert entries and all(e.placeholder for e in entries)
    assert goldset_label("employment") == "the shipped examples"


def test_gold_dir_override_has_no_packaged_fallback(tmp_path):
    with pytest.raises(GoldSetMissing):
        load_goldset("employment", gold_dir=tmp_path)


def test_packaged_goldsets_are_identical_to_the_repo_root_files():
    from importlib import resources

    root = Path(__file__).resolve().parent.parent / "evals"
    for track in ("employment", "consulting"):
        packaged = resources.files("kestrel").joinpath(
            "resources", "evals", f"goldset.{track}.jsonl"
        )
        assert packaged.read_bytes() == (root / f"goldset.{track}.jsonl").read_bytes()


def test_bench_output_names_the_answer_key(tmp_path, monkeypatch):
    """`kestrel bench` prints which answer key it used, and only labels it a placeholder when it
    is not the operator's own."""
    import inspect

    from kestrel import cli

    _point_evals_dir(monkeypatch, tmp_path)
    assert cli._goldset_is_placeholder("employment") is True  # packaged examples
    (tmp_path / "goldset.employment.regenerated.jsonl").write_text(
        json.dumps(_row("mine")) + "\n", encoding="utf-8"
    )
    assert cli._goldset_is_placeholder("employment") is False
    assert "answer key: {goldset_label(track)}" in inspect.getsource(cli._bench)
