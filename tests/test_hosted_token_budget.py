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


# ------------------------------------------------------------- WP-10 (#33): cut-off replies
_CUT = '{"verdict": "keep", "confidence": 0.8, "reason": "plausible, a senior role with scope'
_OK = json.dumps({"verdict": "keep", "confidence": 0.8, "reason": "plausible",
                  "signals": ["level_match"]})


def _reply(content: str, finish: str = "length", *, prompt: int = 900, cost: float = 0.01):
    return Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": 700, "cost": cost},
    })


def _sent(route, i: int) -> int:
    return json.loads(route.calls[i].request.content)["max_tokens"]


def _guard(settings, **kw):
    return asyncio.run(run_guarded(
        LLMClient(settings), tier="tier1", messages=MSG, model_cls=P1Triage, max_tokens=300, **kw
    ))


@respx.mock
def test_a_cut_off_reply_is_retried_once_with_twice_the_budget(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[_reply(_CUT), _reply(_OK, "stop")])
    res = _guard(_hosted())
    assert res.ok and route.call_count == 2
    assert _sent(route, 0) == 1200 and _sent(route, 1) == 2400  # the floor, then doubled
    # It is not the schema repair: that attempt is still unspent, and the reply was not repaired.
    assert res.repaired is False
    # Both attempts were billed, so both are in the record.
    assert res.prompt_tokens == 1800 and res.completion_tokens == 1400
    assert res.cost_usd == pytest.approx(0.02)


@respx.mock
def test_the_length_retry_does_not_use_up_the_schema_repair(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    bad = '{"verdict": "keep"}'  # complete JSON that fails the schema
    route = respx.post(OR_URL).mock(
        side_effect=[_reply(_CUT), _reply(bad, "stop"), _reply(_OK, "stop")]
    )
    res = _guard(_hosted())
    assert res.ok and res.repaired and route.call_count == 3
    assert _sent(route, 1) == 2400 and _sent(route, 2) == 2400  # the repair keeps the larger budget


@respx.mock
def test_a_second_cut_off_quarantines_naming_the_limit_with_the_raw_reply(monkeypatch):
    from kestrel.web.fit import failure_words

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[_reply(_CUT)] * 6)
    seen: dict = {}

    async def hook(_messages, raw, reason, _model):
        seen.update(raw=raw, reason=reason)

    res = _guard(_hosted(), on_quarantine=hook)
    assert route.call_count == 2  # one retry, never more
    assert not res.ok and res.quarantined and res.raw
    assert "max_tokens 2400" in seen["reason"] and "retried once" in seen["reason"]
    assert seen["raw"] and failure_words(seen["reason"]) == "the reply ran out of room"
    assert res.prompt_tokens == 1800  # both attempts billed


@respx.mock
def test_the_retry_budget_stops_at_the_models_context_minus_the_input(monkeypatch):
    from kestrel.config import HostedModelCaps

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = _hosted()
    s.llm.hosted_model_caps["nvidia/nemotron:free"] = HostedModelCaps(context_length=3000)
    route = respx.post(OR_URL).mock(side_effect=[_reply(_CUT), _reply(_OK, "stop")])
    assert _guard(s).ok
    assert _sent(route, 1) == 3000 - 900  # not 2400: that would overrun the window
    # No room at all (window minus input is below what was already asked): quarantine at once.
    respx.reset()
    s.llm.hosted_model_caps["nvidia/nemotron:free"] = HostedModelCaps(context_length=2000)
    route = respx.post(OR_URL).mock(side_effect=[_reply(_CUT), _reply(_OK, "stop")])
    res = _guard(s)
    assert route.call_count == 1 and res.quarantined


@respx.mock
def test_length_retry_and_reasoning_off_reask_are_each_bounded(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # An empty cut-off (reasoning spent the budget) gets the reasoning-off re-ask; a partial
    # reply after that gets the one length retry; a third cut-off quarantines. Three calls, ever.
    route = respx.post(OR_URL).mock(side_effect=[_reply("")] + [_reply(_CUT)] * 8)
    res = _guard(_hosted())
    assert route.call_count == 3 and res.quarantined


@respx.mock
def test_an_external_tier_in_local_mode_gets_the_hosted_floor(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = get_settings().model_copy(deep=True)
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-15T00:00:00Z"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    s.llm.tiers["tier1"] = TierConfig(backend="frontier", model="vendor/m", max_tokens=300)
    assert s.llm.mode == "local"
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}}
    route = respx.post(OR_URL).mock(return_value=Response(200, json=ok))
    asyncio.run(LLMClient(s).complete(tier="tier1", messages=MSG, max_tokens=300))
    assert json.loads(route.calls[0].request.content)["max_tokens"] == HOSTED_MAX_TOKENS["tier1"]
    # A local backend keeps the prompt's own cap, and the frontier tier its own budget.
    assert LLMClient(s)._external_floor("tier1", s.llm.backends["local"]) is None
    assert LLMClient(s)._external_floor("frontier", s.llm.backends["frontier"]) is None
