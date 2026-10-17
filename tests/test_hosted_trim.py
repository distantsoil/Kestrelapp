"""The hosted Tier 2 description slice (D-LLM-3). Serve Robotics was flagged no_company_detail
and no_comp_no_location_no_team on a hosted run although the posting states all three: the
3,000-token hosted slice cut a long description's middle and most of its tail, where comp and
location sit. The slice now follows the model's context window and keeps the head and a tail
window. Pure functions; no model is called."""

from __future__ import annotations

from kestrel.config import HostedModelCaps, get_settings
from kestrel.ingest.tokens import count_tokens
from kestrel.pipeline import _shared
from kestrel.pipeline.context import build_tier2_values
from kestrel.pipeline.profile import load_profile
from tests.test_prompt_budgets import _opp, _org

_ACK = "2026-10-15T00:00:00Z"
_COMP = "Compensation: $210,000 to $260,000 base plus equity. Location: Redwood City, CA."
_TEAM = "You will lead a team of 14 engineers across platform and IT operations."


def _long_description() -> str:
    """About 9,000 tokens: an About Us head, a long middle, the team line two thirds in, and the
    comp and location line at the very end."""
    about = "Serve Robotics builds sidewalk delivery robots for restaurants and retailers. "
    middle = " ".join(
        f"Responsibility {i}: own the roadmap for fleet systems, vendor contracts and the "
        "service desk, with clear reporting to the executive team." for i in range(330)
    )
    tail = " ".join(f"Benefit {i}: medical, dental and vision coverage." for i in range(40))
    return f"{about * 20}\n\n{middle}\n\n{_TEAM}\n\n{tail}\n\n{_COMP}"


def _hosted(context_length: int | None):
    s = get_settings()
    s.llm.tiers["tier2"].backend = "frontier"
    s.llm.tiers["tier2"].model = "nvidia/nemotron-3.5-super-vl:free"
    s.llm.tiers["tier2"].max_tokens = 8000
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = _ACK
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    if context_length is not None:
        s.llm.hosted_model_caps["nvidia/nemotron-3.5-super-vl:free"] = HostedModelCaps(
            json_mode="schema", context_length=context_length)
    return s


def _slice(settings) -> str:
    n = _shared.jd_budget(settings, "tier2", settings.pipeline.max_jd_tokens)
    values = build_tier2_values(
        opp=_opp("employment", _long_description()), org=_org(),
        profile=load_profile("employment"), source_name="greenhouse", max_jd_tokens=n,
        few_shot="", comp_intel=None,
    )
    return values["description_truncated_to_max_jd_tokens"]


def test_the_long_description_is_longer_than_the_old_hosted_slice():
    assert count_tokens(_long_description()) > 8000


def test_a_large_context_model_gets_the_whole_posting_with_comp_and_team():
    desc = _slice(_hosted(262144))
    assert _COMP in desc and _TEAM in desc and "elided" not in desc


def test_the_slice_keeps_head_and_tail_when_it_must_cut():
    # A 16k window: below the ceiling, so the description is cut, keeping both ends.
    s = _hosted(16384)
    n = _shared.jd_budget(s, "tier2", s.pipeline.max_jd_tokens)
    # 16,384 - 6,000 overhead - 8,000 output = 2,384, less the 15% tokenizer margin. The 3,000
    # floor would not fit in this window, so it is not applied.
    assert n == int(2384 * 0.85)
    desc = _slice(s)
    assert desc.startswith("Serve Robotics builds") and desc.rstrip().endswith(_COMP)
    assert "elided" in desc and count_tokens(desc) <= n


def test_unknown_context_uses_the_local_budget_and_still_keeps_the_tail():
    s = _hosted(None)
    assert _shared.jd_budget(s, "tier2", s.pipeline.max_jd_tokens) == s.pipeline.max_jd_tokens
    assert _slice(s).rstrip().endswith(_COMP)


def test_the_disclosure_states_the_ceiling():
    from kestrel import setup as setup_mod

    s = get_settings()
    assert _shared.hosted_jd_ceiling(s) == 16000
    assert "up to 16000 tokens in hosted mode" in setup_mod.tier_sends(s)["tier2"]
