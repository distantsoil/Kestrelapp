"""Other service: private storage, model-list-only test and the existing hosted consent gates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import zipfile
from html import unescape

import httpx
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select

from kestrel import config, paths
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.llm import providers as prov
from kestrel.services.other_service import KEY_ENV, URL_ENV, Connection
from tests.test_hosted_mode import _db_settings
from tests.test_settings_page import _client

URL = "https://private-service.example/v1"
KEY = "private-test-key-123456789"
MODELS = {"data": [{"id": "custom-chat", "context_length": 128000}]}


def _post(client, op="save", **values):
    return client.post(
        "/settings/computer/key",
        data={
            "provider": "other",
            "base_url": URL,
            "api_key": KEY,
            "op": op,
            **values,
        },
    )


def _rows():
    async def read():
        async with get_sessionmaker()() as s:
            return dict((await s.execute(select(Setting.key, Setting.value))).all())

    return asyncio.run(read())


@pytest.mark.parametrize(
    "url",
    [
        "https://service.example/v1/",
        "http://localhost:8000/v1",
        "http://127.0.0.1/v1",
        "http://[::1]:8000/v1",
        "https://service.example:8443/custom/api",
    ],
)
def test_valid_addresses(url):
    assert Connection(base_url=url).base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://service.example/v1",
        "http://192.168.1.5/v1",
        "ftp://localhost/v1",
        "https://user:password@service.example/v1",
        "https://service.example/v1?key=secret",
        "https://service.example/v1#part",
        "https://service.example:0/v1",
        "https://service.example:abc/v1",
        "https://[broken/v1",
        "https://service.example/a b",
        "https://service.example/${TOKEN}",
        "https://service.example/\nkey",
    ],
)
def test_invalid_addresses_never_write_or_contact_service(url):
    with _client() as c, respx.mock(assert_all_called=False) as router:
        r = _post(c, base_url=url)
        assert r.status_code == 200
        assert not router.calls
    assert not paths.dotenv_path().exists()
    assert not _rows()
    assert KEY not in r.text


def test_save_is_env_only_and_survives_a_reload(tmp_path):
    with _client() as c, respx.mock(assert_all_called=False) as router:
        r = _post(c)
        assert not router.calls
        assert r.text.count(' id="st-mode"') == 1
        assert 'hx-target="#st-mode" hx-swap="outerHTML"' in r.text
        assert KEY not in r.text
        assert URL in r.text
        assert "Service address and key saved" in r.text
        assert 'data-st-svc="other"' in r.text
    text = paths.dotenv_path().read_text(encoding="utf-8")
    assert f"{URL_ENV}={URL}" in text and f"{KEY_ENV}={KEY}" in text
    assert URL_ENV not in os.environ and KEY_ENV not in os.environ
    assert not _rows()
    config._dotenv_state["mtime"] = None
    settings = config.load_settings()
    backend = settings.llm.backends["other"]
    assert backend.base_url == URL and backend.api_key_env == KEY_ENV
    assert backend.provider == "generic" and backend.protocol == "chat_completions"
    assert backend.external and not settings.llm.hosted_mode.acknowledged_at
    assert settings.llm.mode == "local"
    for p in paths.config_dir().rglob("*.y*ml"):
        assert URL not in p.read_text(encoding="utf-8")
        assert KEY not in p.read_text(encoding="utf-8")
    from kestrel.services.backup import create_backup

    with zipfile.ZipFile(create_backup(tmp_path / "backups")) as z:
        assert not any(n.endswith(".env") for n in z.namelist())
        for n in z.namelist():
            data = z.read(n)
            assert URL.encode() not in data and KEY.encode() not in data, n


@pytest.mark.parametrize("status", [200, 401, 500])
def test_connection_test_only_lists_models_and_saves_nothing(status, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    with _client() as c, respx.mock as router:
        models = router.get(URL + "/models").respond(status, json=MODELS)
        r = _post(c, "test")
        assert models.call_count == 1
        request = models.calls.last.request
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert not request.content
    assert ("The connection works" if status == 200 else "The connection failed") in r.text
    assert not paths.dotenv_path().exists() and not _rows()
    assert KEY not in r.text and URL in r.text  # preserve the address draft, never the key
    assert KEY not in caplog.text and "private-service.example" not in caplog.text


def test_saved_key_can_test_without_echoing_it_and_replacement_is_not_saved():
    with _client() as c:
        _post(c)
        with respx.mock as router:
            models = router.get(URL + "/models").respond(200, json=MODELS)
            assert "connection works" in _post(c, "test", api_key="").text
            assert models.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
        with respx.mock as router:
            router.get(URL + "/models").respond(401)
            assert "connection failed" in _post(c, "test", api_key="rejected-key").text
    assert prov.secret_value(KEY_ENV) == KEY


def test_no_key_and_invalid_key_are_refused():
    with _client() as c:
        assert "Paste your service key first" in _post(c, api_key="").text
        for key in ("abc\ndef", "abc\rdef", "${HOME}"):
            assert "without spaces" in _post(c, api_key=key).text
    assert not paths.dotenv_path().exists()
    with pytest.raises(ValidationError):
        Connection(base_url=URL, api_key="key\n")


def test_connect_uses_existing_approval_and_models_then_endpoint_change_revokes_it(monkeypatch):
    from kestrel.services import setup_state

    monkeypatch.setattr(setup_state, "probe_ollama", lambda settings: (None, []))
    with _client() as c:
        _post(c)
        body = c.get("/models/online").text
        assert 'value="other"' in body and "Use Other service" in body
        for sends in config.load_settings().llm.hosted_mode.disclosure_terms()["sends"].values():
            assert sends in unescape(body)
        assert 'name="ack" value="on" checked' not in body
        assert "style=" not in body
        with respx.mock(assert_all_called=False) as router:
            models = router.get(URL + "/models").respond(200, json=MODELS)
            refused = c.post("/models/online/connect", data={"switch": "on"})
            assert "nothing was saved" in refused.text and not router.calls
            connected = c.post("/models/online/connect", data={"ack": "on", "switch": "on"})
            assert "Connected to Other service" in connected.text
            assert models.call_count == 2
        st = _db_settings()
        assert st.llm.mode == "hosted" and st.llm.hosted_mode.acknowledgement_current
        assert st.llm.tiers["tier1"].backend == "other"
        assert st.llm.tiers["tier1"].model == "custom-chat"
        assert URL not in json.dumps(_rows()) and KEY not in json.dumps(_rows())
        # Same destination keeps approval; a new destination must be approved again.
        _post(c)
        assert _db_settings().llm.hosted_mode.acknowledgement_current
        _post(c, base_url="https://replacement.example/v1")
        assert not _db_settings().llm.hosted_mode.acknowledgement_current


def test_other_uses_generic_schema_client_and_per_action_consent():
    from kestrel.llm.client import ExternalBackendError, LLMClient
    from kestrel.services import online_consent as consent

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    with _client() as c:
        _post(c)
        st = _db_settings()
        st.llm.tiers["tier1"].backend = "other"
        st.llm.tiers["tier1"].model = "custom-chat"

        async def call():
            async with httpx.AsyncClient() as http:
                return await LLMClient(st, http=http).complete(
                    tier="tier1",
                    messages=[{"role": "user", "content": "test"}],
                    schema=schema,
                )

        with respx.mock(assert_all_called=False) as router:
            router.post(URL + "/chat/completions").respond(200, json={})
            with pytest.raises(ExternalBackendError):
                asyncio.run(call())
            assert not router.calls
        dest = consent.destination_id(st, "tier1")
        assert URL in dest  # the private address is part of what a confirmation approves
        route = {"online": True, "hosted_active": True, "model": "custom-chat",
                 "employer": "hidden", "dest": dest}
        ok = {"confirmed": "1", "model": "custom-chat", "dest": dest}
        assert consent.consent_problem(route, {})
        assert consent.consent_problem(route, {**ok, "model": "old-model"})
        assert consent.consent_problem(route, {**ok, "dest": "other|https://old.example|x"})
        assert consent.consent_problem(route, ok) is None
        assert consent.consent_problem({**route, "hosted_active": False}, ok)
        assert consent.consent_problem({**route, "employer": "held"}, ok)


def test_private_logs_cover_saved_and_unsaved_values_including_exceptions(caplog):
    from kestrel.private_logging import private_values

    with _client() as c:
        _post(c)
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("other-service-test")
    logger.info("destination %s key %s", URL, KEY)
    with private_values("https://unsaved.example/v1", "unsaved-private-key"):
        try:
            raise httpx.ConnectError("https://unsaved.example/v1 unsaved-private-key")
        except httpx.ConnectError:
            logger.exception("unsaved connection failed")
    for value in (URL, KEY, "private-service.example", "unsaved.example", "unsaved-private-key"):
        assert value not in caplog.text
    assert "ConnectError" in caplog.text and "[private service]" in caplog.text


def test_broken_saved_url_never_falls_back_to_yaml_destination():
    prov.write_secret(URL_ENV, "not-a-valid-url")
    st = config.load_settings(
        db_overrides={
            "llm": {
                "backends": {
                    "other": {
                        "base_url": "https://unapproved.example/v1",
                        "external": True,
                    }
                }
            }
        }
    )
    assert st.llm.backends["other"].base_url == ""


@pytest.mark.parametrize("repaired", [True, False])
async def test_generic_reply_uses_schema_repair_and_quarantine(repaired):
    from kestrel.llm.client import LLMClient
    from kestrel.llm.guard import run_guarded
    from kestrel.llm.schemas import P1Triage
    from tests.test_guard import _chat

    prov.write_secret(URL_ENV, URL)
    prov.write_secret(KEY_ENV, KEY)
    settings = config.load_settings()
    settings.llm.tiers["tier1"].backend = "other"
    settings.llm.tiers["tier1"].model = "custom-chat"
    settings.llm.hosted_mode.enabled = True
    settings.llm.hosted_mode.acknowledged_at = "2026-10-25T00:00:00+00:00"
    settings.llm.hosted_mode.acknowledged_terms = settings.llm.hosted_mode.disclosure_terms()
    valid = '{"verdict":"keep","confidence":0.8,"reason":"plausible","signals":["level_match"]}'
    captured = {}

    async def quarantine(prompt, raw, error, model):
        captured.update(raw=raw, error=error, model=model)

    with respx.mock as router:
        chat = router.post(URL + "/chat/completions").mock(
            side_effect=[_chat("{bad"), _chat(valid if repaired else "still bad}")]
        )
        async with httpx.AsyncClient() as http:
            result = await run_guarded(
                LLMClient(settings, http=http),
                tier="tier1",
                messages=[{"role": "user", "content": "triage"}],
                model_cls=P1Triage,
                on_quarantine=quarantine,
            )
        assert chat.call_count == 2
        for call in chat.calls:
            assert call.request.headers["authorization"] == f"Bearer {KEY}"
            body = json.loads(call.request.content)
            assert "provider" not in body  # OpenRouter routing options do not reach Other.
            assert body["response_format"]["type"] == "json_schema"
    assert result.ok == repaired and result.quarantined != repaired
    if repaired:
        assert result.value.verdict == "keep" and not captured
    else:
        assert captured["raw"] == "still bad}" and captured["model"] == "custom-chat"


def test_changing_url_holds_approval_even_if_env_write_fails(monkeypatch):
    from kestrel.services.hosted_connect import _set

    with _client() as c:
        _post(c)

        async def approve():
            async with get_sessionmaker()() as s:
                st = await s.get(Setting, "llm.hosted_mode.acknowledged_at")
                assert st is None
                await _set(s, "llm.hosted_mode.acknowledged_at", "previous-approval")
                await s.commit()

        asyncio.run(approve())
        original = prov.write_secret

        def fail_url(name, value):
            if name == URL_ENV:
                raise OSError("private-service.example")
            original(name, value)

        monkeypatch.setattr(prov, "write_secret", fail_url)
        response = _post(c, base_url="https://replacement.example/v1")
        assert "connection was not saved" in response.text
        assert not _db_settings().llm.hosted_mode.acknowledged_at
        assert _db_settings().llm.backends["other"].base_url == URL


def test_models_can_manage_the_saved_other_key(monkeypatch):
    from kestrel.services import setup_state

    monkeypatch.setattr(setup_state, "probe_ollama", lambda settings: (None, []))
    with _client() as c:
        _post(c)
        with respx.mock as router:
            models = router.get(URL + "/models").respond(200, json=MODELS)
            r = c.post("/models/online/key", data={"provider": "other", "op": "test"})
            assert "connection works" in r.text and models.called
        r = c.post("/models/online/key", data={"provider": "other", "op": "remove"})
        assert "removed" in r.text.lower()
        assert not prov.secret_present(KEY_ENV)
        assert config.load_settings().llm.backends["other"].base_url == URL


def test_other_action_consent_names_the_destination_and_uses_tier_sends():
    from kestrel.services import online_consent as consent

    with _client() as c:
        _post(c)
    st = _db_settings()
    st.llm.tiers["tier3"].backend = "other"
    st.llm.tiers["tier3"].model = "custom-chat"
    st.llm.hosted_mode.enabled = True
    st.llm.hosted_mode.acknowledged_at = "2026-10-25T00:00:00+00:00"
    st.llm.hosted_mode.acknowledged_terms = st.llm.hosted_mode.disclosure_terms()
    st.confidential_mode = False
    async def route():
        async with get_sessionmaker()() as s:
            return await consent.online_route(s, st, "tier3")
    result = asyncio.run(route())
    assert result["provider"] == "private-service.example"
    sends = st.llm.hosted_mode.disclosure_terms()["sends"]["tier3"]
    assert result["sends"] == sends[:1].upper() + sends[1:]
    assert result["model"] == "custom-chat"


def test_diagnostic_settings_scrub_both_private_values():
    from kestrel.services.diagnostics import (
        make_scrubber,
        redacted_settings_yaml,
        saved_secret_values,
    )

    with _client() as c:
        _post(c)
    scrub = make_scrubber({}, saved_secret_values())
    exported = scrub(redacted_settings_yaml(_db_settings()))
    assert URL not in exported and KEY not in exported
    assert "other:" in exported
