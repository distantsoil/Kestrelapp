"""The Tier 2 over-budget reason reaches the person in plain words (D-SCORE-21), not the generic
"did not finish" fallback. Invented text only; nothing touches the network."""

from __future__ import annotations

from kestrel.pipeline.tier2 import PROMPT_OVER_BUDGET
from kestrel.web import fit, retry_words


def test_the_needs_another_try_page_names_the_reason():
    assert retry_words.plain_reason(PROMPT_OVER_BUDGET) == "Too long for your local model"


def test_the_pipeline_row_names_the_reason():
    assert fit.failure_words(PROMPT_OVER_BUDGET) == "too long for your local model"


def test_the_stage_reason_form_maps_the_same_way():
    assert fit.failure_words(f"t2 {PROMPT_OVER_BUDGET}") == "too long for your local model"
