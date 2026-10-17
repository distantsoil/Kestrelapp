"""Schema validators: TRUNCATE overlong strings, CLAMP out-of-range numbers, cap overlong lists;
structural problems (missing required, invalid enum) are the only validation failures."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kestrel.llm.schemas import P1Triage, P9Mock


def test_overlong_string_is_truncated_not_rejected():
    m = P1Triage.model_validate(
        {"verdict": "keep", "confidence": 0.5, "reason": "x" * 300, "signals": []}
    )
    assert len(m.reason) == 140


def test_confidence_clamped():
    hi = P1Triage.model_validate(
        {"verdict": "keep", "confidence": 5.0, "reason": "r", "signals": []}
    )
    lo = P1Triage.model_validate(
        {"verdict": "kill", "confidence": -3, "reason": "r", "signals": []}
    )
    assert hi.confidence == 1.0 and lo.confidence == 0.0


def test_int_scores_clamped_to_1_5():
    m = P9Mock.model_validate(
        {
            "scores": {
                "structure": 9,
                "specificity": 0,
                "relevance": 3,
                "length": 3,
                "ownership": 3,
            },
            "worked_well": "",
            "improvements": [],
            "unsupported_claims": [],
            "revised_opening": "",
            "next_question": "",
            "next_question_rationale": "",
        }
    )
    assert m.scores.structure == 5 and m.scores.specificity == 1


def test_overlong_list_capped():
    m = P1Triage.model_validate(
        {"verdict": "keep", "confidence": 0.5, "reason": "r", "signals": ["level_match"] * 10}
    )
    assert m.signals == ["level_match"]  # D-LLM-2: a repeated signal is one signal
    distinct = ["level_match", "function_match", "location_ok", "comp_unstated", "comp_ok",
                "comp_below_floor"]
    m = P1Triage.model_validate(
        {"verdict": "keep", "confidence": 0.5, "reason": "r", "signals": distinct * 2}
    )
    assert len(m.signals) == 5 and m.signals == distinct[:5]


def test_missing_required_field_is_a_failure():
    with pytest.raises(ValidationError):
        P1Triage.model_validate({"verdict": "keep", "confidence": 0.5})  # no reason/signals


def test_invalid_enum_is_a_failure():
    with pytest.raises(ValidationError):
        P1Triage.model_validate(
            {"verdict": "maybe", "confidence": 0.5, "reason": "r", "signals": []}
        )


def test_extra_fields_are_ignored_not_rejected():
    m = P1Triage.model_validate(
        {"verdict": "keep", "confidence": 0.5, "reason": "r", "signals": [], "extra": 1}
    )
    assert m.verdict == "keep"
