"""Concurrency boundaries, persistence, and the Models form on isolated test data."""

from __future__ import annotations

import asyncio
import json

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from kestrel.config import load_settings
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.llm.profiles import Hardware
from kestrel.services import setup_state as ss
from kestrel.services.setting_overrides import load_settings_with_db
from tests.test_hosted_mode import _db_settings, _fresh_app


@pytest.mark.parametrize("tier", ["tier1", "tier2", "embed"])
@pytest.mark.parametrize("value", [0, -1, 17, 1.5, True, "1.5", "true"])
def test_invalid_concurrency_is_rejected_at_load(tier, value):
    with pytest.raises(ValidationError, match=tier):
        load_settings(db_overrides={"concurrency": {tier: value}})


@pytest.mark.parametrize("value", [1, 16])
def test_bounds_load_for_every_tier(value):
    settings = load_settings(db_overrides={
        "concurrency": dict.fromkeys(("tier1", "tier2", "embed"), value),
    })
    assert set(settings.concurrency.model_dump().values()) == {value}


def test_environment_concurrency_is_validated(monkeypatch):
    monkeypatch.setenv("KESTREL_CONCURRENCY", json.dumps({"tier1": 17}))
    with pytest.raises(ValidationError, match="tier1"):
        load_settings()


def test_yaml_concurrency_is_validated(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.yml"
    settings_file.write_text("concurrency:\n  tier2: 0\n", encoding="utf-8")
    monkeypatch.setattr("kestrel.config.paths.config_file", lambda _: settings_file)
    with pytest.raises(ValidationError, match="tier2"):
        load_settings()


def test_database_concurrency_is_validated():
    _fresh_app()

    async def load_invalid():
        async with get_sessionmaker()() as session:
            session.add(Setting(key="concurrency.tier2", value=0))
            await session.commit()
            await load_settings_with_db(session)

    with pytest.raises(ValidationError, match="tier2"):
        asyncio.run(load_invalid())


@pytest.fixture
def models_client(monkeypatch):
    monkeypatch.setattr(ss, "detected_hardware", lambda: Hardware(
        os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0,
    ))
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))
    return _fresh_app()


@pytest.mark.parametrize("tier1,tier2", [("1", "16"), ("16", "1")])
def test_models_saves_both_bounds_and_refresh_keeps_values(models_client, tier1, tier2):
    response = models_client.post("/models/speed", data={"tier1": tier1, "tier2": tier2})
    assert response.status_code == 200
    assert "Saved. It applies on the next scoring pass." in response.text
    settings = _db_settings()
    assert (settings.concurrency.tier1, settings.concurrency.tier2) == (int(tier1), int(tier2))
    for url in ("/models", "/models/downloads"):
        body = BeautifulSoup(models_client.get(url).text, "html.parser")
        assert body.select_one('[name="tier1"]')["value"] == tier1
        assert body.select_one('[name="tier2"]')["value"] == tier2
        assert "at least 16" in body.select_one('[data-testid="mo-speed-parallel"]').get_text()


@pytest.mark.parametrize("tier1,tier2", [
    ("8", "0"), ("17", "3"), ("1.5", "3"), ("2", "abc"), ("", "3"),
    ("2", ""), ("2", "9" * 5000),
])
def test_invalid_form_changes_neither_setting(models_client, tier1, tier2):
    models_client.post("/models/speed", data={"tier1": "6", "tier2": "3"})
    response = models_client.post("/models/speed", data={"tier1": tier1, "tier2": tier2})
    assert response.status_code == 200
    assert "Neither setting was changed." in response.text
    settings = _db_settings()
    assert (settings.concurrency.tier1, settings.concurrency.tier2) == (6, 3)


def test_form_explains_memory_tier_draining_and_ollama_environment(models_client):
    models_client.post("/models/speed", data={"tier1": "1", "tier2": "1"})
    body = BeautifulSoup(models_client.get("/models/downloads").text, "html.parser")
    form = body.select_one("#mo-speed")
    assert form.find_parent("details") is not None
    assert form["hx-target"] == "#mo-speed"
    for tier in ("tier1", "tier2"):
        field = form.select_one(f'[name="{tier}"]')
        assert (field["min"], field["max"], field["step"]) == ("1", "16", "1")
        assert field.has_attr("required")
    text = form.get_text(" ", strip=True)
    for phrase in ("more memory", "before starting the next", "share a model",
                   "OLLAMA_NUM_PARALLEL", "at least 4", "restart Ollama",
                   "separate Ollama process", "OLLAMA_KEEP_ALIVE=2h"):
        assert phrase in text


def test_online_mode_still_offers_concurrency(models_client):
    response = models_client.post("/setup/mode", data={"mode": "hosted"})
    assert response.status_code == 200
    body = models_client.get("/models/downloads").text
    assert "nothing needs to be downloaded" in body
    assert 'id="mo-speed"' in body
    assert "Online providers have their own request limits." in body


@pytest.mark.parametrize("value", ["0", "17", "1.5"])
def test_legacy_settings_route_cannot_persist_invalid_concurrency(models_client, value):
    response = models_client.post("/settings", data={"concurrency.tier1": value})
    assert response.status_code == 200
    assert _db_settings().concurrency.tier1 == 4
