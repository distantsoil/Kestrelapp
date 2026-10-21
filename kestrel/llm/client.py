"""LLM access.

Two clients:
  - OllamaClient: minimal SYNC client used only by preflight (doctor).
  - LLMClient: the async, config-driven, OpenAI-compatible client used by the pipeline.

The async client encodes the per-backend differences (SPEC.md 8.1/8.4/8.5) that are SILENT if
crossed: json_schema vs json_object structured output, and reasoning_effort vs chat_template_kwargs
vs neither for thinking control. Schema normalization (union ["X","null"] -> "X" + drop from
required) is applied ONCE here, in json_schema mode.
"""

from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from kestrel.config import BackendConfig, HostedModelCaps, JsonMode, Settings
from kestrel.llm import credit
from kestrel.llm.credit import OutOfCredit

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


# ============================================================ sync preflight client (doctor)
class OllamaClient:
    def __init__(self, base_url: str, native_url: str, api_key: str = "ollama") -> None:
        self.base_url = base_url.rstrip("/")
        self.native_url = native_url.rstrip("/")
        self.api_key = api_key

    def version(self, timeout: float | httpx.Timeout = 5.0) -> str:
        r = httpx.get(f"{self.native_url}/api/version", timeout=timeout)
        r.raise_for_status()
        return r.json().get("version", "")

    def list_models(self, timeout: float = 10.0) -> list[str]:
        r = httpx.get(f"{self.native_url}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def show(self, model: str, timeout: float = 15.0) -> dict:
        r = httpx.post(f"{self.native_url}/api/show", json={"name": model}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def effective_num_ctx(self, model: str) -> int | None:
        info = self.show(model)
        params = info.get("parameters", "")
        if isinstance(params, str):
            m = re.search(r"^\s*num_ctx\s+(\d+)", params, re.MULTILINE)
            if m:
                return int(m.group(1))
        elif isinstance(params, dict) and "num_ctx" in params:
            return int(params["num_ctx"])
        return None

    def max_context(self, model: str) -> int | None:
        """Model's max context from model_info '<arch>.context_length'; for display only."""
        info = self.show(model)
        for key, val in (info.get("model_info") or {}).items():
            if key.endswith("context_length") and isinstance(val, int):
                return val
        return None

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        timeout: float = 180.0,
    ) -> str:
        body: dict = {"model": model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        if response_format is not None:
            body["response_format"] = response_format
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""

    @staticmethod
    def strip_think(text: str) -> str:
        return _THINK_RE.sub("", text)

    @staticmethod
    def has_think(text: str) -> bool:
        return "<think" in text.lower()


# ==================================================================== schema normalization
def normalize_schema(node: Any) -> Any:
    """Rewrite optional-via-null unions to a plain type and drop those fields from `required`
    (SPEC.md 8.5). Handles BOTH the PROMPTS.md form {"type": ["X","null"]} and pydantic's
    {"anyOf": [{"type":"X"}, {"type":"null"}]}. Also unwraps single-element allOf and strips the
    advisory-only `title`/`default` keys so the result stays inside the constrained-decoding subset.
    """
    if isinstance(node, list):
        return [normalize_schema(x) for x in node]
    if not isinstance(node, dict):
        return node

    node = {k: v for k, v in node.items() if k not in ("title", "default")}

    # unwrap allOf: [single]
    if "allOf" in node and isinstance(node["allOf"], list) and len(node["allOf"]) == 1:
        merged = dict(node["allOf"][0])
        merged.update({k: v for k, v in node.items() if k != "allOf"})
        node = merged

    # anyOf with a null branch -> the non-null branch(es)
    if "anyOf" in node and isinstance(node["anyOf"], list):
        branches = node["anyOf"]
        non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
        if len(non_null) < len(branches) and len(non_null) == 1:
            merged = dict(non_null[0])
            merged.update({k: v for k, v in node.items() if k != "anyOf"})
            node = merged

    # type: ["X", "null"] -> "X"
    t = node.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        node["type"] = non_null[0] if len(non_null) == 1 else non_null

    # recurse and compute which properties were optional-via-null (to drop from required)
    if isinstance(node.get("properties"), dict):
        optional_keys = set()
        new_props = {}
        for key, sub in node["properties"].items():
            if _is_nullable(sub):
                optional_keys.add(key)
            new_props[key] = normalize_schema(sub)
        node["properties"] = new_props
        if optional_keys and isinstance(node.get("required"), list):
            node["required"] = [r for r in node["required"] if r not in optional_keys]
    if "items" in node:
        node["items"] = normalize_schema(node["items"])
    if isinstance(node.get("$defs"), dict):
        node["$defs"] = {k: normalize_schema(v) for k, v in node["$defs"].items()}
    if isinstance(node.get("additionalProperties"), dict):
        node["additionalProperties"] = normalize_schema(node["additionalProperties"])
    return node


def _is_nullable(sub: Any) -> bool:
    if not isinstance(sub, dict):
        return False
    t = sub.get("type")
    if isinstance(t, list) and "null" in t:
        return True
    if isinstance(sub.get("anyOf"), list):
        return any(isinstance(b, dict) and b.get("type") == "null" for b in sub["anyOf"])
    return False


def strict_schema(node: Any) -> Any:
    """Rewrite a normalized schema into the OpenAI-style `strict` dialect that hosted endpoints
    validate before running: every object carries `additionalProperties: false` and lists EVERY
    property in `required`; a property that was optional becomes `anyOf: [X, {"type":"null"}]`,
    which strict mode accepts and which pydantic accepts too, because every optional field in
    PROMPTS.md/schemas.py is `X | None = None`. Without this a strict request is rejected with
    HTTP 400 after the operator has confirmed the send and the payload has left the machine."""
    if isinstance(node, list):
        return [strict_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    if isinstance(node.get("additionalProperties"), dict):
        # A free-form dict field (dict[str, X]) has no strict-mode representation: strict rejects
        # any additionalProperties other than false. No shipped hosted schema has one; refusing
        # here turns a would-be HTTP 400 after the send into a clear error before it.
        raise ValueError(
            "strict schema: free-form dict fields (additionalProperties as a schema) cannot be "
            "sent to a hosted strict endpoint"
        )
    if isinstance(node.get("properties"), dict):
        required = set(node.get("required") or [])
        props = {}
        for key, sub in node["properties"].items():
            sub = strict_schema(sub)
            if key not in required and not _is_nullable(sub):
                sub = {"anyOf": [sub, {"type": "null"}]}
            props[key] = sub
        node["properties"] = props
        node["required"] = list(props)
        node["additionalProperties"] = False
    elif node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    if "items" in node:
        node["items"] = strict_schema(node["items"])
    if isinstance(node.get("anyOf"), list):
        node["anyOf"] = [strict_schema(b) for b in node["anyOf"]]
    if isinstance(node.get("$defs"), dict):
        node["$defs"] = {k: strict_schema(v) for k, v in node["$defs"].items()}
    return node


def drop_array_bounds(node: Any) -> Any:
    """Remove `minItems`/`maxItems`. The Claude API's structured outputs reject array bounds
    other than minItems 0 or 1; the bound is advisory everywhere and the schemas' validators cap
    the list anyway (schemas._dedupe_cap), so dropping it changes nothing but the 400."""
    if isinstance(node, list):
        return [drop_array_bounds(x) for x in node]
    if not isinstance(node, dict):
        return node
    return {k: drop_array_bounds(v) for k, v in node.items() if k not in ("minItems", "maxItems")}


# OpenRouter model families on which `maxItems`/`minItems` inside a strict json_schema are known
# to be accepted (structured_outputs on OpenAI and Google models). Array bounds are not on the
# verified constrained-decoding keyword list (CLAUDE.md, Platform traps), and Anthropic models
# reject any bound other than 0 or 1 with HTTP 400, so every other hosted request leaves them out;
# the post-parse cap (schemas._dedupe_cap) is the real guard either way.
_ARRAY_BOUNDS_SAFE_PREFIXES = ("openai/", "google/")


def keeps_array_bounds(model: str) -> bool:
    """Whether a hosted strict schema for OpenRouter `model` may carry array bounds."""
    return model.startswith(_ARRAY_BOUNDS_SAFE_PREFIXES)


def json_schema_for(model_cls: type) -> dict:
    """A constrained-decoding-safe JSON schema for a pydantic model: its json schema, normalized."""
    return normalize_schema(model_cls.model_json_schema())


# ==================================================================== request-body builder
_PROMPT_ONLY_RULE = (
    "\n\nReply with only that JSON object: no prose, no markdown fences, nothing before or after "
    "it. Use only the listed property names and enum values, and list each array value once."
)


def _embed_schema_in_system(
    messages: list[dict], schema: dict, *, prompt_only: bool = False
) -> list[dict]:
    text = "\n\nReturn a single JSON object conforming exactly to this JSON Schema:\n" + json.dumps(
        schema
    )
    if prompt_only:
        # No response_format at all (D-UI-48 "prompt" mode): the instruction is the only guide,
        # and the guard still validates the reply (hard rule 3).
        text += _PROMPT_ONLY_RULE
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + text
            return messages
    messages.insert(0, {"role": "system", "content": text.strip()})
    return messages


def build_request_body(
    *,
    model: str,
    messages: list[dict],
    backend: BackendConfig,
    temperature: float | None,
    max_tokens: int | None,
    schema: dict | None = None,
    schema_name: str = "response",
    reasoning_hint: str | None = None,
    caps: HostedModelCaps | None = None,
    repair: bool = False,
) -> dict:
    """Construct the exact /v1/chat/completions body for a backend. Crossing json_mode or
    thinking_control is silent, so each is driven strictly by the backend config, except that
    on OpenRouter the model's own recorded capability (`caps`, D-UI-48) picks the JSON mode."""
    body: dict = {"model": model, "messages": [dict(m) for m in messages]}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    mode = request_json_mode(backend, caps)
    if schema is not None:
        if mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": normalize_schema(schema)},
            }
        elif mode == "json_object":  # no grammar, so the schema goes into the system prompt
            body["response_format"] = {"type": "json_object"}
            body["messages"] = _embed_schema_in_system(body["messages"], normalize_schema(schema))
        else:  # "prompt": the model takes neither; the instruction alone asks for the object
            body["messages"] = _embed_schema_in_system(
                body["messages"], normalize_schema(schema), prompt_only=True
            )

    if backend.thinking_control == "reasoning_effort":
        body["reasoning_effort"] = "none"  # Ollama
    elif backend.thinking_control == "chat_template_kwargs":
        body["chat_template_kwargs"] = {"enable_thinking": False}  # oMLX / vLLM
    # thinking_control == "none": neither

    if backend.provider == "openrouter":
        _apply_openrouter_options(
            body, backend, reasoning_hint, mode=mode, caps=caps, repair=repair
        )
    elif backend.provider == "openai":
        _apply_openai_options(body, backend, reasoning_hint)
    return body


def request_json_mode(backend: BackendConfig, caps: HostedModelCaps | None = None) -> JsonMode:
    """The JSON mode one request uses. On OpenRouter a model's recorded capability wins (a model
    that lists only response_format cannot take a strict schema, and one that lists neither
    takes no response_format at all); everywhere else the backend's `json_mode` decides."""
    if backend.provider == "openrouter" and caps is not None:
        # "unknown" (the model's record could not be fetched when it was assigned) is sent as
        # JSON mode, which routes on any endpoint taking response_format, never strict.
        return "json_object" if caps.json_mode == "unknown" else caps.json_mode
    return "schema" if backend.json_mode == "json_schema" else "json_object"


# OpenAI model families that take no sampling parameters (temperature is rejected unless it is
# the default) and expose `reasoning_effort` instead. Matched on the id prefix; anything else
# (gpt-4o, gpt-4.1, chatgpt-*) is treated as a plain chat model.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

# OpenAI `reasoning_effort` values. "xhigh" and "max" are not accepted there and would turn every
# call into a 400, so a value outside this set sends nothing (model default). The Settings page
# offers exactly this list (providers.EFFORT_LEVELS mirrors it).
_OPENAI_EFFORTS = ("none", "minimal", "low", "medium", "high")


def openai_is_reasoning_model(model: str) -> bool:
    return model.startswith(_OPENAI_REASONING_PREFIXES)


def _apply_openai_options(
    body: dict, backend: BackendConfig, reasoning_hint: str | None = None
) -> None:
    """OpenAI-direct body fields. `max_tokens` is deprecated there in favor of
    `max_completion_tokens`, which every current model accepts and the reasoning families
    require. Reasoning models (o-series, gpt-5) reject a non-default temperature, so it is dropped
    for them rather than failing every call. The schema goes strict, as on OpenRouter, because
    OpenAI enforces the strict dialect (every property required, additionalProperties false)."""
    if "max_tokens" in body:
        body["max_completion_tokens"] = body.pop("max_tokens")
    if openai_is_reasoning_model(str(body.get("model") or "")):
        body.pop("temperature", None)
        if backend.hosted.reasoning_effort in _OPENAI_EFFORTS:
            body["reasoning_effort"] = backend.hosted.reasoning_effort
        elif backend.hosted.reasoning_effort is None and reasoning_hint in _OPENAI_EFFORTS:
            body["reasoning_effort"] = reasoning_hint
    if backend.json_mode == "json_schema" and "response_format" in body:
        js = body["response_format"]["json_schema"]
        js["strict"] = True
        # Array bounds are not verified on OpenAI-direct strict mode: left out (Platform traps).
        js["schema"] = drop_array_bounds(strict_schema(js["schema"]))


_PENALTY_FIELDS = ("repetition_penalty", "frequency_penalty")


def openrouter_effort_none_verified(caps: HostedModelCaps | None) -> bool:
    """Whether an OpenRouter model takes the verified reasoning-off shape,
    `reasoning: {effort: "none"}` (REFERENCES.md): its record lists the reasoning field and marks
    reasoning optional. Anything else (no record, mandatory reasoning, only `include_reasoning`)
    gets the unverified `enabled: false` fallback, which the model may ignore and keep thinking."""
    if caps is None or caps.reasoning_mandatory is not False or caps.params is None:
        return False
    return "reasoning" in caps.params or "reasoning_effort" in caps.params


def _apply_openrouter_options(
    body: dict,
    backend: BackendConfig,
    reasoning_hint: str | None = None,
    *,
    mode: JsonMode = "schema",
    caps: HostedModelCaps | None = None,
    repair: bool = False,
) -> None:
    """OpenRouter-only body fields (REFERENCES.md: provider routing, reasoning, usage accounting).
    `require_parameters` is what stops a schema'd request from silently landing on an endpoint
    that ignores response_format; `data_collection`/`zdr` are the privacy controls; `usage.include`
    makes the response carry the USD cost so the transmission record can state it.

    A JSON-mode or prompt-only model (D-UI-48) is sent with require_parameters off: there is no
    schema for an endpoint to ignore, and requiring every parameter would route the request
    nowhere. Its schema stays in the prompt and is not rewritten to the strict dialect."""
    h = backend.hosted
    require = h.require_parameters if mode == "schema" else False
    provider: dict = {
        "require_parameters": require,
        "data_collection": h.data_collection,
    }
    if h.zdr:
        provider["zdr"] = True
    body["provider"] = provider
    if h.reasoning_effort is not None:
        body["reasoning"] = {"effort": h.reasoning_effort}
    elif reasoning_hint == "none":
        # Reasoning off (D-IMPORT-4: `effort: low` left a DeepSeek reasoning model thinking
        # through the whole 3,000-token cap of an extraction call). The request schema (SDK
        # 1.3.59) carries only `reasoning.effort` (none among its values) and `summary`;
        # `enabled` appears on the MODEL record, where `mandatory` models reject effort none.
        # So: effort "none" where the model's record says it takes the reasoning field and
        # reasoning is not mandatory; otherwise `enabled: false`, the switch the model record
        # documents. Both need the live check on the operator's machine (REFERENCES.md); a
        # 400 either way quarantines with the provider's text (hard rule 3).
        if openrouter_effort_none_verified(caps):
            body["reasoning"] = {"effort": "none"}
        else:
            body["reasoning"] = {"enabled": False}
    elif reasoning_hint is not None:
        # The prompt's own hint (D-IMPORT-3): an extraction prompt needs little thinking, and a
        # reasoning model's default spends the output budget before the answer starts. The
        # operator's explicit setting above always wins.
        body["reasoning"] = {"effort": reasoning_hint}
    body["usage"] = {"include": True}
    known = caps.params if caps is not None else None
    def routable(name: str) -> bool:
        # With require_parameters on, OpenRouter routes only to an endpoint that supports EVERY
        # parameter sent, so a penalty beside a strict schema needs one endpoint with both; if
        # none exists every call is a 404. Sendable therefore when require_parameters is off (an
        # endpoint without the field just ignores it), or when the model's supported_parameters
        # lists both the penalty and structured_outputs. That list is the union over the model's
        # endpoints, so it cannot prove one endpoint has both: a residual risk, which surfaces
        # as the provider's 404 text, never as a silent drop.
        return not require or (
            known is not None and name in known and "structured_outputs" in known
        )

    for name in _PENALTY_FIELDS:
        value = getattr(h, name)
        if value is not None and routable(name):  # against generation loops (D-LLM-2)
            body[name] = value
    if (
        repair
        and h.repair_frequency_penalty is not None
        and not any(n in body for n in _PENALTY_FIELDS)
        and known is not None
        and "repetition_penalty" not in known
        and "frequency_penalty" in known
        and routable("frequency_penalty")
    ):
        # The repair of a looping reply (D-LLM-5) on a model that does not take
        # repetition_penalty: frequency_penalty, but only because the model lists it. Neither
        # listed: nothing is sent, and require_parameters is never turned off to make room for a
        # penalty (that could route a schema'd request to an endpoint ignoring response_format).
        body["frequency_penalty"] = h.repair_frequency_penalty
    rf = body.get("response_format")
    if mode == "schema" and isinstance(rf, dict) and rf.get("type") == "json_schema":
        js = rf["json_schema"]
        js["strict"] = True
        js["schema"] = strict_schema(js["schema"])
        if not keeps_array_bounds(str(body.get("model") or "")):
            # anthropic/* answers a maxItems above 1 with HTTP 400; outside the families where
            # bounds are known safe, they are left out (the post-parse cap still applies).
            js["schema"] = drop_array_bounds(js["schema"])


def build_responses_body(
    *,
    model: str,
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    schema: dict | None = None,
    schema_name: str = "response",
) -> dict:
    """The OpenAI Responses-protocol body (/v1/responses), used for openmodel.ai, whose
    chat-completions endpoint no longer exists. Same messages in, structured output requested via
    `text.format` rather than `response_format`. The schema is normalized exactly as in
    chat-completions mode so both protocols see one grammar."""
    # store: false asks the provider not to retain the request and output server-side (the
    # Responses protocol defaults to retention). OpenRouter's path has data_collection for this;
    # this is the openmodel.ai equivalent, and whether openmodel honors it is unverified.
    # System prompt goes in `instructions` (openmodel.ai and OpenAI both document it that way);
    # `input` carries only the user and assistant turns.
    system_parts = [m.get("content") or "" for m in messages if m.get("role") == "system"]
    turns = [dict(m) for m in messages if m.get("role") in ("user", "assistant")]
    body: dict = {"model": model, "input": turns, "store": False}
    if schema is not None:
        system_parts.append(
            "Reply with only a JSON object that matches the required schema, and nothing else."
        )
    if system_parts:
        body["instructions"] = "\n\n".join(system_parts)
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_output_tokens"] = max_tokens
    if schema is not None:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                # Array bounds are unverified on this protocol: left out (Platform traps).
                "schema": drop_array_bounds(strict_schema(normalize_schema(schema))),
            }
        }
    return body


