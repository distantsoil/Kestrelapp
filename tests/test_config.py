"""Settings load and the SPEC.md 4.6 precedence chain (db-override > env > YAML > default)."""

from __future__ import annotations

import pytest

from kestrel.config import get_settings, load_settings


def test_defaults_load_from_packaged_yaml():
    s = get_settings()
    assert s.llm.tiers["tier2"].model == "kestrel-tier2"
    assert s.llm.tiers["tier3"].model == "kestrel-tier3"  # the decision log (still LOCAL)
    # Tier 3 ESCALATION (Wave 1C / SPEC 6.4) is a SEPARATE `frontier` role on the ONLY external
    # backend, so nothing local (cover letters, tier3) is ever routed off-machine.
    assert s.llm.tiers["frontier"].backend == "frontier"
    assert s.llm.backends["frontier"].external is True
    assert s.llm.backends["frontier"].base_url == "https://openrouter.ai/api/v1"
    assert s.llm.backends["local"].external is False
    assert s.llm.backends["local"].requires_num_ctx_modelfile is True
    assert s.thresholds.apply == 4.0
    assert s.max_posting_age_days == 45
    assert s.mcp.port == 9110


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("KESTREL_MAX_POSTING_AGE_DAYS", "10")
    s = load_settings()
    assert s.max_posting_age_days == 10


def test_db_override_beats_env(monkeypatch):
    monkeypatch.setenv("KESTREL_MAX_POSTING_AGE_DAYS", "10")
    s = load_settings(db_overrides={"max_posting_age_days": 7})
    assert s.max_posting_age_days == 7


def test_reread_reflects_change(monkeypatch):
    assert get_settings().max_posting_age_days == 45
    monkeypatch.setenv("KESTREL_MAX_POSTING_AGE_DAYS", "99")
    assert get_settings().max_posting_age_days == 99  # no restart needed


def test_old_settings_yaml_with_scheduler_enabled_false_loads_without_error(monkeypatch, tmp_path):
    """An old settings.yml that still contains `scheduler: enabled: false` loads without error.
    The scheduler config was removed; extra keys in YAML are ignored by pydantic extra="ignore"."""
    old_yaml = tmp_path / "settings.yml"
    old_yaml.write_text("""
scheduler:
  enabled: false
llm:
  backends:
    local:
      base_url: "http://127.0.0.1:11434/v1"
      api_key: "ollama"
      json_mode: json_schema
      thinking_control: reasoning_effort
      requires_num_ctx_modelfile: true
  tiers:
    embed:   { backend: local, model: "nomic-embed-text" }
    tier1:   { backend: local, model: "kestrel-tier1", temperature: 0.0, max_tokens: 300 }
    tier2:   { backend: local, model: "kestrel-tier2", temperature: 0.2, max_tokens: 4000 }
    tier3:   { backend: local, model: "kestrel-tier3", temperature: 0.3, max_tokens: 6000 }
    docsel:  { backend: local, model: "kestrel-docsel", temperature: 0.0, max_tokens: 1500 }
""", encoding="utf-8")
    monkeypatch.setattr("kestrel.config.paths.config_file", lambda _: old_yaml)
    # Should not raise
    s = load_settings()
    assert s.llm.tiers["tier2"].model == "kestrel-tier2"


def test_nested_env_concurrency_accepts_whole_number_text(monkeypatch):
    monkeypatch.setenv("KESTREL_CONCURRENCY__TIER1", "4")
    monkeypatch.setenv("KESTREL_CONCURRENCY__TIER2", " 3 ")
    monkeypatch.setenv("KESTREL_CONCURRENCY__EMBED", "16")
    cfg = load_settings().concurrency
    assert (cfg.tier1, cfg.tier2, cfg.embed) == (4, 3, 16)


@pytest.mark.parametrize("bad", ["4.5", "4.0", "true", "0", "17", "-1", "", "four"])
def test_nested_env_concurrency_still_rejects_other_values(monkeypatch, bad):
    monkeypatch.setenv("KESTREL_CONCURRENCY__TIER1", bad)
    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.parametrize("bad", [True, 4.0, 4.5, 0, 17])
def test_concurrency_model_still_rejects_non_integers_and_out_of_range(bad):
    from kestrel.config import Concurrency

    with pytest.raises(ValueError):
        Concurrency(tier1=bad)
