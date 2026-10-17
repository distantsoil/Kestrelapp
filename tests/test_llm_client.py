"""The four backend request-body variants (exact), schema normalization, and the async client's
temperature/max_tokens precedence and embeddings, all respx-mocked (no live calls)."""

from __future__ import annotations

import json

import respx
from httpx import Response

from kestrel.config import BackendConfig, get_settings
from kestrel.llm.client import (
    ExternalBackendError,
    LLMClient,
    build_request_body,
    json_schema_for,
    normalize_schema,
)

_MSGS = [{"role": "user", "content": "hi"}]
_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def _backend(json_mode, thinking):
    return BackendConfig(base_url="http://x/v1", json_mode=json_mode, thinking_control=thinking)


def test_variant_json_schema_reasoning_effort():
    body = build_request_body(
        model="m",
        messages=_MSGS,
        backend=_backend("json_schema", "reasoning_effort"),
        temperature=0.0,
        max_tokens=100,
        schema=_SCHEMA,
        schema_name="resp",
    )
    assert body == {
        "model": "m",
        "messages": _MSGS,
        "temperature": 0.0,
        "max_tokens": 100,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "resp", "schema": _SCHEMA},
        },
        "reasoning_effort": "none",
    }


def test_variant_json_object_chat_template_kwargs():
    body = build_request_body(
        model="m",
        messages=_MSGS,
        backend=_backend("json_object", "chat_template_kwargs"),
        temperature=0.2,
        max_tokens=100,
        schema=_SCHEMA,
    )
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body
    # schema goes into the system prompt in json_object mode
    system = [m for m in body["messages"] if m["role"] == "system"]
    assert system and "JSON Schema" in system[0]["content"]


def test_variant_json_object_none():
    body = build_request_body(
        model="m",
        messages=_MSGS,
        backend=_backend("json_object", "none"),
        temperature=0.2,
        max_tokens=100,
        schema=_SCHEMA,
    )
    assert body["response_format"] == {"type": "json_object"}
    assert "chat_template_kwargs" not in body
    assert "reasoning_effort" not in body


def test_variant_json_schema_none():
    body = build_request_body(
        model="m",
        messages=_MSGS,
        backend=_backend("json_schema", "none"),
        temperature=0.0,
        max_tokens=100,
        schema=_SCHEMA,
    )
    assert body["response_format"]["type"] == "json_schema"
    assert "chat_template_kwargs" not in body and "reasoning_effort" not in body


def test_union_normalization_type_list_form():
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": ["string", "null"], "maxLength": 120},
        },
    }
    out = normalize_schema(schema)
    assert out["properties"]["b"]["type"] == "string"  # null branch removed
    assert out["required"] == ["a"]  # b dropped from required


def test_union_normalization_pydantic_anyof_form():
    from kestrel.llm.schemas import P1CTriage  # has Optional implied_engagement -> anyOf[str, null]

    out = json_schema_for(P1CTriage)
    schema_str = json.dumps(out)
    assert "anyOf" not in schema_str  # collapsed
    assert "null" not in schema_str  # null branch removed
    assert out["properties"]["implied_engagement"]["type"] == "string"
    assert "implied_engagement" not in out.get("required", [])  # optional -> not required


@respx.mock
async def test_complete_prompt_temperature_wins_over_tier():
    route = respx.post("http://127.0.0.1:11434/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})
    )
    client = LLMClient(get_settings())
    # tier1 default temperature is 0.0; passing 0.9 (a "prompt-declared" value) must win
    await client.complete(tier="tier1", messages=_MSGS, temperature=0.9, max_tokens=42)
    sent = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.9
    assert sent["max_tokens"] == 42
    assert sent["reasoning_effort"] == "none"  # local backend


@respx.mock
async def test_complete_falls_back_to_tier_defaults():
    route = respx.post("http://127.0.0.1:11434/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})
    )
    client = LLMClient(get_settings())
    await client.complete(tier="tier2", messages=_MSGS)  # no temperature/max_tokens
    sent = json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0.2  # tier2 default from settings.yml
    assert sent["max_tokens"] == 4000


@respx.mock
async def test_embed_returns_vectors():
    respx.post("http://127.0.0.1:11434/v1/embeddings").mock(
        return_value=Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    )
    client = LLMClient(get_settings())
    vecs = await client.embed(["hello"])
    assert vecs == [[0.1, 0.2, 0.3]]


@respx.mock
async def test_embed_refuses_an_external_backend_even_under_hosted_mode():
    """Embeddings have no hosted path: no request is made whatever the opt-in state."""
    import pytest

    route = respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=Response(200, json={"data": [{"embedding": [0.1]}]})
    )
    s = get_settings()
    s.llm.tiers["embed"].backend = "frontier"
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-12T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    client = LLMClient(s)
    with pytest.raises(ExternalBackendError, match="embeddings have no hosted path"):
        await client.embed(["hello"])
    assert not route.called