def responses_output_text(data: dict) -> str:
    """Extract the assistant text from a Responses-protocol reply. Prefers the convenience field
    `output_text`; otherwise concatenates every `output_text` content part of every `message`
    output item, which is the documented canonical shape. Refusals and reasoning items are not
    text and are skipped, so a refusal surfaces as an empty (unparseable) response, not prose."""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                parts.append(part.get("text") or "")
    return "".join(parts)


# The Messages protocol version header. One value for every backend that speaks the protocol.
ANTHROPIC_VERSION = "2023-06-01"

# Anthropic `output_config.effort` levels. "none" and "minimal" are OpenRouter/OpenAI words and
# are not sent to the Claude API.
_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Claude models think by default and the thinking counts against `max_tokens`, which cannot be
# turned off on the current family. A tier's local cap (300 tokens for a Tier 1 verdict) would
# therefore stop the reply inside the thinking and quarantine every item; the cap is raised to this
# floor on the Claude API. It is a ceiling, not a spend: only produced tokens are billed.
ANTHROPIC_MIN_MAX_TOKENS = 8192


def build_messages_body(
    *,
    model: str,
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    schema: dict | None = None,
    schema_name: str = "response",
    backend: BackendConfig | None = None,
) -> dict:
    """The Anthropic Messages protocol body (/v1/messages). Two dialects:

    openmodel.ai (and generic): the system message becomes the top-level `system`; `max_tokens`
    is required by the protocol. Structured output is a forced tool call: the schema is the
    tool's `input_schema` and `tool_choice` names it, so the reply is the tool's `input` object
    and nothing else. Thinking is off unless requested, so nothing is sent for it.

    Claude API direct (`backend.provider == "anthropic"`): structured output is the API's own
    `output_config.format` json_schema, which guarantees the first content block is text holding
    valid JSON, and no tool is declared (current Claude models reject a forced `tool_choice`).
    No sampling parameter is sent: the Claude 5 family rejects `temperature`, and omitting it is
    harmless on older models. Thinking is left at the model's default and its depth is the
    operator's `hosted.reasoning_effort`, when it is one of the API's effort levels."""
    anthropic = backend is not None and backend.provider == "anthropic"
    system_parts = [m.get("content") or "" for m in messages if m.get("role") == "system"]
    turns = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    body: dict = {"model": model, "messages": turns, "max_tokens": max_tokens or 4096}
    if anthropic:
        body["max_tokens"] = max(body["max_tokens"], ANTHROPIC_MIN_MAX_TOKENS)
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        output_config: dict = {}
        if schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": drop_array_bounds(strict_schema(normalize_schema(schema))),
            }
        effort = backend.hosted.reasoning_effort if backend is not None else None
        if effort in _ANTHROPIC_EFFORTS:
            output_config["effort"] = effort
        if output_config:
            body["output_config"] = output_config
        return body
    if schema is not None:
        # openmodel.ai: "tool use may vary by provider" on the Messages protocol. The forced tool
        # call is the primary path; this one line lets a provider that answers in text instead
        # still produce the JSON object the validator expects (hard rule 3, no prose).
        system_parts.append(
            f"Answer by calling the tool `{schema_name}`. If you cannot call tools, reply with "
            "only a JSON object that matches that tool's input_schema, and nothing else."
        )
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if temperature is not None:
        body["temperature"] = temperature
    if schema is not None:
        body["tools"] = [
            {
                "name": schema_name,
                "description": "Return the structured result for this request.",
                "input_schema": drop_array_bounds(strict_schema(normalize_schema(schema))),
            }
        ]
        body["tool_choice"] = {"type": "tool", "name": schema_name}
    return body


