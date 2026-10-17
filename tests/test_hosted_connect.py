"""Settings > Hosted one-form connect (D-UI-7) and the packaged settings.yml layer under a stale
seeded copy (D-CONFIG-LAYER). Every provider call is mocked with respx."""

from __future__ import annotations

import respx
from httpx import Response

from kestrel import paths
from kestrel.config import load_settings
from kestrel.llm import providers as prov
from tests.test_hosted_mode import _db_settings, _fresh_app

_OR = "https://openrouter.ai/api/v1"
_MODELS = {
    "data": [
        {
            "id": "a/cheap",
            "context_length": 32000,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
            "supported_parameters": ["structured_outputs"],
        },
        {
            "id": "b/strong",
            "context_length": 128000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["structured_outputs"],
        },
    ]
}


def _mock_openrouter(router, key_status: int = 200) -> None:
    router.get(f"{_OR}/key").mock(
        return_value=Response(key_status, json={"data": {"label": "k", "is_free_tier": False}})
    )
    router.get(f"{_OR}/models").mock(return_value=Response(200, json=_MODELS))


def test_stale_seeded_settings_still_lists_every_shipped_backend():
    # A settings.yml seeded before the hosted backends shipped: only the local backend.
    seeded = paths.config_dir() / "settings.yml"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(
        "llm:\n  backends:\n    local:\n      base_url: http://10.0.0.5:11434/v1\n"
        "      api_key: ollama\n"
        "  tiers:\n    tier1: { backend: local, model: m1 }\n",
        encoding="utf-8",
    )
    s = load_settings()
    assert set(prov.external_backends(s)) == {"frontier", "openmodel", "anthropic", "openai"}
    assert s.llm.backends["local"].base_url == "http://10.0.0.5:11434/v1"  # seeded copy wins


def test_packaged_layer_adds_only_missing_backends_never_tiers_or_fields():
    seeded = paths.config_dir() / "settings.yml"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text(
        "llm:\n  backends:\n    local:\n      base_url: http://127.0.0.1:11434/v1\n"
        "      api_key: ollama\n"
        "    frontier:\n      base_url: https://example.test/v1\n      external: true\n"
        "  tiers:\n    tier1: { backend: local, model: m1 }\n"
        "    tier2: { backend: local, model: m2 }\n",
        encoding="utf-8",
    )
    s = load_settings()
    assert s.llm.backends["frontier"].base_url == "https://example.test/v1"
    assert s.llm.backends["frontier"].api_key_env != "OPENROUTER_API_KEY"  # no field fill-in
    assert "anthropic" in s.llm.backends  # a whole missing backend is added
    assert "frontier" not in s.llm.tiers  # a removed tier stays removed


def test_panel_leads_with_the_connect_form(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    body = _fresh_app().get("/settings/hosted").text
    assert "Connect a hosted provider" in body
    assert body.index("hosted-connect") < body.index("hosted-assignments")
    for label in ("OpenRouter", "Anthropic (Claude API)", "OpenAI (API)"):
        assert label in body
    assert "https://platform.openai.com/api-keys" in body
    assert 'value="openrouter" checked' in body
    assert "—" not in body


def test_ack_box_is_never_pre_ticked(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    with respx.mock as router:
        _mock_openrouter(router)
        body = client.post(
            "/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"}
        ).text
    assert "Change hosted provider" in body
    assert 'name="ack" value="on" checked' not in body


def test_bad_pasted_key_never_replaces_the_saved_one(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-good")
    client = _fresh_app()
    with respx.mock(assert_all_called=False) as router:
        _mock_openrouter(router, key_status=401)
        r = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "api_key": "sk-or-typo", "ack": "on"},
        )
    assert "rejected the key" in r.text and "saved key was not changed" in r.text
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-good"


def test_unticked_switch_while_hosted_says_hosted(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    with respx.mock as router:
        _mock_openrouter(router)
        client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "switch": "on"},
        )
        r = client.post("/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"})
    assert "now runs on hosted models" in r.text and "Local models stay active" not in r.text


