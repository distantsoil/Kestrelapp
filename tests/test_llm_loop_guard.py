"""Generation loops, JSON modes and penalties on hosted calls (D-LLM-2, D-LLM-3, D-UI-48).

The 0.4.x diagnostics: a free OpenRouter model repeated "comp_unstated" in Tier 1's `signals`
until max_tokens; Tier 2 replies spent most of an 8,000-token cap reasoning; a long posting's
comp and location were trimmed away on a hosted Tier 2 call. Every provider call is respx-mocked
or a pure request-body build; nothing reaches a live endpoint."""

from __future__ import annotations

import asyncio
import json

import respx
from httpx import Response

from kestrel.config import HostedModelCaps, get_settings
from kestrel.llm.client import (
    LLMClient,
    build_messages_body,
    build_request_body,
    json_schema_for,
    reasoning_exhausted,
)
from kestrel.llm.guard import degenerate_repeat, run_guarded
from kestrel.llm.prompts import load_prompt
from kestrel.llm.schemas import P1CTriage, P1Triage, P2Legitimacy

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "triage this"}]
_VALID = {"verdict": "keep", "confidence": 0.8, "reason": "plausible", "signals": ["level_match"]}
_LOOP = (
    '{"verdict": "keep", "confidence": 0.7, "reason": "senior IT role", "signals": ["level_match", '
    + ", ".join(['"comp_unstated"'] * 60)
)


def _reply(content: str, finish: str = "stop", reasoning: int | None = None) -> Response:
    usage: dict = {"prompt_tokens": 900, "completion_tokens": 1200}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return Response(200, json={
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content}}],
        "usage": usage,
    })


def _or_backend(**hosted):
    be = get_settings().llm.backends["frontier"].model_copy(deep=True)
    for k, v in hosted.items():
        setattr(be.hosted, k, v)
    return be


def _body(backend, caps=None, schema=True):
    return build_request_body(
        model="vendor/model", messages=[dict(m) for m in _MSGS], backend=backend,
        temperature=0.0, max_tokens=300,
        schema=json_schema_for(P1Triage) if schema else None, schema_name="triage_v1",
        caps=caps,
    )


# ------------------------------------------------------------------ (d) the loop detector
def test_degenerate_repeat_flags_one_value_repeated_past_the_limit():
    why = degenerate_repeat(_LOOP)
    assert why and '"comp_unstated"' in why and "60 times" in why and "list each value once" in why


def test_degenerate_repeat_ignores_keys_and_ordinary_replies():
    # A full Tier 2 shaped reply repeats keys (requirement, evidence) and a few enum values;
    # keys are never counted and nothing here is a loop.
    matches = [{"requirement": f"r{i}", "evidence": f"e{i}", "strength": "strong"}
               for i in range(8)]
    gaps = [{"requirement": "x", "severity": "minor"}] * 6
    reply = json.dumps({"matches": matches, "gaps": gaps})
    assert degenerate_repeat(reply) is None
    assert degenerate_repeat(json.dumps(_VALID)) is None
    assert degenerate_repeat("") is None


@respx.mock
def test_a_looping_reply_at_the_cap_gets_one_repair_then_succeeds(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LOOP, finish="length"), _reply(json.dumps(_VALID))])
    res = asyncio.run(run_guarded(LLMClient(get_settings()), tier="frontier", messages=_MSGS,
                                  model_cls=P1Triage, max_tokens=300, allow_external=True))
    assert res.ok and res.repaired and route.call_count == 2
    repair = json.loads(route.calls[1].request.content)["messages"]
    # The repair names the loop, and quotes only the head of the looping reply back.
    assert "generation loop" in repair[-1]["content"]
    assert len(repair[-2]["content"]) <= 600