def messages_output_text(data: dict) -> str:
    """Extract the reply from a Messages-protocol response. A `tool_use` block carries the
    structured object (serialized back to JSON for the same parse-and-validate path as every
    other protocol); otherwise the `text` blocks are joined. Thinking blocks are skipped."""
    tool_inputs = []
    texts: list[str] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            tool_inputs.append(block["input"])
        elif block.get("type") == "text":
            texts.append(block.get("text") or "")
    if tool_inputs:
        return json.dumps(tool_inputs[0])
    return "".join(texts)


def _never_left(exc: BaseException) -> bool:
    """True only when the request provably never reached the provider, so re-sending it cannot
    duplicate an egress.

    The distinction the `request error:` label cannot carry: a refused connection, a DNS failure or
    a connect timeout means no bytes arrived; an HTTP error status means the provider received the
    entire payload and answered (401 bad key, 429 rate limit, 500); a read timeout means the request
    went out and the answer was slow. Only the first group is safe to retry (review).

    Deliberately conservative: anything not positively identified as a connection failure counts as
    egressed: re-sending operator data off-machine is the one error that cannot be undone."""
    import httpx

    # A transport failure that is NOT a connect-phase failure.
    if isinstance(exc, ProviderError):
        return False
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        # Read/write timeouts and mid-stream drops happen AFTER the request was sent.
        return False
    # A refused connection is the one socket-level failure that happens before any byte is sent.
    # A reset or an abort can arrive after the provider has the whole request, so they count as
    # egressed like every other unproven case (D-RR1-5).
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, (socket.gaierror, socket.herror)):
        return True  # DNS
    return False


