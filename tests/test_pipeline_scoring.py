"""Post-processing overrides (SPEC.md 7.3): weighted score, needs_review divergence, blocker and
legitimacy forcing, the work-auth blocker, and the threshold-derived verdict beside the model's."""

from __future__ import annotations

from kestrel.config import get_settings
from kestrel.pipeline import scoring

_EMP_DIMS = {
    "role_fit": 4.0,
    "domain_match": 4.0,
    "company_context": 4.0,
    "comp_logistics": 4.0,
    "trajectory": 4.0,
}


def test_weighted_score_uses_track_weights():
    settings = get_settings()
    weights = scoring.weights_for("employment", settings)
    # default weights sum to 1.0, so an all-4.0 vector weights to exactly 4.0
    assert scoring.weighted_score(_EMP_DIMS, weights) == 4.0


def test_verdict_from_score_thresholds():
    settings = get_settings()
    assert scoring.verdict_from_score(4.0, "employment", settings) == "apply"
    assert scoring.verdict_from_score(4.0, "consulting", settings) == "pursue"
    assert scoring.verdict_from_score(3.5, "employment", settings) == "consider"
    assert scoring.verdict_from_score(2.9, "employment", settings) == "pass"


def test_needs_review_on_divergence():
    settings = get_settings()
    # holistic 5.0 but dimensions weight to 1.0 -> |5-1| > 0.75
    low = dict.fromkeys(_EMP_DIMS, 1.0)
    r = scoring.post_process(
        track="employment", dimension_scores=low, holistic=5.0, model_verdict="apply",
        blockers=[], legitimacy_status="clean", work_auth_signal="not_mentioned",
        requires_sponsorship=False, settings=settings,
    )
    assert r.needs_review is True
    assert r.weighted == 1.0


def test_blocker_forces_pass_and_preserves_score():
    settings = get_settings()
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.5, model_verdict="apply",
        blockers=[{"kind": "location", "detail": "onsite NYC"}], legitimacy_status="clean",
        work_auth_signal="not_mentioned", requires_sponsorship=False, settings=settings,
    )
    assert r.verdict == "pass"
    assert r.displayed_score == 4.5  # the model's score is preserved for reference
    assert r.forced_by and "blocker" in r.forced_by


def test_legitimacy_reject_forces_pass():
    settings = get_settings()
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.5, model_verdict="apply",
        blockers=[], legitimacy_status="reject", work_auth_signal="not_mentioned",
        requires_sponsorship=False, settings=settings,
    )
    assert r.verdict == "pass"
    assert r.forced_by and "legitimacy_reject" in r.forced_by


def test_legitimacy_suspect_caps_displayed_score():
    settings = get_settings()
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.8, model_verdict="apply",
        blockers=[], legitimacy_status="suspect", work_auth_signal="not_mentioned",
        requires_sponsorship=False, settings=settings,
    )
    assert r.displayed_score == 3.5  # capped
    assert r.verdict == "consider"  # 3.5 falls in the consider band


def test_work_auth_adds_blocker_when_sponsorship_required():
    settings = get_settings()
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.5, model_verdict="apply",
        blockers=[], legitimacy_status="clean", work_auth_signal="explicit_no_sponsorship",
        requires_sponsorship=True, settings=settings,
    )
    assert any(b["kind"] == "work_authorization" for b in r.blockers)
    assert r.verdict == "pass"


def test_work_auth_no_blocker_when_sponsorship_not_required():
    settings = get_settings()
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.5, model_verdict="apply",
        blockers=[], legitimacy_status="clean", work_auth_signal="explicit_no_sponsorship",
        requires_sponsorship=False, settings=settings,
    )
    assert r.blockers == []
    assert r.verdict == "apply"


def test_verdict_disagreement_flagged():
    settings = get_settings()
    # model says apply, but a blocker forces pass -> disagreement
    r = scoring.post_process(
        track="employment", dimension_scores=_EMP_DIMS, holistic=4.5, model_verdict="apply",
        blockers=[{"kind": "comp", "detail": "below floor"}], legitimacy_status="clean",
        work_auth_signal="not_mentioned", requires_sponsorship=False, settings=settings,
    )
    assert r.verdict_model == "apply"
    assert r.verdict == "pass"
    assert r.verdict_disagreement is True
