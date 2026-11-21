"""Legitimacy signals are checked against what ingestion knows (issue #208 round 7, D-SCORE-31).

INVARIANT: a legitimacy flag never caps a score (or rejects a role) on the model's word alone when
Kestrel's own data contradicts it. A contradicted signal is dropped; one the data cannot check
becomes a worth-checking line; the suspect cap applies only while a checked signal survives; the
stored status and signals always agree. Post-processing only: no prompt changes (D-SCORE-6).
Invented postings only; the two live cases are re-created with invented equivalents.
"""

from __future__ import annotations

from types import SimpleNamespace

from kestrel.config import get_settings
from kestrel.pipeline import scoring

_DIMS = dict.fromkeys(
    ("role_fit", "domain_match", "company_context", "comp_logistics", "trajectory"), 4.2)
TEAM = "You will lead a team of six planners and report to the head of operations."


def _opp(**over):
    base = dict(remote_type="onsite", country="US", state="TX", city="Dallas",
                location_raw="Dallas, TX", is_remote=False, title="Freight Planner",
                description_md=f"Plan inbound freight. {TEAM}", comp_min=None, comp_max=None,
                comp_source="none", seen_count=5, ghost_score=0.30)
    base.update(over)
    return SimpleNamespace(**base)


def _post(status, signals, opp, holistic=4.2, verdict="apply"):
    return scoring.post_process(
        track="employment", dimension_scores=_DIMS, holistic=holistic, model_verdict=verdict,
        blockers=[], legitimacy_status=status, work_auth_signal="not_mentioned",
        requires_sponsorship=False, settings=get_settings(), opp=opp, profile=None,
        legitimacy={"status": status, "confidence": "medium", "signals": list(signals)},
    )


def test_live_case_one_a_suspect_the_data_contradicts_is_not_capped():
    """Like the first live role: suspect for reposting, no pay/location/team and boilerplate,
    while Kestrel saw it five times in two days (ghost 0.30), the city is stated and the team
    is described. Two signals are contradicted; boilerplate cannot be checked."""
    r = _post("suspect", ["reposted_repeatedly", "no_comp_no_location_no_team",
                          "generic_boilerplate"], _opp())
    assert r.legitimacy_status == "clean" and r.legitimacy["signals"] == []
    assert r.legitimacy["model_status"] == "suspect"
    assert r.displayed_score == 4.2 and r.verdict == "apply"
    assert r.forced_by is None
    checks = {c["signal"]: c["check"] for c in r.legitimacy["signal_checks"]}
    assert checks == {"reposted_repeatedly": scoring.CONTRADICTED,
                      "no_comp_no_location_no_team": scoring.CONTRADICTED,
                      "generic_boilerplate": scoring.NEEDS_CONFIRMING}
    assert any("generic" in n for n in r.legitimacy_notes)


def test_live_case_two_clean_never_lists_signals():
    """Like the second live role: clean, yet listing reposting and no pay/location/team, while
    it was seen twice (ghost 0.00) with pay, place and team posted."""
    opp = _opp(seen_count=2, ghost_score=0.0, comp_min=95000, comp_max=110000,
               comp_source="structured")
    r = _post("clean", ["reposted_repeatedly", "no_comp_no_location_no_team"], opp)
    assert r.legitimacy_status == "clean" and r.legitimacy["signals"] == []
    assert r.legitimacy_notes == []  # both contradicted: nothing worth checking


def test_a_checked_signal_that_survives_keeps_the_cap():
    r = _post("suspect", ["reposted_repeatedly"], _opp(ghost_score=0.8))
    assert r.legitimacy_status == "suspect" and r.legitimacy["signals"] == ["reposted_repeatedly"]
    assert r.displayed_score == 3.5 and "legitimacy_suspect_cap" in (r.forced_by or "")


def test_no_pay_no_place_no_team_is_upheld_only_when_none_is_posted():
    bare = _opp(location_raw=None, city=None, country=None, state=None, is_remote=None,
                description_md="Plan freight. Apply today.")
    r = _post("suspect", ["no_comp_no_location_no_team"], bare)
    assert r.legitimacy_status == "suspect" and r.displayed_score == 3.5
    r = _post("suspect", ["no_comp_no_location_no_team"], _opp(location_raw=None, city=None,
                                                                country=None, state=None))
    assert r.legitimacy_status == "clean"  # the team is described


def test_a_suspect_with_only_unchecked_signals_is_not_capped_but_noted():
    r = _post("suspect", ["generic_boilerplate", "no_company_detail"], _opp())
    assert r.legitimacy_status == "clean" and r.displayed_score == 4.2
    assert len(r.legitimacy_notes) == 2


def test_a_reject_stands_on_an_unchecked_scam_signal_and_falls_when_all_are_contradicted():
    r = _post("reject", ["upfront_cost_or_purchase"], _opp())
    assert r.verdict == "pass" and r.legitimacy["signals"] == ["upfront_cost_or_purchase"]
    r = _post("reject", ["reposted_repeatedly", "no_comp_no_location_no_team"], _opp())
    assert r.legitimacy_status == "clean" and r.verdict == "apply"


def test_commission_only_is_contradicted_by_a_posted_base_salary():
    opp = _opp(comp_min=90000, comp_max=100000, comp_source="structured")
    r = _post("suspect", ["commission_only"], opp)
    assert r.legitimacy_status == "clean"
    assert r.legitimacy["signal_checks"][0]["check"] == scoring.CONTRADICTED


def test_the_repost_line_is_the_pipeline_warning_line():
    from kestrel.web.pipeline_view import GHOST_WARN

    assert scoring.GHOST_UPHOLD == GHOST_WARN


def test_without_a_posting_nothing_is_contradicted_or_upheld():
    r = _post("suspect", ["reposted_repeatedly"], None)
    assert r.legitimacy_status == "clean"
    assert r.legitimacy["signal_checks"][0]["check"] == scoring.NEEDS_CONFIRMING


def test_the_final_verdict_comes_from_the_score_bands_never_the_model_field():
    """Owner review (round 7): the final verdict is the score's band (thresholds.apply,
    thresholds.consider), and the model's own verdict field is kept only for context. A model
    "pass" at 3.6 is "consider", a model "pass" at 4.2 is "apply", and a model "apply" at 3.4
    (the shape of one live role) is "consider". Only an upheld blocker or a reject lowers it."""
    clean = _opp()
    assert _post("clean", [], clean, holistic=3.6, verdict="pass").verdict == "consider"
    assert _post("clean", [], clean, holistic=4.2, verdict="pass").verdict == "apply"
    r = _post("clean", [], clean, holistic=3.4, verdict="apply")
    assert (r.verdict, r.verdict_model, r.verdict_disagreement) == ("consider", "apply", True)
    assert _post("clean", [], clean, holistic=2.9, verdict="apply").verdict == "pass"