class ModelDeclined(RuntimeError):
    """A 200 whose content is, by the provider's own account, not an answer: a safety refusal
    (Anthropic `stop_reason: "refusal"`, OpenAI `message.refusal`) or a reply stopped at
    `max_tokens` inside the Messages protocol. The guard treats it as a structural failure of this
    call: no repair (the model would decline again), one quarantine row with the reason and the
    raw response, no worker retry (hard rule 3). It is not a request error: the request was
    accepted and billed."""

    def __init__(
        self,
        reason: str,
        raw: str,
        *,
        reasoning_exhausted: bool = False,
        degenerate: bool = False,
        truncated: bool = False,
        sent_max_tokens: int | None = None,
        external: bool = False,
        content: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float | None = None,
        latency_ms: int = 0,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw
        # The cap was spent on reasoning with nothing (or almost nothing) written: the one case
        # a re-ask with reasoning off can answer (D-IMPORT-4).
        self.reasoning_exhausted = reasoning_exhausted
        # The reply was a generation loop (one value repeated until the cap, D-LLM-2): a failed
        # attempt, so the guard makes its one repair rather than quarantining at once.
        self.degenerate = degenerate
        # The reply was cut at the output cap with no sign of a loop or of reasoning eating the
        # budget: a longer answer than the cap allowed. The guard retries it once with a larger
        # budget (D-LLM-7); `sent_max_tokens` is the cap this request actually carried.
        self.truncated = truncated
        self.sent_max_tokens = sent_max_tokens
        # Whether the backend was external. Only an external reply is retried larger: a local
        # model's num_ctx is sized per tier (SPEC 8.2), and a doubled output budget could push the
        # prompt past it, which Ollama truncates silently.
        self.external = external
        self.content = content
        # What the declined call cost: it was accepted and billed, so the guard adds it to the
        # transmission record like any other attempt.
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms


def messages_stop_problem(data: dict) -> str | None:
    """Why a Messages-protocol reply is not an answer, or None. Current Claude models answer HTTP
    200 with `stop_reason: "refusal"` (and a `stop_details` category) when a safety classifier
    declines; `stop_reason: "max_tokens"` means the reply, thinking included, hit the cap and any
    JSON in it is cut. Either way the content is not the structured object and must quarantine
    with that reason, never be parsed as one."""
    stop = data.get("stop_reason")
    if stop == "refusal":
        details = data.get("stop_details")
        category = details.get("category") if isinstance(details, dict) else None
        reason = "the provider's safety classifier declined this request (stop_reason: refusal"
        return f"{reason}, category: {category})" if category else f"{reason})"
    if stop == "max_tokens":
        return "the reply hit max_tokens before the structured object was complete"
    return None


def _complete_json(content: str) -> bool:
    """Whether a capped reply still holds a whole JSON object. A locate-and-load only, the same
    one the guard applies (no field parsing); schema validation happens in the guard."""
    from kestrel.llm.guard import extract_json

    try:
        return isinstance(json.loads(extract_json(content)), dict)
    except ValueError:
        return False


REASONING_RETRY_SHARE = 0.5


def choices_finish(data: dict) -> str | None:
    """The first choice's finish_reason of a chat-completions reply, or None."""
    choices = data.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    return first.get("finish_reason")


def reasoning_exhausted(data: dict) -> bool:
    """A chat-completions reply stopped at the cap with the budget spent on reasoning and no
    usable content: the provider's usage says so, or the content is empty at a length stop."""
    choices = data.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    if first.get("finish_reason") != "length":
        return False
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    used = usage.get("completion_tokens") or 0
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    content = (first.get("message") or {}).get("content") or ""
    if not content.strip():
        return True  # nothing written at all: the cap went to thinking (or to nothing)
    if reasoning and used:
        # Half or more of the budget went to thinking: a re-ask with reasoning off frees that
        # half for the answer. 0.9 missed the 0.4.x Tier 2 overruns (7,000 of 8,000 reasoning).
        return reasoning >= REASONING_RETRY_SHARE * used
    return False


def chat_stop_problem(
    data: dict,
    max_tokens: int | None = None,
    *,
    reasoning_off: bool = False,
    setting: str | None = None,
) -> str | None:
    """Why a chat-completions reply is not an answer, or None. `finish_reason: "length"` means
    the output, reasoning included, hit the cap: the JSON is cut or, on a reasoning model whose
    thinking used the whole budget, empty. Parsing it gave "invalid JSON: Expecting value" and a
    repair at the same cap failed the same way, a minute and two bills later (0.2.0 import). The
    reason names the budget and how much of it went to reasoning so the fix is clear.

    `reasoning_off` is true when this request already asked for no reasoning (the guard's re-ask,
    or a prompt that declares `reasoning: none`): lowering the effort cannot help then, so the
    text says the reply needs more output tokens and names `setting`, the key that raises the
    budget (D-LLM-1)."""
    choices = data.get("choices") or [{}]
    finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
    if finish == "content_filter":
        return "the provider's content filter stopped this reply (finish_reason: content_filter)"
    if finish != "length":
        return None
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    used = usage.get("completion_tokens")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    cap = f"max_tokens {max_tokens}" if max_tokens else "max_tokens"
    spent = f"; {used} output tokens" if used else ""
    if reasoning:
        spent += f", {reasoning} of them reasoning"
    if reasoning_off:
        where = f"raise {setting}" if setting else "raise the tier's max_tokens"
        return (
            f"the reply hit {cap} before the structured object was complete{spent}, with "
            f"reasoning already off. The reply needs more output tokens: {where}"
        )
    if setting:
        return (
            f"the reply hit {cap} before the structured object was complete{spent}. Raise "
            f"{setting}, or lower the reasoning effort under Settings > Hosted"
        )
    return (
        f"the reply hit {cap} before the structured object was complete{spent}. Raise the "
        "prompt's max_tokens, or lower the reasoning effort under Settings > Hosted"
    )


class ProviderError(RuntimeError):
    """An HTTP error from an LLM backend with the provider's own message attached, so a 401 (bad
    key), 402 (no credits), 404 (no endpoint honors the parameters), or 429 (rate limit) reads as
    what it is in the quarantine record rather than as a bare status line."""

    def __init__(
        self, status: int, message: str, url: str, codes: tuple[str, ...] = ()
    ) -> None:
        if status == 404 and "data policy" in message.lower():
            # OpenRouter: every endpoint for this model was excluded by the privacy rule Kestrel
            # sends (data_collection deny), usually because it is a free model (D-UI-32).
            message = (
                f"{message}. In plain words: no provider serves this model under Kestrel's "
                "privacy setting, which blocks providers that may train on your data and "
                "overrides your OpenRouter account setting. Pick a paid model, or allow those "
                "providers under Settings > Hosted (data collection: allowed)"
            )
        super().__init__(f"HTTP {status} from {url}: {message}")
        self.status = status
        self.message = message
        self.url = url
        # The error code/type strings the body carried (`INSUFFICIENT_BALANCE`, `billing_error`),
        # for classifying the failure; never shown (D-SEC-2).
        self.codes = codes


_HTTP_HINTS = {
    401: "API key rejected; add a working key in Settings > Hosted",
    402: "insufficient credits or balance; top up at the provider",
    403: "quota exceeded or permission denied by the provider",
    404: "no endpoint serves this model with the parameters sent (structured output required)",
    429: "rate limited by the provider; retry later",
}


def raise_for_provider_status(r: httpx.Response) -> None:
    """raise_for_status with the provider's error body folded into the exception message. Never
    includes request headers, so the key cannot reach a log through this path."""
    if r.is_success:
        return
    message = ""
    codes: list[str] = []
    try:
        body = r.json()
        err = body.get("error")
        for src in (err, body):
            if isinstance(src, dict):
                codes += [str(src[k]) for k in ("code", "type") if isinstance(src.get(k), str)]
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            meta = err.get("metadata")
            if isinstance(meta, dict) and meta.get("provider_name"):
                message = f"{message} (provider: {meta['provider_name']})"
        elif isinstance(err, str):
            message = err
            codes.append(err)
    except (ValueError, AttributeError):
        message = ""
    hint = _HTTP_HINTS.get(r.status_code, "")
    text = "; ".join(x for x in (message[:300], hint) if x) or r.reason_phrase
    raise ProviderError(r.status_code, text, str(r.request.url), tuple(codes))


# ==================================================================== async pipeline client
@dataclass
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    request_body: dict = field(default_factory=dict)
    # USD cost as reported by the provider (OpenRouter with usage.include), else None. A hosted call
    # whose cost is unknown says so; it is never rendered as zero.
    cost_usd: float | None = None


class ExternalBackendError(RuntimeError):
    """Raised when code tries to reach an `external: true` backend without explicit opt-in: either
    a confirmed Tier 3 escalation (allow_external=True) or Hosted mode turned on AND acknowledged
    by the operator (settings.llm.hosted_mode, SPEC.md 6.4)."""


# (backend, model, tier) triples already announced, so the hosted-mode notice is logged once per
# process per routing, never once per call. Never carries the key.
_HOSTED_ANNOUNCED: set[tuple[str, str, str]] = set()


def _announce_hosted(backend: str, model: str, tier: str) -> None:
    key = (backend, model, tier)
    if key in _HOSTED_ANNOUNCED:
        return
    _HOSTED_ANNOUNCED.add(key)
    import structlog

    structlog.get_logger().info("hosted_mode_active", backend=backend, model=model, tier=tier)


def _resolve_api_key(backend: BackendConfig) -> str:
    if backend.api_key:
        return backend.api_key
    if backend.api_key_env:
        from kestrel.config import get_secret

        return get_secret(backend.api_key_env) or ""
    return "x"


def tier_credit_hold(settings: Settings, tier: str) -> str | None:
    """The provider name when the backend `tier` routes to is on hold as out of credit for its
    current key (D-LLM-8), else None. Reads settings and process memory only; sends nothing."""
    try:
        backend, tcfg = LLMClient(settings)._backend_and_tier(tier)
    except (KeyError, ValueError):
        return None
    return credit.held_provider(tcfg.backend, _resolve_api_key(backend))


class LLMClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http

    def _backend_and_tier(self, tier: str) -> tuple[BackendConfig, Any]:
        tcfg = self.settings.llm.tiers[tier]
        return self.settings.llm.backends[tcfg.backend], tcfg

    def _hosted_tier(self, tier: str, backend: BackendConfig) -> bool:
        """Whether this call runs a tier through its Hosted mode assignment (not a Tier 3
        escalation to the frontier tier, which keeps its own budget)."""
        return (
            backend.external
            and self.settings.llm.mode == "hosted"
            and tier in self.settings.llm.hosted_tiers
        )

    def _external_floor(self, tier: str, backend: BackendConfig) -> int | None:
        """The hosted output floor (config.HOSTED_MAX_TOKENS) for a tier whose backend is
        external, whatever the run mode (D-LLM-7). The prompt's own max_tokens is sized for the
        local models, and a paid reasoning model spends tokens thinking before it writes; a tier
        pointed at an external backend while the mode is local (a settings.yml edit, a legacy
        row) needs the same room. None for a local backend and for tiers with no hosted floor
        (the frontier tier keeps its own budget)."""
        from kestrel.config import HOSTED_MAX_TOKENS

        return HOSTED_MAX_TOKENS.get(tier) if backend.external else None

    def _reasoning_off(
        self, tier: str, backend: BackendConfig, reasoning_hint: str | None
    ) -> bool:
        """Whether this request asked the provider for no reasoning: the operator's effort
        setting, or a `none` hint the provider takes (the guard's re-ask, or the prompt's own)."""
        if backend.hosted.reasoning_effort is not None:
            return backend.hosted.reasoning_effort == "none"
        return reasoning_hint == "none" and self.reasoning_hint_applies(tier)

    def _reasoning_off_verified(
        self, tier: str, backend: BackendConfig, reasoning_hint: str | None
    ) -> bool:
        """Whether the request actually carries a reasoning-off shape the provider is verified to
        honor. On OpenRouter a `none` hint is sent as `effort: "none"` only for a model whose
        record says reasoning is optional; otherwise it falls back to `enabled: false`, the
        model may keep thinking, and a repair budget sized for the bare answer would be eaten.
        The operator's own effort setting is sent as written, so it counts as asked."""
        if not self._reasoning_off(tier, backend, reasoning_hint):
            return False
        if backend.hosted.reasoning_effort is not None or backend.provider != "openrouter":
            return True
        _, tcfg = self._backend_and_tier(tier)
        return openrouter_effort_none_verified(self.settings.llm.hosted_model_caps.get(tcfg.model))

    def reasoning_hint_applies(self, tier: str) -> bool:
        """Whether a prompt's reasoning hint changes the request for `tier`: a hosted provider
        that takes the field, and no operator-set effort (which always wins). The guard re-asks
        with reasoning off only when that re-ask would differ from the call that just failed."""
        backend, _ = self._backend_and_tier(tier)
        return backend.provider in ("openrouter", "openai") and (
            backend.hosted.reasoning_effort is None
        )

    def default_reasoning_hint(self, tier: str) -> str | None:
        """The reasoning hint a first attempt on `tier` gets when neither the prompt nor the
        operator set one (D-LLM-5). Tier 1 triage is a short classification, so on Hosted mode,
        with `hosted.tier1_reasoning_off` on (the default), an OpenRouter model whose record says
        it takes reasoning and that reasoning is optional is asked for none: the request then
        carries `reasoning: {effort: "none"}` (the SDK-verified shape, REFERENCES.md). A model
        whose record is missing or marks reasoning mandatory gets nothing, so the switch can
        never turn every triage call into a 400."""
        if tier != "tier1":
            return None
        backend, tcfg = self._backend_and_tier(tier)
        h = backend.hosted
        if (
            backend.provider != "openrouter"
            or not self._hosted_tier(tier, backend)
            or h.reasoning_effort is not None
            or not h.tier1_reasoning_off
        ):
            return None
        # Only the verified shape: a record listing `include_reasoning` alone would make the
        # body builder fall back to `enabled: false`, which is not verified to stop thinking.
        if not openrouter_effort_none_verified(self.settings.llm.hosted_model_caps.get(tcfg.model)):
            return None
        return "none"

    def json_mode_for(self, tier: str) -> JsonMode:
        """How a structured request on `tier` asks for JSON: "schema" (a grammar or a forced
        tool), "json_object" (JSON mode) or "prompt" (the instruction alone). The guard accepts
        JSON located inside prose only in "prompt" mode (and on its repair attempt)."""
        backend, tcfg = self._backend_and_tier(tier)
        if backend.protocol in ("responses", "messages"):
            return "schema"
        caps = (
            self.settings.llm.hosted_model_caps.get(tcfg.model)
            if backend.provider == "openrouter"
            else None
        )
        return request_json_mode(backend, caps)

    async def complete(
        self,
        *,
        tier: str,
        messages: list[dict],
        schema: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        schema_name: str = "response",
        timeout: float | None = None,
        allow_external: bool = False,
        reasoning_hint: str | None = None,
        repair: bool = False,
        max_tokens_cap: int | None = None,
    ) -> ChatResult:
        """One structured completion. temperature/max_tokens passed here (from the prompt) WIN;
        the tier config is the fallback. `repair` marks the guard's repair attempt (it may carry
        a loop penalty, D-LLM-5); `max_tokens_cap` lowers the budget below the hosted floor for
        that repair, applied only when reasoning is off for the request (or the backend is
        local), so a cap sized for the answer is never eaten by thinking. A backend marked
        `external: true` is refused unless the caller explicitly passes allow_external=True (a
        confirmed Tier 3 escalation) OR the operator has turned on and acknowledged Hosted mode
        (SPEC.md 6.4), so the worker, MCP, or any other path can never transmit operator data
        off the machine by accident (hard rule 1).
        """
        backend, tcfg = self._backend_and_tier(tier)
        if backend.engine != "http":
            raise ValueError(
                f"backend '{tcfg.backend}' for tier '{tier}' is the in-process embedding engine; "
                "it serves llm.tiers.embed only and cannot complete a prompt"
            )
        if backend.external and not allow_external:
            hm = self.settings.llm.hosted_mode
            if hm.enabled and hm.acknowledged_at and not hm.acknowledgement_current:
                from kestrel.config import HOSTED_DISCLOSURE_CHANGED

                raise ExternalBackendError(
                    f"backend '{tcfg.backend}' for tier '{tier}' is external and "
                    f"{HOSTED_DISCLOSURE_CHANGED}; nothing was sent"
                )
            if not hm.active:
                raise ExternalBackendError(
                    f"backend '{tcfg.backend}' for tier '{tier}' is external; it may be reached "
                    "only through operator-confirmed Tier 3 escalation or after Hosted mode is "
                    "turned on and acknowledged under Settings > Hosted, never automatically"
                )
            _announce_hosted(tcfg.backend, tcfg.model, tier)
        temp = temperature if temperature is not None else tcfg.temperature
        max_out = max_tokens if max_tokens is not None else tcfg.max_tokens
        hosted_tier = self._hosted_tier(tier, backend)
        floor = self._external_floor(tier, backend)
        if hosted_tier and tcfg.max_tokens is not None and max_out is not None:
            # Under Hosted mode the tier's budget (HOSTED_MAX_TOKENS unless the operator set
            # one) is a floor under the prompt's, which is sized for the local models (D-LLM-1).
            max_out = max(max_out, tcfg.max_tokens)
        elif floor is not None and max_out is not None:
            # An external backend outside Hosted mode (D-LLM-7): the same floor.
            max_out = max(max_out, floor, tcfg.max_tokens or 0)
            hosted_tier = True
        if (
            max_tokens_cap is not None
            and max_out is not None
            and (
                not backend.external
                or self._reasoning_off_verified(tier, backend, reasoning_hint)
            )
        ):
            # A repair after a looping reply (D-LLM-5): a second loop fails fast at a budget
            # sized to the schema instead of burning the whole hosted cap. On a hosted tier the
            # cap is never below the prompt's own budget (D-LLM-6): P2's legitimate reply runs
            # 2,500 to 3,000 tokens, so its 2,500 repair cap cut good replies off mid-object.
            cap = max_tokens_cap
            if hosted_tier and max_tokens is not None:
                cap = max(cap, max_tokens)
            max_out = min(max_out, cap)
        # The request timeout is per tier (a 6000-token Tier 2 JD behind Ollama's queue can take
        # longer than a Tier 1 verdict), with the explicit argument winning and 180s as the floor
        # default. A timeout is a retryable request error, never a quarantine.
        if timeout is None:
            timeout = tcfg.timeout_s if tcfg.timeout_s is not None else 180.0
        if backend.protocol == "responses":
            body = build_responses_body(
                model=tcfg.model,
                messages=messages,
                temperature=temp,
                max_tokens=max_out,
                schema=schema,
                schema_name=schema_name,
            )
            url = f"{backend.base_url.rstrip('/')}/responses"
        elif backend.protocol == "messages":
            body = build_messages_body(
                model=tcfg.model,
                messages=messages,
                temperature=temp,
                max_tokens=max_out,
                schema=schema,
                schema_name=schema_name,
                backend=backend,
            )
            url = f"{backend.base_url.rstrip('/')}/messages"
        else:
            body = build_request_body(
                model=tcfg.model,
                messages=messages,
                backend=backend,
                temperature=temp,
                max_tokens=max_out,
                schema=schema,
                schema_name=schema_name,
                reasoning_hint=reasoning_hint,
                caps=self.settings.llm.hosted_model_caps.get(tcfg.model)
                if backend.provider == "openrouter"
                else None,
                repair=repair,
            )
            url = f"{backend.base_url.rstrip('/')}/chat/completions"
        key = _resolve_api_key(backend)
        headers = {"Authorization": f"Bearer {key}", **backend.extra_headers}
        if backend.protocol == "messages":
            # The Messages protocol authenticates with x-api-key and needs a version header; the
            # bearer header is kept because openmodel.ai accepts either. The Claude API itself
            # reads a bearer as an OAuth token, so only x-api-key goes to it.
            headers["x-api-key"] = key
            headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
            if backend.provider == "anthropic":
                headers.pop("Authorization", None)
        held = credit.held_provider(tcfg.backend, key)
        if held is not None:
            # This backend already answered "out of credit" for this key: send nothing more to
            # it, and choose no other provider in its place (D-LLM-8).
            raise OutOfCredit(backend.provider, sent=False)
        t0 = time.perf_counter()
        client = self._http or httpx.AsyncClient()

        def _credit_check(exc: ProviderError) -> None:
            if credit.is_out_of_credit(backend.provider, exc.status, exc.codes, exc.message):
                if credit.mark_out_of_credit(tcfg.backend, backend.provider, key):
                    import structlog

                    structlog.get_logger().warning(
                        "llm_out_of_credit", backend=tcfg.backend, provider=backend.provider
                    )
                raise OutOfCredit(backend.provider) from exc

        try:
            r = await client.post(url, json=body, headers=headers, timeout=timeout)
            try:
                raise_for_provider_status(r)
            except ProviderError as exc:
                _credit_check(exc)
                if exc.status == 400 and schema is not None:
                    # The provider refused the structured request itself (a schema keyword it
                    # does not take, a context overflow). The same request would be refused
                    # again, so this is a failed attempt that quarantines with the provider's
                    # text (hard rule 3), not a request error the worker retries forever.
                    raise ModelDeclined(
                        f"the provider rejected the structured request (HTTP 400): "
                        f"{exc.message}",
                        (r.text or "")[:4000],
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                    ) from exc
                raise
            data = r.json()
        finally:
            if self._http is None:
                await client.aclose()
        if isinstance(data, dict) and data.get("error"):
            # OpenRouter (and others) can answer 200 with only an error object for an upstream
            # failure; without this the guard sees KeyError('choices') and loses the message.
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            codes = tuple(
                str(err[k]) for k in ("code", "type") if isinstance(err, dict) and err.get(k)
            )
            inner = err.get("code") if isinstance(err, dict) else None
            status = inner if isinstance(inner, int) and inner >= 400 else r.status_code
            perr = ProviderError(status, str(msg or "provider error")[:300], url, codes)
            _credit_check(perr)
            raise perr
        usage = data.get("usage") or {}
        spent = {
            "prompt_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "completion_tokens": int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            ),
            "cost_usd": float(usage["cost"]) if isinstance(usage.get("cost"), int | float)
            else None,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        if backend.protocol == "responses":
            content = responses_output_text(data)
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
        elif backend.protocol == "messages":
            problem = messages_stop_problem(data)
            if problem:
                raise ModelDeclined(
                    problem, json.dumps(data)[:4000],
                    truncated=data.get("stop_reason") == "max_tokens",
                    sent_max_tokens=body.get("max_tokens"), external=backend.external, **spent,
                )
            content = messages_output_text(data)
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
        else:
            message = data["choices"][0]["message"]
            problem = chat_stop_problem(
                data,
                body.get("max_tokens") or body.get("max_completion_tokens"),
                reasoning_off=self._reasoning_off(tier, backend, reasoning_hint),
                setting=(
                    f"llm.hosted_tiers.{tier}.max_tokens"
                    if self._hosted_tier(tier, backend)
                    else f"llm.tiers.{tier}.max_tokens" if hosted_tier else None
                ),
            )
            text = message.get("content") or ""
            if problem and not _complete_json(text):
                # A JSON-mode model can finish the object and then pad to the cap with
                # whitespace: that reply is whole, so it goes on to validation, not quarantine.
                # A budget spent on reasoning is checked FIRST: a long legitimate reply (a P7
                # resume import) cut at the cap is not a loop, and re-asking with reasoning off
                # is the one remedy that can help it.
                if reasoning_exhausted(data):
                    raise ModelDeclined(problem, json.dumps(data)[:4000],
                                        reasoning_exhausted=True, **spent)
                from kestrel.llm.guard import degenerate_repeat, loop_limit_for

                loop = degenerate_repeat(text, loop_limit_for(schema_name))
                if loop:
                    # The cap was reached by a loop, not by a long answer: say so, and let the
                    # guard spend its one repair on it (D-LLM-2).
                    raise ModelDeclined(f"{loop}; {problem}", json.dumps(data)[:4000],
                                        degenerate=True, content=text, **spent)
                raise ModelDeclined(
                    problem, json.dumps(data)[:4000],
                    truncated=(choices_finish(data) == "length"),
                    sent_max_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
                    external=backend.external, **spent,
                )
            if message.get("refusal"):
                # OpenAI structured outputs: a safety refusal comes back in `refusal` with a null
                # `content`; without this it would quarantine as "invalid JSON" and lose the reason.
                raise ModelDeclined(
                    f"the provider declined this request: {str(message['refusal'])[:300]}",
                    json.dumps(data)[:4000],
                    **spent,
                )
            content = message.get("content") or ""
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
        cost = usage.get("cost")
        return ChatResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            model=tcfg.model,
            request_body=body,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )

    async def embed(
        self, texts: list[str], *, tier: str = "embed", timeout: float = 60.0
    ) -> list[list[float]]:
        """Return raw embedding vectors. Serialize with embeddings.to_bytes.

        Two engines, both on this machine: an HTTP backend's /v1/embeddings (Ollama), or the
        bundled in-process model (`engine: inprocess`, SPEC.md 8.1.1), run in a worker thread so
        the event loop never blocks on it. An external backend is refused unconditionally:
        embeddings have no hosted path (no provider endpoint is wired, no disclosure covers them),
        so neither Hosted mode nor a Tier 3 confirmation can send the operator's text this way."""
        backend, tcfg = self._backend_and_tier(tier)
        if backend.external:
            raise ExternalBackendError(
                f"backend '{tcfg.backend}' for tier '{tier}' is external; embeddings have no "
                "hosted path. Point llm.tiers.embed.backend at `local` or `inprocess`."
            )
        if backend.engine == "inprocess":
            from kestrel.llm import inprocess_embed

            return await inprocess_embed.embed_async(texts, model=tcfg.model)
        headers = {"Authorization": f"Bearer {_resolve_api_key(backend)}"}
        url = f"{backend.base_url.rstrip('/')}/embeddings"
        body = {"model": tcfg.model, "input": texts}
        client = self._http or httpx.AsyncClient()
        try:
            r = await client.post(url, json=body, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        finally:
            if self._http is None:
                await client.aclose()
        return [item["embedding"] for item in data["data"]]

    @staticmethod
    def strip_think(text: str) -> str:
        return _THINK_RE.sub("", text)
