"""D-LLM-1: hosted tiers get their own output budget. The operator's hosted-mode diagnostics
showed every quarantine was finish_reason "length": hosted tier1 and tier2 ran on the local
budgets (300 and 4000) because `llm.hosted_tiers.*.max_tokens` was unset, and a free reasoning
model still ran out on the reasoning-off re-ask while the error said to lower the reasoning
effort. Cloud-safe: respx stands in for OpenRouter; nothing is sent anywhere."""

from __future__ import annotations

import asyncio
import json

import pytest
import respx
from httpx import Response

from kestrel.config import HOSTED_MAX_TOKENS, TierConfig, apply_mode, get_settings
from kestrel.llm.client import LLMClient, chat_stop_problem
from kestrel.llm.guard import run_guarded
from kestrel.llm.schemas import P1Triage

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
MSG = [{"role": "user", "content": "x"}]


def _hosted(**tier1):
    s = get_settings().model_copy(deep=True)
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-15T00:00:00Z"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    s.llm.hosted_tiers = {
        "tier1": TierConfig(backend="frontier", model="nvidia/nemotron:free", **tier1),
        "tier2": TierConfig(backend="frontier", model="nvidia/nemotron:free"),
    }
    s.llm.mode = "hosted"
    return apply_mode(s)


def _length_reply(content: str = "", reasoning: int | None = None) -> dict:
    usage: dict = {"prompt_tokens": 900, "completion_tokens": 1200}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"choices": [{"finish_reason": "length", "message": {"content": content}}],
            "usage": usage}


def test_hosted_tiers_get_their_own_default_and_local_budgets_are_untouched():
    local = get_settings()
    assert local.llm.tiers["tier1"].max_tokens == 300
    assert local.llm.tiers["tier2"].max_tokens == 4000
    s = _hosted()
    assert s.llm.mode == "hosted"
    assert s.llm.tiers["tier1"].max_tokens == HOSTED_MAX_TOKENS["tier1"] == 1200
    assert s.llm.tiers["tier2"].max_tokens == HOSTED_MAX_TOKENS["tier2"] == 8000
    # The operator's own value wins over the default.
    assert _hosted(max_tokens=2500).llm.tiers["tier1"].max_tokens == 2500


def test_a_hosted_tier_with_no_local_entry_still_gets_the_hosted_default():
    """Review finding: a hosted tier absent from llm.tiers (a user settings.yml that omits it)
    was copied without the overlay, so it lost its HOSTED_MAX_TOKENS default."""
    s = get_settings().model_copy(deep=True)
    s.llm.tiers.pop("docsel", None)
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-15T00:00:00Z"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    s.llm.hosted_tiers = {
        "tier1": TierConfig(backend="frontier", model="m"),
        "tier2": TierConfig(backend="frontier", model="m"),
        "docsel": TierConfig(backend="frontier", model="m"),
    }
    s.llm.mode = "hosted"
    out = apply_mode(s)
    assert out.llm.tiers["docsel"].max_tokens == HOSTED_MAX_TOKENS["docsel"]
    assert out.llm.tiers["docsel"].model == "m"


@respx.mock
def test_the_hosted_budget_is_a_floor_under_the_prompts_local_sized_cap(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}}
    route = respx.post(OR_URL).mock(return_value=Response(200, json=ok))
    asyncio.run(LLMClient(_hosted()).complete(tier="tier1", messages=MSG, max_tokens=300))
    assert json.loads(route.calls[0].request.content)["max_tokens"] == 1200
    # Tier 3 escalation (the frontier tier) keeps the prompt's own cap.
    respx.reset()
    route = respx.post(OR_URL).mock(return_value=Response(200, json=ok))
    asyncio.run(LLMClient(_hosted()).complete(tier="frontier", messages=MSG, max_tokens=2000,
                                              allow_external=True))
    assert json.loads(route.calls[0].request.content)["max_tokens"] == 2000


def test_local_mode_keeps_the_prompt_cap():
    s = get_settings()
    assert s.llm.mode == "local"
    client = LLMClient(s)
    assert not client._hosted_tier("tier1", s.llm.backends[s.llm.tiers["tier1"].backend])


def test_reasoning_off_text_asks_for_more_output_tokens_and_names_the_setting():
    msg = chat_stop_problem(_length_reply(), 1200, reasoning_off=True,
                            setting="llm.hosted_tiers.tier1.max_tokens")
    assert msg is not None
    assert "lower the reasoning" not in msg
    assert "needs more output tokens" in msg
    assert "llm.hosted_tiers.tier1.max_tokens" in msg and "max_tokens 1200" in msg
    with_reasoning = chat_stop_problem(_length_reply(reasoning=1100), 1200,
                                       setting="llm.hosted_tiers.tier1.max_tokens")
    assert "lower the reasoning effort" in (with_reasoning or "")
    assert "llm.hosted_tiers.tier1.max_tokens" in (with_reasoning or "")


@respx.mock
def test_reasoning_off_retry_quarantines_with_the_raw_reply_and_the_right_reason(monkeypatch):
    """Hard rules 3 and 4 hold: one re-ask with reasoning off, then quarantine with the raw
    response preserved; the reason no longer suggests lowering the effort."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(return_value=Response(200, json=_length_reply("", 1150)))
    seen: list[tuple[str, str]] = []

    async def hook(_messages, raw, reason, _model):
        seen.append((raw, reason))

    res = asyncio.run(run_guarded(LLMClient(_hosted()), tier="tier1", model_cls=P1Triage,
                                  messages=MSG, max_tokens=300, reasoning_hint="low",
                                  on_quarantine=hook))
    assert route.call_count == 2 and res.quarantined and not res.ok
    sent = [json.loads(c.request.content) for c in route.calls]
    assert sent[1]["reasoning"] == {"enabled": False}
    assert all(b["max_tokens"] == 1200 for b in sent)
    assert "needs more output tokens" in (res.error or "")
    assert "lower the reasoning" not in (res.error or "")
    assert "llm.hosted_tiers.tier1.max_tokens" in (res.error or "")
    assert seen and seen[0][0] and "finish_reason" in seen[0][0]


@pytest.mark.parametrize("tier", ["tier1", "tier2"])
def test_every_required_hosted_tier_has_a_default(tier):
    assert HOSTED_MAX_TOKENS[tier] > get_settings().llm.tiers[tier].max_tokens
