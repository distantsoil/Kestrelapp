"""Settings load and the SPEC.md 4.6 precedence chain (db-override > env > YAML > default)."""

from __future__ import annotations

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
    assert s.scheduler.enabled is False
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