def test_connect_without_ack_saves_nothing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = _fresh_app()
    with respx.mock(assert_all_called=False) as router:
        _mock_openrouter(router)
        r = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "api_key": "sk-or-abc", "switch": "on"},
        )
        assert not router.calls  # no network before the acknowledgement
    assert "nothing was saved" in r.text and "I understand" in r.text
    assert not prov.secret_present("OPENROUTER_API_KEY")
    st = _db_settings()
    assert not st.llm.hosted_mode.acknowledged_at and st.llm.mode == "local"


def test_connect_with_no_key_anywhere_is_refused(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = _fresh_app().post("/settings/hosted/connect", data={"provider": "openai", "ack": "on"})
    assert "Paste your OpenAI (API) API key" in r.text
    assert 'value="openai" checked' in r.text  # the choice survives the refusal


def test_connect_rejected_key_writes_no_rows(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bad")
    client = _fresh_app()
    with respx.mock(assert_all_called=False) as router:
        _mock_openrouter(router, key_status=401)
        r = client.post("/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"})
    assert "rejected the key" in r.text
    st = _db_settings()
    assert not st.llm.hosted_mode.acknowledged_at and "tier1" not in st.llm.hosted_tiers


def test_connect_picks_models_acknowledges_and_switches(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    with respx.mock as router:
        _mock_openrouter(router)
        r = client.post(
            "/settings/hosted/connect",
            data={"provider": "openrouter", "ack": "on", "switch": "on"},
        )
    assert "Connected to OpenRouter" in r.text and "Hosted models (active)" in r.text
    st = _db_settings()
    assert st.llm.hosted_mode.active and st.llm.mode == "hosted"
    assert st.llm.tiers["tier1"].model == "a/cheap"
    assert st.llm.tiers["tier2"].model == "b/strong"
    assert st.llm.tiers["frontier"].backend == "frontier"
    assert st.llm.tiers["embed"].backend == "local"


def test_connect_without_switch_keeps_local_active(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    with respx.mock as router:
        _mock_openrouter(router)
        r = client.post("/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"})
    assert "Local models stay active" in r.text
    st = _db_settings()
    assert st.llm.hosted_mode.active and st.llm.mode == "local"
    assert st.llm.hosted_tiers["tier2"].model == "b/strong"


# ------------------------------------------------------------- D-BYOK-11: keys from .env only
def _write_env(text: str) -> None:
    paths.dotenv_path().parent.mkdir(parents=True, exist_ok=True)
    paths.dotenv_path().write_text(text, encoding="utf-8")


def test_system_key_is_ignored_and_the_kestrel_key_is_used(monkeypatch):
    import os

    from kestrel.config import get_secret

    monkeypatch.delenv("KESTREL_USE_SYSTEM_KEYS")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-used-by-other-tools")
    assert get_secret("OPENAI_API_KEY") is None  # nothing saved in Kestrel: no key at all
    _write_env("OPENAI_API_KEY=sk-kestrel-only\n")
    assert get_secret("OPENAI_API_KEY") == "sk-kestrel-only"
    assert os.environ["OPENAI_API_KEY"] == "sk-used-by-other-tools"  # system value untouched


def test_kestrel_keys_never_enter_the_process_environment(monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.delenv("KESTREL_USE_SYSTEM_KEYS")
    _write_env("ANTHROPIC_API_KEY=sk-ant-kestrel\nKESTREL_LOG_LEVEL=DEBUG\n")
    prov.write_secret("OPENROUTER_API_KEY", "sk-or-kestrel")
    assert prov.secret_present("ANTHROPIC_API_KEY") and prov.secret_present("OPENROUTER_API_KEY")
    assert "ANTHROPIC_API_KEY" not in os.environ and "OPENROUTER_API_KEY" not in os.environ
    assert os.environ.get("KESTREL_LOG_LEVEL") == "DEBUG"  # non-secret config still applies
    child = subprocess.run(
        [sys.executable, "-c", "import os; print('OPENROUTER_API_KEY' in os.environ)"],
        capture_output=True, text=True, check=True,
    )
    assert child.stdout.strip() == "False"  # a program Kestrel launches inherits nothing


def test_page_says_a_system_key_is_ignored(monkeypatch):
    monkeypatch.delenv("KESTREL_USE_SYSTEM_KEYS")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-other-tool")
    body = _fresh_app().get("/settings/hosted").text
    assert "Your system has its own key for this provider; Kestrel ignores it" in body
    # The variable names are under Show details only (release round R1).
    details = body.split('data-testid="key-details"')[1]
    assert "Present in your system environment and ignored" in details
    assert "ANTHROPIC_API_KEY" in details
    assert "Kestrel ignores these variables in your system environment" in details


def test_opt_out_uses_a_system_key_but_the_kestrel_key_wins(monkeypatch):
    from kestrel.config import get_secret

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-shell")
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-shell"
    _write_env("OPENROUTER_API_KEY=sk-or-kestrel\n")
    assert get_secret("OPENROUTER_API_KEY") == "sk-or-kestrel"
    assert "KESTREL_USE_SYSTEM_KEYS is set" in _fresh_app().get("/settings/hosted").text


def test_saving_a_key_writes_only_that_name_to_kestrels_file(monkeypatch):
    import os

    monkeypatch.setenv("OPENAI_API_KEY", "sk-other-tools")
    _write_env("ANTHROPIC_API_KEY=sk-ant-keep\n")
    prov.write_secret("OPENAI_API_KEY", "sk-new")
    text = paths.dotenv_path().read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-new" in text and "ANTHROPIC_API_KEY=sk-ant-keep" in text
    assert os.environ["OPENAI_API_KEY"] == "sk-other-tools"


def test_custom_key_names_are_private_too(monkeypatch):
    import os

    from kestrel.config import get_secret, is_secret_name

    monkeypatch.delenv("KESTREL_USE_SYSTEM_KEYS")
    _write_env("GROQ_TOKEN=gsk-1\nKESTREL_LOG_LEVEL=DEBUG\n")
    assert get_secret("GROQ_TOKEN") == "gsk-1" and "GROQ_TOKEN" not in os.environ
    assert not is_secret_name("KESTREL_SERVER__SHARED_KEY")  # config, not a credential
    prov.write_secret("GROQ_TOKEN", "gsk-2")
    assert get_secret("GROQ_TOKEN") == "gsk-2"


def test_default_readers_use_the_kestrel_key_not_the_system_one(monkeypatch):
    from kestrel.llm.client import _resolve_api_key
    from kestrel.pipeline.tier3 import _has_key

    monkeypatch.delenv("KESTREL_USE_SYSTEM_KEYS")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-system")
    backend = load_settings().llm.backends["frontier"]
    assert _resolve_api_key(backend) == "" and not _has_key("OPENROUTER_API_KEY")
    _write_env("OPENROUTER_API_KEY=sk-or-kestrel\n")
    assert _resolve_api_key(backend) == "sk-or-kestrel" and _has_key("OPENROUTER_API_KEY")


def test_inline_settings_key_is_labeled_as_such(monkeypatch):
    seeded = paths.config_dir() / "settings.yml"
    seeded.parent.mkdir(parents=True, exist_ok=True)
    shipped = (paths.packaged_config() / "settings.yml").read_text(encoding="utf-8")
    seeded.write_text(
        shipped.replace(
            "      api_key_env: OPENAI_API_KEY\n",
            "      api_key_env: OPENAI_API_KEY\n      api_key: sk-inline\n",
        ),
        encoding="utf-8",
    )
    body = _fresh_app().get("/settings/hosted").text
    assert "key written in settings.yml" in body


def test_settings_connect_shows_the_same_clear_result(monkeypatch):
    """D-UI-40: Settings > Hosted shows the headline and the reason, themed, and scrolls to it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ok")
    client = _fresh_app()
    assert "show:#hosted-panel:top" in client.get("/settings/hosted").text
    with respx.mock as router:
        _mock_openrouter(router)
        r = client.post("/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"})
    assert 'data-testid="hosted-connect-result"' in r.text and 'data-connect-ok="true"' in r.text
    assert "Tier 1 will use a/cheap and Tier 2 will use b/strong." in r.text
    assert 'class="okbox"' in r.text  # the theme's success style, never a fixed color
    with respx.mock(assert_all_called=False) as router:
        _mock_openrouter(router, key_status=401)
        r = client.post("/settings/hosted/connect", data={"provider": "openrouter", "ack": "on"})
    assert 'data-connect-ok="false"' in r.text and "Not connected to OpenRouter." in r.text
