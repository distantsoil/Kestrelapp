"""P3 dynamic schema (per-slot enums of real variant keys) and strict catalog validation."""

from __future__ import annotations

from kestrel.llm.docselect import build_doc_select_schema, validate_doc_selection

CATALOG = {
    "layouts": ["executive", "condensed"],
    "summary_variants": [{"key": "transformation"}, {"key": "operator"}],
    "skill_groups": [{"key": "identity"}, {"key": "security"}],
    "slots": [
        {"key": "r1b1", "optional": False, "variants": [{"key": "cost"}, {"key": "scale"}]},
        {"key": "r1b2", "optional": True, "variants": [{"key": "security"}, {"key": "compliance"}]},
    ],
}


def test_dynamic_schema_has_correct_enums():
    s = build_doc_select_schema(**CATALOG)
    props = s["properties"]
    assert props["layout"]["enum"] == ["executive", "condensed"]
    assert props["summary"]["enum"] == ["transformation", "operator"]
    assert props["slots"]["properties"]["r1b1"]["enum"] == ["cost", "scale"]
    # optional slot gains "hide"
    assert props["slots"]["properties"]["r1b2"]["enum"] == ["security", "compliance", "hide"]
    assert props["slots"]["required"] == ["r1b1", "r1b2"]
    assert set(props["skill_groups"]["properties"]) == {"identity", "security"}


def test_schema_refuses_an_empty_enum_and_names_the_slot():
    """A slot with zero variants would emit `enum: []`, an unsatisfiable constrained-decoding
    grammar: every P3 call would quarantine with no diagnostic. Refuse up front, naming the slot."""
    import pytest

    empty_slot = {**CATALOG, "slots": [*CATALOG["slots"],
                                       {"key": "r9b9", "optional": True, "variants": []}]}
    with pytest.raises(ValueError, match="r9b9"):
        build_doc_select_schema(**empty_slot)
    with pytest.raises(ValueError, match="summary"):
        build_doc_select_schema(**{**CATALOG, "summary_variants": []})
    with pytest.raises(ValueError, match="layout"):
        build_doc_select_schema(**{**CATALOG, "layouts": []})
    # and no enum in a schema that does build is ever empty
    s = build_doc_select_schema(**CATALOG)
    assert all(p["enum"] for p in s["properties"]["slots"]["properties"].values())


def _selection(slots: dict) -> dict:
    return {
        "layout": "executive",
        "summary": "transformation",
        "slots": slots,
        "skill_groups": {"identity": True},
        "rationale": {},
        "coherence_check": "ok",
    }


def test_validation_rejects_a_hallucinated_variant_key():
    errors = validate_doc_selection(
        _selection({"r1b1": "invented", "r1b2": "hide"}),
        slots=CATALOG["slots"],
        layouts=CATALOG["layouts"],
        summary_variants=CATALOG["summary_variants"],
    )
    assert any("unknown variant key" in e for e in errors)


def test_validation_accepts_hide_for_optional_slot():
    errors = validate_doc_selection(
        _selection({"r1b1": "cost", "r1b2": "hide"}),
        slots=CATALOG["slots"],
        layouts=CATALOG["layouts"],
        summary_variants=CATALOG["summary_variants"],
    )
    assert errors == []


def test_validation_flags_missing_required_slot():
    errors = validate_doc_selection(
        _selection({"r1b2": "hide"}),  # r1b1 (required) missing
        slots=CATALOG["slots"],
        layouts=CATALOG["layouts"],
        summary_variants=CATALOG["summary_variants"],
    )
    assert any("missing required slot" in e for e in errors)


def test_validation_rejects_unknown_layout():
    errors = validate_doc_selection(
        {**_selection({"r1b1": "cost"}), "layout": "nope"},
        slots=CATALOG["slots"],
        layouts=CATALOG["layouts"],
        summary_variants=CATALOG["summary_variants"],
    )
    assert any("unknown layout" in e for e in errors)


async def test_select_config_falls_back_to_defaults_on_an_empty_slot(monkeypatch):
    """A zero-variant slot makes the P3 schema unsatisfiable. select_config must not raise: it
    renders defaults, flags defaults_used, and names the slot (hard rule 3)."""
    from types import SimpleNamespace

    from kestrel.docs import select as select_mod

    async def _boom(*a, **k):
        raise ValueError("slot 'roles.0.bullets' has no variants")

    monkeypatch.setattr(select_mod, "run_doc_select", _boom)
    monkeypatch.setattr(select_mod, "schema_inputs", lambda c: {
        "layouts": [], "summary_variants": [], "slots": {}, "skill_groups": [],
    })
    monkeypatch.setattr(select_mod, "default_config", lambda c: {"layout": "x"})
    monkeypatch.setattr(select_mod, "_render_user", lambda *a, **k: "u")
    monkeypatch.setattr(
        select_mod, "load_prompt", lambda v: SimpleNamespace(system="s", user="u")
    )

    class _S:
        async def get(self, *a, **k):
            return None

    sel = await select_mod.select_config(_S(), None, catalog=None)
    assert sel.defaults_used and "no variants" in (sel.error or "")
    assert sel.config == {"layout": "x"}
