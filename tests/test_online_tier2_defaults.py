"""Tier 2 on an online model defaults to reasoning "none" and four parallel requests, and a value
the person chose always wins (#92). Local defaults never change (the 16 GB swap-thrash rule)."""

from __future__ import annotations

import pytest

from kestrel.config import HostedMode, HostedModelCaps, load_settings
from kestrel.llm.client import LLMClient

MODEL = "vendor/test-model"


def _overrides(**extra) -> dict:
    hm = HostedMode()
    base = {
        "llm": {
            "hosted_tiers": {
                t: {"backend": "frontier", "model": MODEL} for t in ("tier1", "tier2")
            },
            "hosted_mode": {
                "enabled": True,
                "acknowledged_at": "2026-10-15T10:00:00+00:00",
                "acknowledged_terms": hm.disclosure_terms(),
            },
            "hosted_model_caps": {MODEL: HostedModelCaps(
                json_mode="schema", params=["structured_outputs", "reasoning"],
                reasoning_mandatory=False,
            ).model_dump()},
        }
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("KESTREL_CONCURRENCY__TIER2", raising=False)
    monkeypatch.delenv("KESTREL_CONCURRENCY", raising=False)


def test_online_with_nothing_set_gets_none_and_four():
    s = load_settings(db_overrides=_overrides(), mode="hosted")
    assert s.llm.mode == "hosted"
    assert s.concurrency.tier2 == 4
    assert LLMClient(s).default_reasoning_hint("tier2") == "none"


def test_both_mode_puts_tier2_online_with_the_same_defaults():
    s = load_settings(db_overrides=_overrides(), mode="both")
    assert s.concurrency.tier2 == 4
    assert LLMClient(s).default_reasoning_hint("tier2") == "none"


def test_local_with_nothing_set_keeps_its_defaults():
    s = load_settings(db_overrides=_overrides(), mode="local")
    assert (s.concurrency.tier1, s.concurrency.tier2) == (4, 2)
    assert LLMClient(s).default_reasoning_hint("tier2") is None


def test_a_saved_concurrency_wins_online_even_when_it_equals_the_default():
    for saved in (2, 3, 8):
        s = load_settings(db_overrides=_overrides(concurrency={"tier2": saved}), mode="hosted")
        assert s.concurrency.tier2 == saved


def test_an_environment_or_edited_value_wins_online(monkeypatch):
    monkeypatch.setenv("KESTREL_CONCURRENCY__TIER2", "2")
    assert load_settings(db_overrides=_overrides(), mode="hosted").concurrency.tier2 == 2
    monkeypatch.delenv("KESTREL_CONCURRENCY__TIER2")
    s = load_settings(db_overrides=_overrides(), mode="hosted")
    assert s.concurrency.tier2 == 4  # the unset default is not a choice


def test_a_chosen_local_value_is_untouched():
    s = load_settings(db_overrides=_overrides(concurrency={"tier2": 3}), mode="local")
    assert s.concurrency.tier2 == 3


def test_a_chosen_reasoning_effort_wins_online():
    o = _overrides()
    o["llm"]["backends"] = {"frontier": {"hosted": {"reasoning_effort": "low"}}}
    s = load_settings(db_overrides=o, mode="hosted")
    assert s.llm.backends["frontier"].hosted.reasoning_effort == "low"
    assert LLMClient(s).default_reasoning_hint("tier2") is None


def test_a_backend_without_the_verified_switch_is_sent_nothing():
    o = _overrides()
    o["llm"]["hosted_model_caps"][MODEL]["reasoning_mandatory"] = True
    s = load_settings(db_overrides=o, mode="hosted")
    assert LLMClient(s).default_reasoning_hint("tier2") is None
    assert s.concurrency.tier2 == 4  # concurrency does not depend on the reasoning switch


def test_the_reasoning_off_default_covers_only_the_full_review_it_was_measured_on():
    """D-LLM-62: the #92 run measured the full review only. Story builds, mock critiques and the
    analytics summary on an online Tier 2 keep the model's own reasoning."""
    s = load_settings(db_overrides=_overrides(), mode="hosted")
    c = LLMClient(s)
    assert c.default_reasoning_hint("tier2", prompt="evaluate_v3") == "none"
    assert c.default_reasoning_hint("tier2", prompt="evaluate_consulting_v3") == "none"
    for other in ("story_consolidate_v3", "mock_critique_v2", "analytics_summary_v2"):
        assert c.default_reasoning_hint("tier2", prompt=other) is None, other
    assert c.default_reasoning_hint("tier1", prompt="triage_v2") == "none"


async def test_guard_passes_the_prompt_so_a_story_build_is_not_sent_reasoning_off():
    from types import SimpleNamespace

    from pydantic import BaseModel

    from kestrel.llm import guard

    class _Out(BaseModel):
        text: str

    s = load_settings(db_overrides=_overrides(), mode="hosted")
    seen: list = []

    class _Client(LLMClient):
        async def complete(self, **kw):
            seen.append(kw.get("reasoning_hint"))
            raise RuntimeError("stop after the first attempt")

    spec = SimpleNamespace(tier="tier2", model_cls=_Out, temperature=0.2, max_tokens=50,
                           version="story_consolidate/v3", reasoning=None,
                           repair_max_tokens=None)
    try:
        await guard.run_prompt(_Client(s), spec, [{"role": "user", "content": "x"}])
    except RuntimeError:
        pass
    assert seen and seen[0] is None