@respx.mock
def test_a_loop_twice_quarantines_with_the_raw_reply_kept(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post(OR_URL).mock(side_effect=[
        _reply(_LOOP, finish="length"), _reply(_LOOP, finish="stop")])
    seen = {}

    async def hook(messages, raw, error, model):
        seen.update(raw=raw, error=error)

    res = asyncio.run(run_guarded(LLMClient(get_settings()), tier="frontier", messages=_MSGS,
                                  model_cls=P1Triage, max_tokens=300, allow_external=True,
                                  on_quarantine=hook))
    assert not res.ok and res.quarantined and route.call_count == 2  # one repair, never more
    # The looping first reply is the evidence: kept first, with the repair's reply appended.
    assert "generation loop" in seen["error"]
    assert seen["raw"] == f"{_LOOP}\n\n--- repair attempt ---\n{_LOOP}" and res.raw == seen["raw"]


# --------------------------------------------------------------------- (a) dedupe and cap
def test_signals_are_deduped_in_order_and_capped_not_rejected():
    out = P1Triage.model_validate({**_VALID, "signals": [
        "comp_unstated", "level_match", "comp_unstated", "function_match", "location_ok",
        "industry_match", "comp_ok", "level_match"]})
    assert out.signals == ["comp_unstated", "level_match", "function_match", "location_ok",
                           "industry_match"]
    c = P1CTriage.model_validate({"verdict": "keep", "confidence": 0.9, "reason": "x",
                                  "signals": ["size_fit", "size_fit", "sector_fit"]})
    assert c.signals == ["size_fit", "sector_fit"]
    legit = P2Legitimacy.model_validate({"status": "clean", "confidence": 1,
                                         "signals": ["no_company_detail"] * 30})
    assert legit.signals == ["no_company_detail"]


def test_signals_schema_carries_max_items_except_to_the_claude_api():
    sig = json_schema_for(P1Triage)["properties"]["signals"]
    assert sig["maxItems"] == 5 and sig["items"]["enum"]
    assert json_schema_for(P2Legitimacy)["properties"]["signals"]["maxItems"] == 8
    claude = get_settings().llm.backends["anthropic"]
    body = build_messages_body(model="claude-x", messages=_MSGS, temperature=0, max_tokens=300,
                               schema=json_schema_for(P1Triage), backend=claude)
    assert "maxItems" not in json.dumps(body["output_config"]["format"]["schema"])


# ------------------------------------------------------------------------- (b) the prompt
def test_triage_prompts_ask_for_a_short_reason_and_each_signal_once():
    for version in ("triage/v2", "triage_consulting/v2"):
        system = load_prompt(version).system
        assert "reason under 20 words" in system and "List each signal once" in system
        assert "—" not in system


# --------------------------------------------------------------------- (c) the penalties
def test_penalty_rides_only_on_openrouter_and_only_where_it_cannot_narrow_routing():
    be = _or_backend()
    assert be.hosted.repetition_penalty == 1.05
    listed = HostedModelCaps(json_mode="schema", params=["structured_outputs",
                                                         "repetition_penalty"])
    assert _body(be, listed)["repetition_penalty"] == 1.05
    unlisted = HostedModelCaps(json_mode="schema", params=["structured_outputs"])
    assert "repetition_penalty" not in _body(be, unlisted)
    # Unknown parameters with require_parameters on: a penalty could route to no endpoint.
    assert "repetition_penalty" not in _body(be, None)
    # Unknown parameters, JSON mode (require_parameters off): an endpoint ignores what it lacks.
    assert _body(be, HostedModelCaps(json_mode="json_object"))["repetition_penalty"] == 1.05
    off = _or_backend(repetition_penalty=None, frequency_penalty=0.3)
    both = HostedModelCaps(params=["structured_outputs", "repetition_penalty",
                                   "frequency_penalty"])
    body = _body(off, both)
    assert "repetition_penalty" not in body and body["frequency_penalty"] == 0.3
    s = get_settings()
    for name in ("local", "openai"):
        other = _body(s.llm.backends[name], listed)
        assert "repetition_penalty" not in other and "frequency_penalty" not in other


# ------------------------------------------------------------ JSON mode per model (D-UI-48)
def test_json_object_model_gets_json_mode_with_the_schema_in_the_prompt():
    body = _body(_or_backend(), HostedModelCaps(json_mode="json_object"))
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"]["require_parameters"] is False
    system = body["messages"][0]["content"]
    assert system.startswith("sys") and "JSON Schema" in system and '"comp_unstated"' in system
    assert '"additionalProperties": false' not in system  # not rewritten to the strict dialect


def test_prompt_only_model_gets_no_response_format_and_a_plain_instruction():
    body = _body(_or_backend(), HostedModelCaps(json_mode="prompt"))
    assert "response_format" not in body and body["provider"]["require_parameters"] is False
    assert "Reply with only that JSON object" in body["messages"][0]["content"]


def test_schema_model_keeps_the_strict_schema_and_require_parameters():
    for caps in (None, HostedModelCaps(json_mode="schema")):
        body = _body(_or_backend(), caps)
        js = body["response_format"]["json_schema"]
        assert body["response_format"]["type"] == "json_schema" and js["strict"] is True
        assert js["schema"]["additionalProperties"] is False
        assert body["provider"]["require_parameters"] is True


@respx.mock
def test_the_client_reads_the_tiers_model_caps(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = get_settings().model_copy(deep=True)
    model = s.llm.tiers["frontier"].model
    s.llm.hosted_model_caps[model] = HostedModelCaps(json_mode="json_object")
    route = respx.post(OR_URL).mock(return_value=_reply(json.dumps(_VALID)))
    res = asyncio.run(run_guarded(LLMClient(s), tier="frontier", messages=_MSGS,
                                  model_cls=P1Triage, allow_external=True))
    assert res.ok
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"] == {"type": "json_object"}


# ------------------------------------------------------ reasoning overruns (Tier 2 at 8,000)
def test_a_reply_mostly_spent_on_reasoning_is_re_asked_with_reasoning_off():
    data = json.loads(_reply('{"blocks": {"a": "partial', finish="length",
                             reasoning=7000).content)
    data["usage"]["completion_tokens"] = 8000
    assert reasoning_exhausted(data) is True  # 7,000 of 8,000: the 0.9 rule missed this
    data["usage"]["completion_tokens_details"]["reasoning_tokens"] = 1000
    assert reasoning_exhausted(data) is False  # a long answer cut, not a thinking overrun


def test_the_re_ask_turns_reasoning_off_with_the_unified_switch():
    body = build_request_body(model="nvidia/nemotron-x:free", messages=_MSGS,
                              backend=_or_backend(), temperature=0, max_tokens=8000,
                              reasoning_hint="none")
    assert body["reasoning"] == {"enabled": False} and "reasoning_effort" not in body
