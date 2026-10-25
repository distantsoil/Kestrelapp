"""P3 document-slot selection: the dynamic per-doc_master schema (SPEC-MODULES.md 1.3) and strict
catalog validation. The schema gives every slot an EXPLICIT enum of that slot's real variant keys
(plus "hide" where optional), so a hallucinated variant key is structurally impossible rather than
merely detectable. Validation is the second line of defense for json_object-mode backends that have
no grammar: a bad key is a validation FAILURE surfaced to the UI, never a silent default (rule 2).
"""

from __future__ import annotations

from typing import Any


def build_doc_select_schema(
    *,
    layouts: list[Any],
    summary_variants: list[dict],
    slots: list[dict],
    skill_groups: list[dict],
) -> dict:
    """slots: [{"key","optional","variants":[{"key"}...]}]; layouts: [key or {"key"}]."""

    def _key(x: Any) -> str:
        return x["key"] if isinstance(x, dict) else x

    # An empty enum is an UNSATISFIABLE constrained-decoding grammar: every call would quarantine
    # with no diagnostic pointing at the catalog. Refuse to build it and name the gap instead.
    empty_slots = [slot["key"] for slot in slots if not slot.get("variants")]
    if empty_slots:
        raise ValueError(
            f"cannot build the doc-select schema: slot(s) {empty_slots} have no variants, which "
            "would produce an empty enum (unsatisfiable grammar). Add a variant or remove the slot."
        )
    if not summary_variants:
        raise ValueError(
            "cannot build the doc-select schema: the document master has no summary variants "
            "(empty enum, unsatisfiable grammar)."
        )
    if not layouts:
        raise ValueError(
            "cannot build the doc-select schema: no layouts are available (empty enum, "
            "unsatisfiable grammar)."
        )

    slot_props: dict[str, dict] = {}
    for slot in slots:
        enum = [v["key"] for v in slot.get("variants", [])]
        if slot.get("optional"):
            enum = [*enum, "hide"]
        slot_props[slot["key"]] = {"type": "string", "enum": enum}

    return {
        "type": "object",
        "required": ["layout", "summary", "slots", "skill_groups", "rationale", "coherence_check"],
        "properties": {
            "layout": {"type": "string", "enum": [_key(x) for x in layouts]},
            "summary": {"type": "string", "enum": [_key(x) for x in summary_variants]},
            "slots": {
                "type": "object",
                "required": [s["key"] for s in slots],
                "properties": slot_props,
            },
            "skill_groups": {
                "type": "object",
                "required": [_key(g) for g in skill_groups],
                "properties": {_key(g): {"type": "boolean"} for g in skill_groups},
            },
            "rationale": {
                "type": "object",
                "properties": {s["key"]: {"type": "string"} for s in slots},
            },
            "coherence_check": {"type": "string"},
        },
    }


def validate_doc_selection(
    selection: dict, *, slots: list[dict], layouts: list[Any], summary_variants: list[dict]
) -> list[str]:
    """Return a list of errors; empty means valid. A non-empty result is a validation FAILURE that
    surfaces in the UI, never a silent substitution."""
    errors: list[str] = []

    def _key(x: Any) -> str:
        return x["key"] if isinstance(x, dict) else x

    layout_keys = {_key(x) for x in layouts}
    summary_keys = {_key(x) for x in summary_variants}
    by_key = {s["key"]: s for s in slots}

    if selection.get("layout") not in layout_keys:
        errors.append(f"unknown layout key: {selection.get('layout')!r}")
    if selection.get("summary") not in summary_keys:
        errors.append(f"unknown summary key: {selection.get('summary')!r}")

    chosen = selection.get("slots") or {}
    for slot_key, value in chosen.items():
        slot = by_key.get(slot_key)
        if slot is None:
            errors.append(f"unknown slot key: {slot_key!r}")
            continue
        valid = {v["key"] for v in slot.get("variants", [])}
        if slot.get("optional"):
            valid.add("hide")
        if value not in valid:
            errors.append(f"slot {slot_key!r}: unknown variant key {value!r}")

    for slot in slots:
        if not slot.get("optional") and slot["key"] not in chosen:
            errors.append(f"missing required slot: {slot['key']!r}")

    return errors


def slot_problems(selection: dict, *, slots: list[dict]) -> dict[str, str | None]:
    """The slots whose pick failed the catalog check: slot key -> the key the model named (None
    when it named nothing for a required slot). A slot key the catalog does not have is not a line
    of the operator's document, so it is not listed. Used to keep a model's valid picks and mark
    only the failed ones (the Tailor page's "Needs your pick"); nothing here ever fills a slot."""
    chosen = selection.get("slots") or {}
    out: dict[str, str | None] = {}
    for slot in slots:
        key = slot["key"]
        valid = {v["key"] for v in slot.get("variants", [])}
        if slot.get("optional"):
            valid.add("hide")
        if key not in chosen:
            if not slot.get("optional"):
                out[key] = None
        elif chosen[key] not in valid:
            out[key] = chosen[key]
    return out
