"""Every prompt loads by version, GLOBAL_RULES substituted, metadata + model class resolve."""

from __future__ import annotations

from kestrel.llm.prompts import all_versions, global_rules, load_prompt
from kestrel.llm.schemas import SCHEMA_BY_VERSION


def test_twenty_two_prompts_registered():
    # P7 became three prompts (outline, role map, per-slot wordings) in D-IMPORT-3; P13, the
    # resume assessment, was added in D-DOC-2; P14 (interview questions) in D-INTERVIEW-1 and
    # P15 (suggested titles) in D-TTL-3 and P16 (company summary) in D-LLM-9.
    assert len(all_versions()) == 22


def test_every_prompt_loads_and_substitutes_global_rules():
    rules = global_rules()
    for version in all_versions():
        p = load_prompt(version)
        assert p.system, f"{version}: empty system"
        assert "{GLOBAL_RULES}" not in p.system, f"{version}: GLOBAL_RULES not substituted"
        assert rules in p.system, f"{version}: global rules missing"
        assert p.tier, f"{version}: no tier"
        assert p.temperature is not None and p.max_tokens, f"{version}: missing temp/max_tokens"
        assert p.model_cls is SCHEMA_BY_VERSION[version]


def test_specific_prompt_metadata():
    p = load_prompt("evaluate/v1")
    assert p.id == "P2"
    assert p.tier == "tier2"
    assert p.temperature == 0.2
    assert p.max_tokens == 4000
    assert "executive recruiter" in p.system
    # context-block placeholders are preserved for the caller to fill at render time
    assert "{PROFILE_FULL}" in p.user
