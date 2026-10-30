"""D-UI-45: the one translation from stored verdicts to the operator's fit words."""

from __future__ import annotations

import pytest

from kestrel.web import fit


def _ev(**kw):
    base = {"verdict": None, "tier": 2, "score": None, "weighted_score": None,
            "needs_review": False}
    base.update(kw)
    return base


def test_tier2_verdicts_become_fit_words_and_groups():
    assert fit.fit_for(_ev(verdict="apply", score=4.6), "t2_done").word == fit.STRONG
    assert fit.fit_for(_ev(verdict="apply", score=4.1), "t2_done").word == fit.GOOD
    assert fit.fit_for(_ev(verdict="pursue", score=3.6), "t2_done").group == fit.GROUP_WORTH
    assert fit.fit_for(_ev(verdict="consider", score=2.4), "t2_done").word == fit.MAYBE
    f = fit.fit_for(_ev(verdict="pass", score=1.2), "t2_done")
    assert f.word == fit.NOT_FIT and f.group == fit.GROUP_NOT_FIT and f.tone == "bad"


def test_disagreement_is_your_call_in_the_worth_a_look_group():
    f = fit.fit_for(_ev(verdict="pass", score=3.0, needs_review=True), "t2_done")
    assert f.word == fit.YOUR_CALL and f.group == fit.GROUP_WORTH and f.tone == "warn"
    assert "disagree" in f.why


def test_a_disagreement_on_one_side_of_the_lines_is_not_your_call():
    """Owner round 14: a 1.0 model score against a 1.8 weighted score read as a maybe."""
    low = fit.fit_for(_ev(verdict="pass", score=1.0, weighted_score=1.8, needs_review=True),
                      "t2_done")
    assert low.word == fit.NOT_FIT and low.group == fit.GROUP_NOT_FIT
    high = fit.fit_for(_ev(verdict="apply", score=4.6, weighted_score=4.1, needs_review=True),
                       "t2_done")
    assert high.word == fit.GOOD and high.group == fit.GROUP_WORTH
    split = fit.fit_for(_ev(verdict="pass", score=2.0, weighted_score=3.6, needs_review=True),
                        "t2_done")
    assert split.word == fit.YOUR_CALL


def test_tier1_keep_is_new_never_a_no_and_kill_is_not_a_fit():
    kept = fit.fit_for(_ev(verdict="keep", tier=1), "t1_pass")
    assert kept.word == fit.NEW and kept.group == fit.GROUP_NEW and kept.score is None
    killed = fit.fit_for(_ev(verdict="kill", tier=1), "t1_reject")
    assert killed.word == fit.NOT_FIT and killed.why == "quick screen"


def test_rule_rejects_name_the_rule_and_quarantine_reads_parked():
    f = fit.fit_for(None, "t0_reject", t0_rules=["comp_floor", "seniority_floor"])
    assert f.word == fit.NOT_FIT and f.why == "pay rule"
    assert fit.fit_for(None, "t0_reject", t0_rules="title:vp").why == "title rule"
    assert fit.fit_for(None, "t0_reject", t0_rules="blocker:comp_floor: $120k").why == "pay rule"
    q = fit.fit_for(_ev(verdict="keep", tier=1), "quarantined")
    assert q.word == fit.NEW and q.group == fit.GROUP_FAILED and q.why == "scoring failed"
    # Queued to score again (a live task): back in Being scored, not Scoring failed (D-UI-56).
    again = fit.fit_for(_ev(verdict="keep", tier=1), "quarantined", live=True)
    assert again.group == fit.GROUP_NEW and again.why == ""
    assert fit.fit_for(None, "error").group == fit.GROUP_FAILED


def test_failure_reasons_read_in_plain_words():
    """D-UI-56: the newest quarantine error as the operator would say it."""
    w = fit.failure_words
    assert w("the reply hit max_tokens 8000 before the structured object was complete") == (
        "the reply ran out of room"
    )
    assert w("finish_reason: length") == "the reply ran out of room"
    # A loop names the cap it ran into too; the loop is the truer reason.
    assert w('the reply repeated "x" 40 times (a generation loop); list each value once; '
             "the reply hit max_tokens 8000") == "the model repeated itself"
    assert w("1 validation error for Tier2Response: field required") == (
        "the reply was not in the expected shape"
    )
    assert w("invalid JSON: Expecting value") == "the reply was not in the expected shape"
    assert w("HTTP 404 from https://openrouter.ai/api/v1: no endpoint") == (
        "the provider refused the request"
    )
    assert w("something else") == "scoring did not finish" and w(None) == "scoring did not finish"
    assert fit.GROUP_LABELS[fit.GROUP_FAILED] == "Scoring failed"


def test_fit_words_round_trip_to_stored_verdicts_with_a_training_score():
    # Scores sit on the scoring thresholds (apply 4.0, consider 3.0) so labels never contradict
    # the verdict they carry (review finding 3).
    assert fit.verdict_for_fit(fit.STRONG) == ("apply", 4.75)
    assert fit.verdict_for_fit(fit.STRONG, "consulting") == ("pursue", 4.75)
    assert fit.verdict_for_fit(fit.GOOD) == ("apply", 4.0)
    assert fit.verdict_for_fit(fit.MAYBE) == ("consider", 3.0)
    assert fit.verdict_for_fit(fit.NOT_FIT) == ("pass", 1.5)
    for word in fit.FIT_WORDS:
        verdict, score = fit.verdict_for_fit(word)
        assert fit.fit_word_for_verdict(verdict, score) == word
    with pytest.raises(ValueError):
        fit.verdict_for_fit("keep")


def test_operator_group_outranks_the_model_for_grouping():
    """D-UI-51: a shortlisted role is Worth a look; otherwise the operator's latest label
    decides the group; with neither, the model does (None)."""
    from kestrel.web.fit import (
        GROUP_LABELS,
        GROUP_MAYBE,
        GROUP_NEW,
        GROUP_NOT_FIT,
        GROUP_WORTH,
        operator_group,
    )

    assert operator_group(None, pursued=True) == GROUP_WORTH
    assert operator_group("pass", pursued=True) == GROUP_WORTH
    assert operator_group("apply") == GROUP_WORTH and operator_group("pursue") == GROUP_WORTH
    assert operator_group("consider") == GROUP_MAYBE
    assert operator_group("pass") == GROUP_NOT_FIT
    assert operator_group(None) is None and operator_group("keep") is None
    assert GROUP_LABELS[GROUP_NEW] == "Being scored"


def test_a_forced_pass_never_reads_as_a_fit_and_the_lines_follow_settings():
    """Round 14 review: a blocker or fake-posting pass with high scores stays Your call; the
    lines are the operator's thresholds; one missing score stands in for the other."""
    forced = fit.fit_for(_ev(verdict="pass", score=4.6, weighted_score=4.1, needs_review=True),
                         "t2_done")
    assert forced.word == fit.YOUR_CALL
    try:
        fit.set_lines(2.5, 4.0)
        mid = fit.fit_for(_ev(verdict="consider", score=2.6, weighted_score=2.9,
                              needs_review=True), "t2_done")
        assert mid.word == fit.YOUR_CALL  # above the operator's consider line: still a call
    finally:
        fit.set_lines(3.0, 4.0)
    one = fit.fit_for(_ev(verdict="pass", score=2.0, weighted_score=None, needs_review=True),
                      "t2_done")
    assert one.word == fit.NOT_FIT
    none = fit.fit_for(_ev(verdict="pass", score=None, weighted_score=None, needs_review=True),
                       "t2_done")
    assert none.word == fit.YOUR_CALL
