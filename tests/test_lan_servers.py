"""D-LLM-15: add a model server on the person's own network from the Models page, give a step a
model on it, and see where each step runs. Cloud-safe: TestClient on the real app, a throwaway
data directory, the model list answered by a fake transport, no Ollama and no network.

Invariants: an internet address is never saved as a network server; a key goes to the private key
store and never into the settings table or a page; only this computer can add, change or remove a
server; a step on a non-Ollama server shows no download or build button."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from kestrel import netloc
from kestrel.db.engine import get_sessionmaker
from kestrel.db.models import Setting
from kestrel.llm.profiles import Hardware
from kestrel.services import lan_servers as lan
from kestrel.services import setup_state as ss
from kestrel.services.online_consent import online_route, tier_goes_online
from tests.test_hosted_mode import _db_settings, _fresh_app

GPU = Hardware(os="windows", gpu_vendor="nvidia", vram_gb=12.0, ram_gb=32.0)
ADDR = "http://192.168.2.25:8000"
SECRET = "sk-lan-secret-value-123456"


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setattr(ss, "detected_hardware", lambda: GPU)
    monkeypatch.setattr(ss, "probe_ollama", lambda settings, fresh=False: ("0.5.0", []))
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization")})
        return httpx.Response(200, json={"data": [{"id": "qwen3-14b"}, {"id": "mini-8b"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        lan.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler),
                          **{k: v for k, v in kw.items() if k == "timeout"}))
    return seen


def _rows() -> dict:
    async def go():
        from sqlalchemy import select

        async with get_sessionmaker()() as s:
            return {k: v for k, v in (await s.execute(
                select(Setting.key, Setting.value))).all()}

    return asyncio.run(go())


def test_add_a_server_test_it_and_give_steps_a_model(_world):
    client = _fresh_app()
    r = client.post("/models/network/test", data={"address": "192.168.2.25:8000",
                                                  "kind": "omlx", "api_key": SECRET})
    assert r.status_code == 200 and "qwen3-14b" in r.text and SECRET not in r.text
    assert _world[-1]["url"] == f"{ADDR}/v1/models" and _world[-1]["auth"] == f"Bearer {SECRET}"
    assert not [k for k in _rows() if k.startswith("llm.backends.")]  # a test saves nothing

    r = client.post("/models/network/save", data={"address": ADDR, "kind": "omlx",
                                                  "api_key": SECRET})
    assert "Saved 192.168.2.25" in r.text and SECRET not in r.text
    name = lan.backend_name(f"{ADDR}/v1")
    rows = _rows()
    assert SECRET not in json.dumps(rows, default=str)  # the key is in the key store, not a row
    b = rows[f"llm.backends.{name}"]
    assert (b["json_mode"], b["thinking_control"], b["server_kind"]) == (
        "json_object", "chat_template_kwargs", "omlx")

    for tier in ("tier1", "tier2"):
        r = client.post("/models/network/assign",
                        data={"server": name, "tier": tier, "model": "qwen3-14b"})
        assert "now uses qwen3-14b on 192.168.2.25" in r.text
    s = _db_settings()
    backend = s.llm.backends[name]
    assert backend.location == netloc.LOCAL_NETWORK and not backend.external
    assert (s.llm.tiers["tier1"].backend, s.llm.tiers["tier2"].model) == (name, "qwen3-14b")
    assert not s.llm.hosted_mode.active
    assert tier_goes_online(s, "tier1") is False and tier_goes_online(s, "tier2") is False


def test_the_page_says_where_each_step_runs_and_hides_ollama_only_buttons():
    client = _fresh_app()
    client.post("/models/network/save", data={"address": ADDR, "kind": "vllm"})
    name = lan.backend_name(f"{ADDR}/v1")
    for tier in ("tier1", "tier2"):
        client.post("/models/network/assign", data={"server": name, "tier": tier,
                                                    "model": "mini-8b"})
    page = client.get("/models").text
    assert "On another computer on your network (192.168.2.25)" in page
    assert "Load models in vLLM on that computer" in page
    assert 'data-testid="mo-dl-start"' not in page and 'data-testid="mo-dl-tune"' not in page
    assert "mo-network-card" in page


def test_an_ollama_server_keeps_the_download_buttons_note_off():
    client = _fresh_app()
    client.post("/models/network/save", data={"address": ADDR, "kind": "ollama"})
    assert "Load models in" not in client.get("/models/downloads").text


def test_an_internet_address_is_refused_and_nothing_is_saved():
    client = _fresh_app()
    r = client.post("/models/network/save", data={"address": "http://203.0.113.9:8000",
                                                  "kind": "ollama"})
    assert "on the internet" in r.text and "Online model" in r.text
    assert not [k for k in _rows() if k.startswith("llm.backends.")]
    r = client.post("/models/network/test", data={"address": "https://models.example.invalid"})
    assert "on the internet" in r.text


def test_a_bad_address_is_a_sentence_not_an_error():
    client = _fresh_app()
    for bad in ("", "ftp://192.168.1.5", "http://user:pw@192.168.1.5", "http://192.168.1.5:99999"):
        r = client.post("/models/network/save", data={"address": bad, "kind": "ollama"})
        assert r.status_code == 200 and "mo-network-message" in r.text, bad


def test_labels_name_the_computer_on_the_network():
    from kestrel.analytics.progress import tier2_label
    from kestrel.services import run_notice

    client = _fresh_app()
    client.post("/models/network/save", data={"address": ADDR, "kind": "omlx"})
    name = lan.backend_name(f"{ADDR}/v1")
    for tier in ("tier1", "tier2"):
        client.post("/models/network/assign", data={"server": name, "tier": tier,
                                                    "model": "mini-8b"})
    s = _db_settings()
    assert tier2_label(s) == "mini-8b, on another computer on your network (192.168.2.25)"
    assert "on another computer on your network (192.168.2.25)" in run_notice.describe_run(s)
    route = asyncio.run(_route(s))
    assert route == {"online": False, "where": "on another computer on your network (192.168.2.25)"}
    from kestrel.web.routes_control import _model_row, readout

    row = asyncio.run(_model_row(s, "local"))
    out = readout({"model": row, "worker": {}, "scan": {}, "settings_mode": "local"})
    assert out["engine"]["text"] == "On another computer on your network (192.168.2.25)"


async def _route(settings):
    async with get_sessionmaker()() as session:
        return await online_route(session, settings, "tier2")


def test_removing_a_server_returns_its_steps_to_the_default():
    client = _fresh_app()
    client.post("/models/network/save", data={"address": ADDR, "kind": "ollama"})
    name = lan.backend_name(f"{ADDR}/v1")
    client.post("/models/network/assign", data={"server": name, "tier": "tier1", "model": "m"})
    client.post("/models/network/remove", data={"server": name})
    rows = _rows()
    assert not [k for k in rows if k.startswith("llm.backends.lan_")]
    assert "llm.tiers.tier1.backend" not in rows


def test_only_a_server_added_here_can_be_removed():
    client = _fresh_app()
    r = client.post("/models/network/remove", data={"server": "local"})
    assert "can only remove a server added here" in r.text


def test_presets_pin_what_each_server_type_sends():
    p = lan.backend_row("omlx", f"{ADDR}/v1", "lan_x", with_key=False)
    assert (p["json_mode"], p["thinking_control"], p["requires_num_ctx_modelfile"]) == (
        "json_object", "chat_template_kwargs", False)
    o = lan.backend_row("ollama", f"{ADDR}/v1", "lan_x", with_key=False)
    assert (o["json_mode"], o["thinking_control"], o["requires_num_ctx_modelfile"]) == (
        "json_schema", "reasoning_effort", True)


def test_a_host_name_is_refused_for_a_network_server(_world):
    client = _fresh_app()
    for addr in ("http://studio.local:8000", "http://nas:8000"):
        r = client.post("/models/network/save", data={"address": addr, "kind": "ollama"})
        assert "IP address" in r.text
    assert not [k for k in _rows() if k.startswith("llm.backends.")]


def test_a_saved_key_goes_only_to_that_servers_saved_address(_world):
    client = _fresh_app()
    client.post("/models/network/save", data={"address": ADDR, "kind": "omlx", "api_key": SECRET})
    name = lan.backend_name(f"{ADDR}/v1")
    _world.clear()
    client.post("/models/network/test", data={"server": name, "address": "10.9.9.9:8000"})
    assert _world and _world[-1]["auth"] is None  # a new address gets no saved key
    client.post("/models/network/test", data={"server": name})
    assert _world[-1]["auth"] == f"Bearer {SECRET}"


def _put_row(key: str, value) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            s.add(Setting(key=key, value=value))
            await s.commit()

    asyncio.run(go())


def test_choose_models_works_for_a_server_settings_yml_defines(_world, monkeypatch):
    """A server named in settings.yml (not added on this page) lists its models from its own
    Choose models button, with its own saved key, and the pickers work (fable review)."""
    from kestrel.llm import providers as prov

    client = _fresh_app()
    _put_row("llm.backends.mini", {
        "base_url": f"{ADDR}/v1", "engine": "http", "api_key_env": "OMLX_API_KEY",
        "server_kind": "omlx", "external": False, "provider": "generic",
    })
    monkeypatch.setattr(prov, "secret_value",
                        lambda name: SECRET if name == "OMLX_API_KEY" else "")
    assert "mini" in {sv.name for sv in lan.list_servers(_db_settings())}
    _world.clear()
    r = client.post("/models/network/test", data={"server": "mini", "kind": "omlx"})
    assert _world and _world[-1]["url"] == f"{ADDR}/v1/models"
    assert _world[-1]["auth"] == f"Bearer {SECRET}" and SECRET not in r.text
    assert 'data-testid="mo-nw-use-tier2"' in r.text and 'value="mini"' in r.text
    r = client.post("/models/network/assign",
                    data={"server": "mini", "tier": "tier2", "model": "qwen3-14b"})
    assert "Pick a server and a model first." not in r.text
    assert _db_settings().llm.tiers["tier2"].backend == "mini"


def test_an_unsaved_test_lists_models_but_offers_no_picker(_world):
    """Test, Save, then pickers (D-LLM-15): a picker for a server that is not saved yet could
    only answer 'Pick a server and a model first.'"""
    client = _fresh_app()
    r = client.post("/models/network/test", data={"address": "192.168.2.25:8000",
                                                  "kind": "omlx"})
    assert "qwen3-14b" in r.text
    assert 'data-testid="mo-nw-use-tier2"' not in r.text
    assert 'data-testid="mo-nw-save-first"' in r.text
