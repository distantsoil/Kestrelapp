"""Bring-your-own-key hosted backends (SPEC.md 6.4 / 8.7): the OpenRouter body carries the privacy
routing fields and asks for cost; the Responses protocol (openmodel.ai) builds and parses
correctly; provider errors read as what they are; the data-home .env is really loaded and a key
rotation is picked up; the Settings page stores a key without echoing it, tests it against a
mocked provider, and the frontier form repoints only the frontier tier; doctor warns (no longer
fails) when another tier is routed off-machine while Hosted mode is off (tests/test_hosted_mode.py
covers the mode itself). Every network call is respx-mocked (no live calls)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
import respx
from httpx import Response

from kestrel import paths
from kestrel.config import (
    BackendConfig,
    HostedOptions,
    get_secret,
    get_settings,
    load_dotenv_into_environ,
)
from kestrel.db import migrate
from kestrel.db.engine import reset_engine
from kestrel.llm import providers as prov
from kestrel.llm.client import (
    LLMClient,
    ProviderError,
    build_request_body,
    build_responses_body,
    raise_for_provider_status,
    responses_output_text,
)

_MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def _openrouter(**hosted) -> BackendConfig:
    return BackendConfig(
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        json_mode="json_schema",
        thinking_control="none",
        external=True,
        provider="openrouter",
        hosted=HostedOptions(**hosted),
    )


def _openmodel() -> BackendConfig:
    return BackendConfig(
        base_url="https://api.openmodel.ai/v1",
        api_key_env="OPENMODEL_API_KEY",
        thinking_control="none",
        external=True,
        provider="openmodel",
        protocol="responses",
    )


# --------------------------------------------------------------------------- request bodies
def test_openrouter_body_carries_privacy_routing_cost_and_strict_schema():
    body = build_request_body(
        model="anthropic/claude-sonnet-4.6",
        messages=_MSGS,
        backend=_openrouter(zdr=True, reasoning_effort="low"),
        temperature=0.3,
        max_tokens=100,
        schema=_SCHEMA,
        schema_name="t3",
    )
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny", "zdr": True}
    assert body["reasoning"] == {"effort": "low"}
    assert body["usage"] == {"include": True}
    assert body["response_format"]["json_schema"]["strict"] is True
    # strict dialect: every property required, no extras (see test_review_fixes for the walker)
    assert body["response_format"]["json_schema"]["schema"] == {
        **_SCHEMA, "additionalProperties": False
    }
    # No Ollama thinking control leaks onto a hosted request.
    assert "reasoning_effort" not in body and "chat_template_kwargs" not in body


def test_openrouter_defaults_send_no_zdr_and_no_reasoning():
    body = build_request_body(
        model="m", messages=_MSGS, backend=_openrouter(), temperature=None, max_tokens=None
    )
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert "reasoning" not in body


def test_local_backend_body_is_untouched_by_provider_fields():
    body = build_request_body(
        model="m",
        messages=_MSGS,
        backend=BackendConfig(base_url="http://x/v1"),
        temperature=0.0,
        max_tokens=10,
        schema=_SCHEMA,
    )
    assert "provider" not in body and "usage" not in body
    assert "strict" not in body["response_format"]["json_schema"]


def test_responses_body_uses_text_format_and_max_output_tokens():
    body = build_responses_body(
        model="gpt-x",
        messages=_MSGS,
        temperature=0.1,
        max_tokens=50,
        schema=_SCHEMA,
        schema_name="s",
    )
    assert body == {
        "model": "gpt-x",
        "input": [{"role": "user", "content": "hi"}],
        "store": False,
        "instructions": "sys\n\nReply with only a JSON object that matches the required schema, "
        "and nothing else.",
        "temperature": 0.1,
        "max_output_tokens": 50,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "s",
                "strict": True,
                "schema": {**_SCHEMA, "additionalProperties": False},
            }
        },
    }


def test_responses_output_text_walks_message_items_and_skips_reasoning():
    data = {
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"x":'}]},
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]},
            {"type": "message", "content": [{"type": "output_text", "text": ' "1"}'}]},
        ]
    }
    assert responses_output_text(data) == '{"x": "1"}'
    assert responses_output_text({"output_text": "direct"}) == "direct"
    assert responses_output_text({}) == ""


# -------------------------------------------------------------------------- client transport
def _settings_with(backend_name: str, backend: BackendConfig, model: str):
    s = get_settings()
    s.llm.backends[backend_name] = backend
    s.llm.tiers["frontier"].backend = backend_name
    s.llm.tiers["frontier"].model = model
    return s


@respx.mock
def test_responses_protocol_posts_to_responses_and_parses(monkeypatch):
    monkeypatch.setenv("OPENMODEL_API_KEY", "om-test")
    route = respx.post("https://api.openmodel.ai/v1/responses").mock(
        return_value=Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": '{"x":"ok"}'}]}
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )
    )
    s = _settings_with("openmodel", _openmodel(), "openai/gpt-x")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    res = asyncio.run(go())
    assert route.called
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer om-test"
    assert b'"max_output_tokens"' in sent.content and b'"text"' in sent.content
    assert res.content == '{"x":"ok"}'
    assert (res.prompt_tokens, res.completion_tokens, res.cost_usd) == (12, 4, None)


@respx.mock
def test_openrouter_cost_is_captured_and_key_comes_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"content": '{"x":"ok"}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0042},
            },
        )
    )
    s = _settings_with("frontier", _openrouter(), "anthropic/claude-sonnet-4.6")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    res = asyncio.run(go())
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-or-test"
    assert res.cost_usd == pytest.approx(0.0042)


def test_provider_error_reads_as_what_it_is():
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    r = Response(
        402,
        json={"error": {"message": "Insufficient credits", "metadata": {"provider_name": "X"}}},
        request=req,
    )
    with pytest.raises(ProviderError) as ei:
        raise_for_provider_status(r)
    msg = str(ei.value)
    assert "402" in msg and "Insufficient credits" in msg and "provider: X" in msg
    assert "insufficient credits" in msg  # the hint
    # A non-JSON body still produces a readable error, never a crash.
    with pytest.raises(ProviderError):
        raise_for_provider_status(Response(429, text="slow down", request=req))


# ----------------------------------------------------------------------------- secrets (.env)
def test_dotenv_is_loaded_and_rotation_is_picked_up(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = paths.dotenv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OPENROUTER_API_KEY=sk-or-one\n")
    get_settings()
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-one"
    assert "OPENROUTER_API_KEY" not in os.environ  # private store only (D-BYOK-11)
    # Rotate in the file: the new value replaces it without a restart.
    path.write_text("OPENROUTER_API_KEY=sk-or-two\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert load_dotenv_into_environ() is True
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-two"
    # Remove the file: the key goes away too.
    path.unlink()
    assert get_secret("OPENROUTER_API_KEY") is None


def test_kestrel_dotenv_key_beats_a_real_environment_variable(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-real-env")
    path = paths.dotenv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OPENROUTER_API_KEY=from-file\n")
    os.utime(path, (1, 1))  # force an mtime the loader has not seen
    assert get_secret("OPENROUTER_API_KEY") == "from-file"
    assert os.environ["OPENROUTER_API_KEY"] == "from-real-env"  # never overwritten
    path.unlink()


def test_write_secret_roundtrip_and_refusals(monkeypatch):
    monkeypatch.delenv("OPENMODEL_API_KEY", raising=False)
    prov.write_secret("OPENMODEL_API_KEY", "om-abc")
    assert "OPENMODEL_API_KEY=om-abc" in paths.dotenv_path().read_text()
    assert get_secret("OPENMODEL_API_KEY") == "om-abc"
    assert "OPENMODEL_API_KEY" not in os.environ
    assert prov.masked("om-abcdefghij") == "********ghij"
    prov.write_secret("OPENMODEL_API_KEY", "")
    assert "OPENMODEL_API_KEY" not in paths.dotenv_path().read_text()
    assert get_secret("OPENMODEL_API_KEY") is None
    with pytest.raises(ValueError):
        prov.write_secret("bad name", "x")
    with pytest.raises(ValueError):
        prov.write_secret("OPENMODEL_API_KEY", "x\nINJECTED=1")
    with pytest.raises(ValueError, match="not a key name"):
        prov.write_secret("KESTREL_LOG_LEVEL", "DEBUG")


# --------------------------------------------------------------------------- provider service
@respx.mock
def test_verify_key_and_list_models_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    respx.get("https://openrouter.ai/api/v1/key").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "label": "kestrel",
                    "usage": 1.25,
                    "limit": 10,
                    "limit_remaining": 8.75,
                    "is_free_tier": False,
                }
            },
        )
    )
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "a/one",
                        "name": "One",
                        "context_length": 200000,
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        "supported_parameters": ["structured_outputs", "tools"],
                    },
                    {"id": "b/two", "supported_parameters": ["tools"]},
                    {"id": "c/three"},
                ]
            },
        )
    )
    b = _openrouter()
    key = asyncio.run(prov.verify_key(b))
    assert key.ok and key.label == "kestrel" and key.limit_remaining_usd == 8.75
    models = prov.models_fit_for_tier3(asyncio.run(prov.list_models(b)), b)
    ids = [m.id for m in models]
    assert "a/one" in ids and "c/three" in ids  # unknown capability is kept, labeled unknown
    # D-UI-48: a model that takes neither response_format nor structured_outputs is admitted,
    # asked in the prompt alone and labeled "no JSON guarantee"
    two = next(m for m in models if m.id == "b/two")
    assert two.json_mode == "prompt" and prov.json_mode_label(two) == "no JSON guarantee"
    one = next(m for m in models if m.id == "a/one")
    assert one.prompt_usd_per_m == pytest.approx(3.0)
    assert one.completion_usd_per_m == pytest.approx(15.0)
    assert one.structured_outputs is True


@respx.mock
def test_verify_key_openmodel_uses_models_and_filters_by_protocol(monkeypatch):
    monkeypatch.setenv("OPENMODEL_API_KEY", "om-test")
    respx.get("https://api.openmodel.ai/v1/models").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {"id": "openai/gpt-x", "supported_apis": ["responses"]},
                    {"id": "anthropic/claude-x", "supported_apis": ["messages"]},
                ]
            },
        )
    )
    b = _openmodel()
    assert asyncio.run(prov.verify_key(b)).ok
    fit = prov.models_fit_for_tier3(asyncio.run(prov.list_models(b)), b)
    assert [m.id for m in fit] == ["openai/gpt-x", "anthropic/claude-x"]  # responses + messages


def test_verify_key_without_a_key_never_calls_out(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://openrouter.ai/api/v1/key").mock(return_value=Response(200, json={}))
        st = asyncio.run(prov.verify_key(_openrouter()))
        assert not st.ok and "not set" in st.detail
        assert not router.calls


# ------------------------------------------------------------------------------ settings page
def _fresh_app():
    from starlette.testclient import TestClient

    from kestrel.web.app import build_app

    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations(paths.db_path())
    asyncio.run(reset_engine())
    return TestClient(build_app(worker=False))


def test_settings_hosted_flow(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENMODEL_API_KEY", raising=False)
    client = _fresh_app()

    # The page has the tab, and the partial shows both external backends with no key.
    assert "Hosted (BYOK)" in client.get("/settings").text
    body = client.get("/settings/hosted").text
    assert "openmodel" in body and "no key saved" in body
    # Key names are for power users only: under Show details (release round R1).
    assert "OPENROUTER_API_KEY" in body.split('data-testid="key-details"')[1]

    # Save a key: it lands in .env, the partial says present, and the value is never echoed.
    r = client.post(
        "/settings/hosted/key", data={"backend": "frontier", "api_key": "sk-or-secret9"}
    )
    assert r.status_code == 200 and "saved, ends in" in r.text and "ret9" in r.text
    assert "sk-or-secret9" not in r.text
    assert "OPENROUTER_API_KEY=sk-or-secret9" in paths.dotenv_path().read_text()

    # Test it against a mocked provider: key status and a model list appear.
    with respx.mock:
        respx.get("https://openrouter.ai/api/v1/key").mock(
            return_value=Response(200, json={"data": {"label": "k", "usage": 0.5, "limit": None}})
        )
        respx.get("https://openrouter.ai/api/v1/models").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"id": "z/last", "supported_parameters": ["structured_outputs"]},
                        {"id": "a/first", "supported_parameters": ["structured_outputs"]},
                    ]
                },
            )
        )
        r = client.post("/settings/hosted/test", data={"backend": "frontier"})
    assert r.status_code == 200 and "accepted" in r.text and "a/first" in r.text
    assert r.text.index("a/first") < r.text.index("z/last")

    # Choose a model with ZDR on: only the frontier tier moves, and the option is persisted.
    r = client.post(
        "/settings/hosted/model",
        data={
            "backend": "frontier",
            "model": "a/first",
            "zdr": ["off", "on"],
            "data_collection": "deny",
            "reasoning_effort.frontier": "none",
        },
    )
    assert r.status_code == 200 and "Deeper reads now use a/first on " in r.text
    assert "frontier /" not in r.text  # the service is named, never the backend key

    async def _check():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        async with get_sessionmaker()() as s:
            return await load_settings_with_db(s)

    st = asyncio.run(_check())
    assert st.llm.tiers["frontier"].model == "a/first"
    assert st.llm.tiers["frontier"].backend == "frontier"
    assert st.llm.backends["frontier"].hosted.zdr is True
    assert st.llm.backends["frontier"].hosted.reasoning_effort == "none"
    assert st.llm.tiers["tier1"].backend == "local" and st.llm.tiers["tier2"].backend == "local"

    # An unknown backend or a non-external one is refused; nothing is written.
    r = client.post("/settings/hosted/model", data={"backend": "local", "model": "kestrel-tier1"})
    assert "Pick an external backend" in r.text
    r = client.post("/settings/hosted/key", data={"backend": "local", "api_key": "x"})
    assert "Unknown hosted backend" in r.text

    # Clearing the key removes it from .env.
    r = client.post("/settings/hosted/key", data={"backend": "frontier", "api_key": ""})
    assert "OpenRouter key removed." in r.text
    assert "OPENROUTER_API_KEY" not in paths.dotenv_path().read_text()


# -------------------------------------------------------------------------------------- doctor
def test_doctor_warns_not_fails_when_a_tier_is_external_while_hosted_mode_is_off():
    """Hosted mode (SPEC 6.4) replaced the old hard FAIL: a non-frontier tier on an external
    backend is a WARN that says the tier will not be used until the mode is turned on."""
    from kestrel.doctor import FAIL, WARN, DoctorReport, _check_hosted

    s = get_settings()
    s.llm.tiers["tier2"].backend = "frontier"
    rep = DoctorReport()
    _check_hosted(rep, s)
    assert not [c for c in rep.results if c.status == FAIL]
    row = next(c for c in rep.results if c.name == "Hosted routing")
    assert row.status == WARN and "tier2 -> frontier" in row.detail
    assert "will not be used until Hosted mode is turned on" in row.detail


def test_doctor_reports_frontier_provider_and_protocol(monkeypatch):
    from kestrel.doctor import WARN, DoctorReport, _check_hosted

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rep = DoctorReport()
    _check_hosted(rep, get_settings())
    row = next(c for c in rep.results if c.name == "Tier 3 frontier")
    assert row.status == WARN and "openrouter, chat_completions" in row.detail


# ------------------------------------------------------------------ final-review follow-ups
def test_public_hostnames_refuse_wildcards_and_normalize():
    from pydantic import ValidationError

    from kestrel.config import Server

    assert Server(public_hostnames=[" K.Example.COM ", ""]).public_hostnames == ["k.example.com"]
    for bad in ("*", "*.example.com", "https://k.example.com", "k.example.com:8765"):
        with pytest.raises(ValidationError):
            Server(public_hostnames=[bad])


def test_responses_body_asks_for_no_retention():
    body = build_responses_body(model="m", messages=_MSGS, temperature=None, max_tokens=None)
    assert body["store"] is False


def test_strict_schema_refuses_free_form_dict_fields():
    from kestrel.llm.client import strict_schema

    with pytest.raises(ValueError):
        strict_schema(
            {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}
        )


@respx.mock
def test_200_with_error_object_is_a_provider_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=Response(200, json={"error": {"message": "upstream exploded", "code": 502}})
    )
    s = _settings_with("frontier", _openrouter(), "m")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, allow_external=True
            )

    with pytest.raises(ProviderError) as ei:
        asyncio.run(go())
    assert "upstream exploded" in str(ei.value)


@respx.mock
def test_verify_key_survives_a_non_json_200(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    respx.get("https://openrouter.ai/api/v1/key").mock(
        return_value=Response(200, text="<html>challenge</html>")
    )
    st = asyncio.run(prov.verify_key(_openrouter()))
    assert not st.ok and "unexpected provider response" in st.detail


def test_write_secret_with_a_real_environment_variable_saves_for_kestrel_only(monkeypatch):
    monkeypatch.setenv("OPENMODEL_API_KEY", "from-shell")
    prov.write_secret("OPENMODEL_API_KEY", "om-new")
    assert get_secret("OPENMODEL_API_KEY") == "om-new"
    assert os.environ["OPENMODEL_API_KEY"] == "from-shell"


# --------------------------------------------------------------- openmodel Messages protocol
def _openmodel_messages() -> BackendConfig:
    b = _openmodel()
    b.protocol = "messages"
    return b


def test_messages_body_forces_a_tool_call_carrying_the_schema():
    from kestrel.llm.client import build_messages_body

    body = build_messages_body(
        model="anthropic/claude-x", messages=_MSGS, temperature=0.2, max_tokens=90,
        schema=_SCHEMA, schema_name="t3",
    )
    assert body["system"].startswith("sys\n\nAnswer by calling the tool `t3`")
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 90 and body["temperature"] == 0.2
    assert body["tool_choice"] == {"type": "tool", "name": "t3"}
    assert body["tools"][0]["input_schema"] == {**_SCHEMA, "additionalProperties": False}
    assert "thinking" not in body


def test_messages_output_prefers_tool_use_and_skips_thinking():
    from kestrel.llm.client import messages_output_text

    data = {
        "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "Here you go"},
            {"type": "tool_use", "name": "t3", "input": {"x": "ok"}},
        ]
    }
    assert messages_output_text(data) == '{"x": "ok"}'
    assert messages_output_text({"content": [{"type": "text", "text": "plain"}]}) == "plain"


@respx.mock
def test_messages_protocol_posts_to_messages_with_x_api_key(monkeypatch):
    monkeypatch.setenv("OPENMODEL_API_KEY", "om-test")
    route = respx.post("https://api.openmodel.ai/v1/messages").mock(
        return_value=Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": "response", "input": {"x": "ok"}}],
                "usage": {"input_tokens": 20, "output_tokens": 5},
                "stop_reason": "tool_use",
            },
        )
    )
    s = _settings_with("openmodel", _openmodel_messages(), "anthropic/claude-x")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    res = asyncio.run(go())
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "om-test" and req.headers["anthropic-version"]
    assert b'"tool_choice"' in req.content
    assert res.content == '{"x": "ok"}' and (res.prompt_tokens, res.completion_tokens) == (20, 5)


def test_openmodel_models_fit_by_either_protocol():
    b = _openmodel_messages()
    models = [
        prov.ModelInfo(id="a", protocols=["messages"]),
        prov.ModelInfo(id="b", protocols=["responses"]),
        prov.ModelInfo(id="c", protocols=["gemini"]),
        prov.ModelInfo(id="d"),
    ]
    assert [m.id for m in prov.models_fit_for_tier3(models, b)] == ["a", "b", "d"]
    assert prov.protocol_for_model(models[0]) == "messages"
    assert prov.protocol_for_model(models[1]) == "responses"
    assert prov.protocol_for_model(models[2]) is None


# ------------------------------------------------------------- Anthropic and OpenAI direct
def _anthropic(**hosted) -> BackendConfig:
    return BackendConfig(
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        thinking_control="none",
        external=True,
        provider="anthropic",
        protocol="messages",
        hosted=HostedOptions(**hosted),
    )


def _openai(**hosted) -> BackendConfig:
    return BackendConfig(
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        thinking_control="none",
        external=True,
        provider="openai",
        protocol="chat_completions",
        hosted=HostedOptions(**hosted),
    )


def test_shipped_settings_carry_both_direct_backends_as_external():
    from kestrel.config import get_settings

    b = get_settings().llm.backends
    assert b["anthropic"].provider == "anthropic" and b["anthropic"].protocol == "messages"
    assert b["openai"].provider == "openai" and b["openai"].protocol == "chat_completions"
    for name in ("anthropic", "openai"):
        assert b[name].external and b[name].api_key_env and b[name].api_key is None
    assert set(prov.PRESETS) == {"openrouter", "openmodel", "anthropic", "openai"}
    assert {p["backend"] for p in prov.PRESETS.values()} <= set(b)


def test_anthropic_body_uses_output_config_and_sends_no_sampling_or_tools():
    from kestrel.llm.client import ANTHROPIC_MIN_MAX_TOKENS, build_messages_body

    body = build_messages_body(
        model="claude-haiku-5-5", messages=_MSGS, temperature=0.2, max_tokens=90,
        schema=_SCHEMA, schema_name="t3", backend=_anthropic(reasoning_effort="low"),
    )
    assert body["system"] == "sys"  # no tool instruction appended
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == ANTHROPIC_MIN_MAX_TOKENS  # the tier's 90 is below the floor
    assert "temperature" not in body and "tools" not in body and "tool_choice" not in body
    assert "thinking" not in body
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == {**_SCHEMA, "additionalProperties": False}
    assert body["output_config"]["effort"] == "low"


def test_anthropic_effort_ignores_words_the_claude_api_does_not_take():
    from kestrel.llm.client import build_messages_body

    body = build_messages_body(
        model="claude-haiku-5-5", messages=_MSGS, temperature=None, max_tokens=90,
        schema=None, backend=_anthropic(reasoning_effort="none"),
    )
    assert "output_config" not in body


def test_openmodel_messages_body_is_unchanged_by_the_backend_argument():
    from kestrel.llm.client import build_messages_body

    with_backend = build_messages_body(
        model="m", messages=_MSGS, temperature=0.2, max_tokens=90, schema=_SCHEMA,
        backend=_openmodel_messages(),
    )
    without = build_messages_body(
        model="m", messages=_MSGS, temperature=0.2, max_tokens=90, schema=_SCHEMA
    )
    assert with_backend == without and with_backend["tool_choice"]["type"] == "tool"


@respx.mock
def test_anthropic_posts_with_x_api_key_only_and_parses_the_text_block(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"x": "ok"}'}],
                "usage": {"input_tokens": 20, "output_tokens": 5},
                "stop_reason": "end_turn",
                "stop_details": None,
            },
        )
    )
    s = _settings_with("anthropic", _anthropic(), "claude-haiku-5-5")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    res = asyncio.run(go())
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in {k.lower() for k in req.headers}
    assert b'"output_config"' in req.content and b'"tool_choice"' not in req.content
    assert b'"temperature"' not in req.content
    assert res.content == '{"x": "ok"}' and (res.prompt_tokens, res.completion_tokens) == (20, 5)


@respx.mock
def test_anthropic_refusal_quarantines_once_with_its_category_and_never_retries(monkeypatch):
    """A 200 with stop_reason refusal is not a request error (which the worker would retry three
    times, billing each) and not a parse failure (which would hide the reason): it quarantines at
    once, with the category, and the guard does not attempt a repair."""
    from kestrel.llm.client import ModelDeclined
    from kestrel.llm.guard import run_guarded
    from kestrel.llm.schemas import P1Triage

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=Response(
            200,
            json={
                "content": [],
                "usage": {"input_tokens": 20, "output_tokens": 0},
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
            },
        )
    )
    s = _settings_with("anthropic", _anthropic(), "claude-haiku-5-5")
    seen: list[tuple] = []

    async def hook(messages, raw, error, model):
        seen.append((raw, error, model))

    async def go():
        async with httpx.AsyncClient() as http:
            client = LLMClient(s, http=http)
            with pytest.raises(ModelDeclined):
                await client.complete(
                    tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
                )
            return await run_guarded(
                client, tier="frontier", messages=_MSGS, model_cls=P1Triage,
                allow_external=True, on_quarantine=hook,
            )

    res = asyncio.run(go())
    assert not res.ok and res.quarantined and "refusal" in res.error and "cyber" in res.error
    assert not res.error.startswith("request error")
    assert len(seen) == 1 and seen[0][2] == "claude-haiku-5-5" and "refusal" in seen[0][0]
    assert len(route.calls) == 2  # one direct call above, one from the guard: no repair attempt


def test_messages_stop_at_max_tokens_is_declined_not_parsed():
    from kestrel.llm.client import messages_stop_problem

    assert messages_stop_problem({"stop_reason": "end_turn"}) is None
    assert "max_tokens" in (messages_stop_problem({"stop_reason": "max_tokens"}) or "")
    assert "refusal" in (messages_stop_problem({"stop_reason": "refusal"}) or "")


def test_anthropic_max_tokens_floor_covers_default_thinking():
    from kestrel.llm.client import ANTHROPIC_MIN_MAX_TOKENS, build_messages_body

    low = build_messages_body(
        model="claude-haiku-5-5", messages=_MSGS, temperature=None, max_tokens=300,
        schema=_SCHEMA, backend=_anthropic(),
    )
    assert low["max_tokens"] == ANTHROPIC_MIN_MAX_TOKENS
    high = build_messages_body(
        model="claude-haiku-5-5", messages=_MSGS, temperature=None, max_tokens=20000,
        schema=_SCHEMA, backend=_anthropic(),
    )
    assert high["max_tokens"] == 20000
    # openmodel.ai keeps the tier's cap untouched
    om = build_messages_body(
        model="m", messages=_MSGS, temperature=None, max_tokens=300, schema=_SCHEMA,
        backend=_openmodel_messages(),
    )
    assert om["max_tokens"] == 300


@respx.mock
def test_anthropic_models_list_reads_context_and_structured_output_capability(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    route = respx.get("https://api.anthropic.com/v1/models", params={"limit": "1000"}).mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-haiku-5-5",
                        "display_name": "Claude Haiku 5.5",
                        "max_input_tokens": 1000000,
                        "capabilities": {"structured_outputs": {"supported": True}},
                    },
                    {
                        "id": "claude-old",
                        "display_name": "Old",
                        "capabilities": {"structured_outputs": {"supported": False}},
                    },
                    {"id": "claude-unknown", "display_name": "Unknown"},
                ],
                "has_more": False,
            },
        )
    )
    b = _anthropic()

    async def go():
        async with httpx.AsyncClient() as http:
            return await prov.verify_key(b, http=http), await prov.list_models(b, http=http)

    key, models = asyncio.run(go())
    assert key.ok and "3 models" in key.detail
    assert route.calls[0].request.headers["x-api-key"] == "sk-ant-test"
    assert "authorization" not in {k.lower() for k in route.calls[0].request.headers}
    by_id = {m.id: m for m in models}
    assert by_id["claude-haiku-5-5"].name == "Claude Haiku 5.5"
    assert by_id["claude-haiku-5-5"].context_length == 1_000_000
    assert by_id["claude-haiku-5-5"].structured_outputs is True
    assert by_id["claude-old"].structured_outputs is False
    assert by_id["claude-unknown"].structured_outputs is None
    fit = {m.id for m in prov.models_fit_for_tier3(models, b)}
    assert fit == {"claude-haiku-5-5", "claude-unknown"}  # unknown kept, never hidden


def test_openai_body_uses_max_completion_tokens_and_the_strict_dialect():
    from kestrel.llm.client import build_request_body

    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}, "y": {"type": ["integer", "null"]}},
        "required": ["x"],
    }
    body = build_request_body(
        model="gpt-4.1-mini", messages=_MSGS, backend=_openai(), temperature=0.3,
        max_tokens=120, schema=schema, schema_name="t1",
    )
    assert "max_tokens" not in body and body["max_completion_tokens"] == 120
    assert body["temperature"] == 0.3  # a plain chat model keeps it
    js = body["response_format"]["json_schema"]
    assert js["strict"] is True and js["name"] == "t1"
    assert js["schema"]["additionalProperties"] is False
    assert set(js["schema"]["required"]) == {"x", "y"}
    assert "provider" not in body and "usage" not in body  # OpenRouter-only fields


def test_openai_reasoning_models_drop_temperature_and_take_reasoning_effort():
    from kestrel.llm.client import build_request_body

    body = build_request_body(
        model="gpt-5-mini", messages=_MSGS, backend=_openai(reasoning_effort="low"),
        temperature=0.3, max_tokens=120, schema=_SCHEMA,
    )
    assert "temperature" not in body and body["reasoning_effort"] == "low"
    # xhigh and max are Anthropic/OpenRouter words; OpenAI would answer 400, so nothing is sent
    capped = build_request_body(
        model="gpt-5-mini", messages=_MSGS, backend=_openai(reasoning_effort="max"),
        temperature=0.3, max_tokens=120, schema=_SCHEMA,
    )
    assert "reasoning_effort" not in capped and "temperature" not in capped
    plain = build_request_body(
        model="gpt-4o", messages=_MSGS, backend=_openai(reasoning_effort="low"),
        temperature=0.3, max_tokens=120, schema=_SCHEMA,
    )
    assert plain["temperature"] == 0.3 and "reasoning_effort" not in plain


@respx.mock
def test_openai_models_list_drops_non_chat_families(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    route = respx.get("https://api.openai.com/v1/models").mock(
        return_value=Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-5-mini", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
                    {"id": "text-embedding-3-small", "object": "model"},
                    {"id": "whisper-1", "object": "model"},
                    {"id": "gpt-4o-mini-tts", "object": "model"},
                    {"id": "gpt-4o-audio-preview", "object": "model"},
                    {"id": "dall-e-3", "object": "model"},
                    {"id": "gpt-image-1", "object": "model"},
                    {"id": "gpt-4o-realtime-preview", "object": "model"},
                    {"id": "omni-moderation-latest", "object": "model"},
                ],
            },
        )
    )
    b = _openai()

    async def go():
        async with httpx.AsyncClient() as http:
            return await prov.verify_key(b, http=http), await prov.list_models(b, http=http)

    key, models = asyncio.run(go())
    assert key.ok and route.calls[0].request.headers["authorization"] == "Bearer sk-proj-test"
    assert [m.id for m in models] == ["gpt-5-mini", "gpt-4.1", "gpt-4o-audio-preview"]
    assert all(m.structured_outputs is None for m in models)
    assert [m.id for m in prov.models_fit_for_tier3(models, b)] == [m.id for m in models]
    assert prov.free_models(models) == []  # no prices reported, so nothing is called free


@respx.mock
def test_openai_completion_posts_to_chat_completions_with_a_bearer(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": '{"x": "ok"}'}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 6},
            },
        )
    )
    s = _settings_with("openai", _openai(), "gpt-5-mini")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    res = asyncio.run(go())
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-proj-test"
    assert b'"max_completion_tokens"' in req.content and b'"max_tokens"' not in req.content
    assert res.content == '{"x": "ok"}' and (res.prompt_tokens, res.completion_tokens) == (30, 6)
    assert res.cost_usd is None  # OpenAI reports no cost in usage


def test_setup_default_picks_prefer_the_suggested_models_when_listed():
    from kestrel.setup import default_picks

    pool = [
        prov.ModelInfo(id="claude-fable-5-1", context_length=1_000_000),
        prov.ModelInfo(id="claude-haiku-5-5", context_length=1_000_000),
        prov.ModelInfo(id="claude-sonnet-5-5", context_length=1_000_000),
    ]
    picks = default_picks(pool, "anthropic")
    assert {t: m.id for t, m in picks.items()} == {
        "tier1": "claude-haiku-5-5",
        "tier2": "claude-sonnet-5-5",
        "docsel": "claude-haiku-5-5",
    }
    # Without the suggestion the generic rule still finds the fast member of the family.
    assert default_picks(pool, "generic")["tier1"].id == "claude-haiku-5-5"
    # A suggestion the provider does not list falls through to the generic rule for that tier.
    partial = default_picks(pool[:1] + pool[2:], "anthropic")
    assert partial["tier2"].id == "claude-sonnet-5-5" and partial["tier1"].id == "claude-fable-5-1"


@respx.mock
def test_openai_refusal_is_declined_with_its_reason(monkeypatch):
    from kestrel.llm.client import ModelDeclined

    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None, "refusal": "I can't."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 6},
            },
        )
    )
    s = _settings_with("openai", _openai(), "gpt-5-mini")

    async def go():
        async with httpx.AsyncClient() as http:
            return await LLMClient(s, http=http).complete(
                tier="frontier", messages=_MSGS, schema=_SCHEMA, allow_external=True
            )

    with pytest.raises(ModelDeclined) as exc:
        asyncio.run(go())
    assert "declined" in exc.value.reason and "I can't." in exc.value.reason
    assert '"refusal"' in exc.value.raw


def test_openai_audio_and_search_previews_stay_listed():
    assert prov.openai_is_chat_model("gpt-4o-audio-preview")
    assert prov.openai_is_chat_model("gpt-4o-search-preview")
    assert not prov.openai_is_chat_model("gpt-4o-mini-tts")
    assert not prov.openai_is_chat_model("gpt-4o-realtime-preview")


def test_settings_offers_reasoning_effort_per_provider_with_only_its_words(monkeypatch):
    """The effort control is one row per external backend with a control, each list holding only
    the words that provider accepts; a value outside the list is stored as the model default."""
    import re

    client = _fresh_app()
    body = client.get("/settings/hosted").text
    assert 'data-testid="effort-per-provider"' in body
    for name in ("frontier", "anthropic", "openai"):
        assert f'name="reasoning_effort.{name}"' in body
    assert 'name="reasoning_effort.openmodel"' not in body  # no wired control there

    def options(name: str) -> list[str]:
        m = re.search(rf'<select name="reasoning_effort\.{name}">(.*?)</select>', body, re.S)
        assert m, name
        return re.findall(r'value="([a-z]*)"', m.group(1))

    assert options("anthropic") == ["", "low", "medium", "high", "xhigh", "max"]
    assert options("openai") == ["", "none", "minimal", "low", "medium", "high"]
    assert options("frontier") == ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

    r = client.post(
        "/settings/hosted/model",
        data={
            "backend": "anthropic",
            "model": "claude-haiku-5-5",
            "reasoning_effort.anthropic": "low",
            "reasoning_effort.openai": "max",  # not an OpenAI word: stored as model default
            "reasoning_effort.frontier": "xhigh",
        },
    )
    assert r.status_code == 200
    assert "Deeper reads now use claude-haiku-5-5 on anthropic.com" in r.text

    async def _check():
        from kestrel.db.engine import get_sessionmaker
        from kestrel.services.setting_overrides import load_settings_with_db

        async with get_sessionmaker()() as s:
            return await load_settings_with_db(s)

    st = asyncio.run(_check())
    assert st.llm.backends["anthropic"].hosted.reasoning_effort == "low"
    assert st.llm.backends["openai"].hosted.reasoning_effort is None
    assert st.llm.backends["frontier"].hosted.reasoning_effort == "xhigh"
    assert st.llm.tiers["frontier"].backend == "anthropic"
    # The saved value reaches the request body on the right provider.
    from kestrel.llm.client import build_messages_body

    sent = build_messages_body(
        model="claude-haiku-5-5", messages=_MSGS, temperature=None, max_tokens=300,
        schema=_SCHEMA, backend=st.llm.backends["anthropic"],
    )
    assert sent["output_config"]["effort"] == "low"
