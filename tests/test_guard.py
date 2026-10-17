"""The schema guard: valid parsed; malformed repaired on retry; malformed twice quarantined;
<think> stripped. respx-mocked."""

from __future__ import annotations

import respx
from httpx import Response

from kestrel.config import get_settings
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_guarded
from kestrel.llm.schemas import P1Triage

_URL = "http://127.0.0.1:11434/v1/chat/completions"
_MSGS = [{"role": "user", "content": "triage this"}]
_VALID = '{"verdict": "keep", "confidence": 0.8, "reason": "plausible", "signals": ["level_match"]}'


def _chat(content: str) -> Response:
    return Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        },
    )


async def _run(on_quarantine=None):
    client = LLMClient(get_settings())
    return await run_guarded(
        client, tier="tier1", messages=_MSGS, model_cls=P1Triage, on_quarantine=on_quarantine
    )


@respx.mock
async def test_valid_parsed():
    respx.post(_URL).mock(return_value=_chat(_VALID))
    res = await _run()
    assert res.ok and not res.repaired and not res.quarantined
    assert isinstance(res.value, P1Triage) and res.value.verdict == "keep"
    assert res.prompt_tokens == 5 and res.completion_tokens == 7


@respx.mock
async def test_malformed_then_repaired():
    respx.post(_URL).mock(side_effect=[_chat("{ this is not json"), _chat(_VALID)])
    res = await _run()
    assert res.ok and res.repaired and not res.quarantined
    assert res.value.verdict == "keep"
    assert res.prompt_tokens == 10  # tokens summed across both calls


@respx.mock
async def test_malformed_twice_quarantined():
    respx.post(_URL).mock(side_effect=[_chat("{bad"), _chat("still bad}")])
    captured = {}

    async def hook(prompt, raw, error, model):
        captured.update(prompt=prompt, raw=raw, error=error, model=model)

    res = await _run(on_quarantine=hook)
    assert not res.ok and res.quarantined and res.error
    assert captured["raw"] == "still bad}"
    assert captured["model"] == "kestrel-tier1"
    assert captured["prompt"] == _MSGS


@respx.mock
async def test_think_span_stripped_before_parsing():
    respx.post(_URL).mock(return_value=_chat(f"<think>let me reason about this...</think>{_VALID}"))
    res = await _run()
    assert res.ok and not res.repaired
    assert res.value.verdict == "keep"


@respx.mock
async def test_json_with_surrounding_prose_is_located():
    respx.post(_URL).mock(return_value=_chat(f"Here is the result:\n{_VALID}\nThanks!"))
    res = await _run()
    assert res.ok and res.value.verdict == "keep"


# -------------------------------------------------------------------- prompt-declared params win
@respx.mock
async def test_run_prompt_threads_prompt_temperature_and_max_tokens():
    import json

    from kestrel.llm.guard import run_prompt
    from kestrel.llm.prompts import load_prompt

    valid = '{"body_md": "hi", "word_count": 300, "claims_used": [], "opening_alternatives": []}'
    route = respx.post(_URL).mock(return_value=_chat(valid))
    spec = load_prompt(
        "cover/v1"
    )  # temperature 0.4, max_tokens 1200 (tier3 fallback is 0.3 / 6000)
    res = await run_prompt(LLMClient(get_settings()), spec, [{"role": "user", "content": "x"}])
    assert res.ok
    sent = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.4 and sent["max_tokens"] == 1200  # prompt WINS over tier


# ------------------------------------------------------------------------ P3 safe-path guardrail
async def test_run_guarded_refuses_p3_without_dynamic_schema():
    import pytest

    from kestrel.llm.schemas import P3DocSelect

    with pytest.raises(ValueError, match="run_doc_select"):
        await run_guarded(
            LLMClient(get_settings()), tier="docsel", messages=[], model_cls=P3DocSelect
        )


_CATALOG = {
    "layouts": ["executive"],
    "summary_variants": [{"key": "transformation"}],
    "skill_groups": [{"key": "identity"}],
    "slots": [
        {"key": "r1b1", "optional": False, "variants": [{"key": "cost"}, {"key": "scale"}]},
        {"key": "r1b2", "optional": True, "variants": [{"key": "security"}]},
    ],
}


def _p3(slots):
    return (
        '{"layout": "executive", "summary": "transformation", "slots": '
        + __import__("json").dumps(slots)
        + ', "skill_groups": {"identity": true}, "rationale": {}, "coherence_check": "ok"}'
    )


@respx.mock
async def test_run_doc_select_uses_dynamic_enum_schema_and_catches_hallucination():
    import json

    route = respx.post("http://127.0.0.1:11434/v1/chat/completions").mock(
        return_value=_chat(_p3({"r1b1": "HALLUCINATED", "r1b2": "hide"}))
    )
    from kestrel.llm.guard import run_doc_select

    res = await run_doc_select(
        LLMClient(get_settings()), messages=[{"role": "user", "content": "x"}], **_CATALOG
    )
    # the sent schema constrained r1b1 to real variant keys...
    sent = json.loads(route.calls.last.request.content)
    slot_schema = sent["response_format"]["json_schema"]["schema"]["properties"]["slots"]
    assert slot_schema["properties"]["r1b1"]["enum"] == ["cost", "scale"]
    # ...and the hallucinated key is a validation FAILURE, never a silent default
    assert not res.ok and "unknown variant key" in res.error


@respx.mock
async def test_run_doc_select_accepts_a_valid_selection():
    from kestrel.llm.guard import run_doc_select

    respx.post("http://127.0.0.1:11434/v1/chat/completions").mock(
        return_value=_chat(_p3({"r1b1": "cost", "r1b2": "hide"}))
    )
    res = await run_doc_select(
        LLMClient(get_settings()), messages=[{"role": "user", "content": "x"}], **_CATALOG
    )
    assert res.ok and res.value.slots["r1b1"] == "cost"
