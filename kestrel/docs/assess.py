"""Resume assessment (PROMPTS.md P13, D-DOC-2). Reads one document master (roles, bullets with
their slot keys) and the operator's own targets from profile.yml, and asks the model how the
document reads for those targets: overall fit, strengths, gaps, and bullets to strengthen.

The assessment is ADVISORY and DISPLAYED ONLY. Nothing it returns is written to doc_variant or
anywhere else that reaches a PDF (hard rule 2): the model never writes resume text, and the schema
has no field for replacement text. Slot keys in the reply are an enum of this master's real keys,
built per call, so a hallucinated key is structurally impossible on a constrained backend and a
validation failure (repaired once, then quarantined) on any other (hard rule 3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, create_model

from kestrel.docs.catalog import MasterCatalog
from kestrel.llm.schemas import (
    P13ResumeAssess,
    P13Strength,
    P13Strengthen,
    _cap,
)

log = structlog.get_logger()

VERSION = "resume_assess/v1"
_TEXT_MAX = 600


@dataclass
class Assessment:
    """What the page shows: the validated reply, or a plain-language error."""

    value: P13ResumeAssess | None = None
    error: str | None = None
    quarantined: bool = False
    slot_labels: dict[str, str] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)


def slot_keys(catalog: MasterCatalog) -> list[str]:
    return [sl.key for sl in catalog.all_slots()]


def assess_model_for(keys: list[str]) -> type[BaseModel]:
    """The P13 model with every slot key restricted to this master's keys."""
    if not keys:
        raise ValueError("a document with no bullets cannot be assessed")
    key_type: Any = Literal[tuple(keys)]  # type: ignore[valid-type]
    strength = create_model(
        "P13StrengthForMaster", __base__=P13Strength,
        slot_keys=(Annotated[list[key_type], _cap(6)], ...),
    )
    strengthen = create_model(
        "P13StrengthenForMaster", __base__=P13Strengthen, slot_key=(key_type, ...),
    )
    return create_model(
        "P13ResumeAssessForMaster", __base__=P13ResumeAssess,
        strengths=(Annotated[list[strength], _cap(6)], ...),
        strengthen=(Annotated[list[strengthen], _cap(8)], ...),
    )


def _const_to_enum(node: Any) -> Any:
    """A one-key master makes pydantic emit `const`, which is outside the constrained-decoding
    subset (CLAUDE.md, JSON Schema subset); write it as a one-value enum."""
    if isinstance(node, list):
        return [_const_to_enum(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _const_to_enum(v) for k, v in node.items() if k != "const"}
    if "const" in node:
        out["enum"] = [node["const"]]
    return out


def assess_schema(model_cls: type[BaseModel]) -> dict:
    from kestrel.llm.client import json_schema_for

    return _const_to_enum(json_schema_for(model_cls))


def profile_targets(track: str) -> tuple[list[str], list[str]]:
    """(target titles, must-haves) from the operator's OWN profile blocks only: a track with no
    block in profile.yml, or a terms section still shown from the shipped example, contributes
    nothing (the example's fictional targets must never steer a real assessment)."""
    from kestrel.services.profile_edit import load_state

    state = load_state()
    if track not in state.present:
        return [], []
    block = state.tracks.get(track) or {}
    example_only = set(state.from_example.get(track, ()))
    titles: list[str] = []
    if track == "employment" and "employment_terms" not in example_only:
        titles = list((block.get("employment_terms") or {}).get("target_titles") or [])
    elif track == "consulting" and "consulting_terms" not in example_only:
        titles = list((block.get("consulting_terms") or {}).get("service_lines") or [])
    must = list(block.get("must_have") or [])
    clean = [str(t).strip() for t in titles if str(t).strip()]
    return clean, [str(m).strip() for m in must if str(m).strip()]


def document_text(catalog: MasterCatalog, *, redact: bool = False) -> str:
    """The master as the model reads it: summary, each section's header line, every bullet's
    default wording labeled with its slot key, and the skills, with the real names. On an online
    tier the send replaces the current employer with a code (pipeline._shared.encode_send,
    D-PRIV-1); `redact` is kept for callers and no longer changes the text."""
    lines: list[str] = []
    summaries = catalog.summary_variants
    if summaries and summaries[0].get("text"):
        lines += ["SUMMARY", str(summaries[0]["text"])[:_TEXT_MAX], ""]
    for n, sec in enumerate(catalog.sections, start=1):
        s = sec.section
        org = s.org_label or ""
        dates = " to ".join(x for x in (s.start_date, s.end_date) if x)
        head = ", ".join(x for x in (s.title, org) if x) or s.section_kind.replace("_", " ")
        kind = "ROLE" if s.section_kind == "role" else s.section_kind.replace("_", " ").upper()
        lines.append(f"{kind} {n}: {head}" + (f" ({dates})" if dates else ""))
        if s.context_line:
            lines.append(f"Context: {s.context_line[:_TEXT_MAX]}")
        for sl in sec.slots:
            v = next((x for x in sl.variants if x.key == sl.default_variant_key), None) or (
                sl.variants[0] if sl.variants else None)
            if v is not None:
                lines.append(f"- [{sl.key}] {v.text[:_TEXT_MAX]}")
        lines.append("")
    for g in catalog.skill_groups:
        items = ", ".join(str(i) for i in (g.get("items") or []))
        lines.append(f"SKILLS {g.get('label') or g.get('key')}: {items}"[:_TEXT_MAX])
    return "\n".join(lines).strip()


QUARANTINE_TIER = 13  # the quarantine row's `tier` names the prompt, as P8 and P9 do


async def assess_master(
    client, catalog: MasterCatalog, *, session=None, redact: bool = False
) -> Assessment:
    """Run P13 through the guard with the per-master schema. Writes nothing to the document;
    with a session, a failed reply is staged as a quarantine row (the caller commits) and a
    tier routed to a hosted backend gets the same confidential redaction or refusal as every
    other hosted call (pipeline._shared). Returns the reply or a plain error, never raises for
    a bad reply."""
    from kestrel.llm.guard import GuardResult, run_guarded
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline._shared import (
        encode_send,
        make_quarantine_hook,
        plan_hosted_send,
        record_refusal,
    )
    from kestrel.web.pages.documents import slot_label

    keys = slot_keys(catalog)
    titles, must = profile_targets(catalog.master.track)
    labels: dict[str, str] = {}
    for sec in catalog.sections:
        where = sec.section.title or sec.section.org_label or "Section"
        for n, sl in enumerate(sec.slots, start=1):
            labels[sl.key] = f"{where}, bullet {n} ({slot_label(sl.key)})"
    out = Assessment(slot_labels=labels, targets=titles)
    if not keys:
        out.error = "This document has no bullets yet, so there is nothing to assess."
        return out
    spec = load_prompt(VERSION)
    model_cls = assess_model_for(keys)
    kind = "resume" if catalog.master.doc_kind == "resume" else "capability statement"
    user = (
        spec.user.replace("{doc_kind}", kind)
        .replace("{target_titles}", "; ".join(titles) or "(none set in Profile)")
        .replace("{must_haves}", "; ".join(must) or "(none set in Profile)")
        .replace("{document}", document_text(catalog, redact=redact))
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    hook = None
    plan = None
    book = None
    res: GuardResult | None = None
    if session is not None:
        hook = make_quarantine_hook(session, tier=QUARANTINE_TIER, opportunity_id=None)
        plan = await plan_hosted_send(session, client.settings, spec.tier,
                                      master_id=getattr(catalog.master, "id", None))
        if plan.refuse:
            tcfg = client.settings.llm.tiers.get(spec.tier)
            record_refusal(session, tier=QUARANTINE_TIER, opportunity_id=None,
                           model=tcfg.model if tcfg else "", reason=plan.refuse,
                           messages=messages)
            res = GuardResult(ok=False, quarantined=True, error=plan.refuse)
        else:
            messages, book = encode_send(messages, plan.mapping)
    if res is None:
        res = await run_guarded(
            client, tier=spec.tier, messages=messages, model_cls=model_cls,
            schema=assess_schema(model_cls), temperature=spec.temperature,
            max_tokens=spec.max_tokens, schema_name=VERSION.replace("/", "_"),
            on_quarantine=hook, reasoning_hint=spec.reasoning,
            reply_filter=book,
        )
    if not res.ok or res.value is None:
        out.quarantined = res.quarantined
        log.warning("resume_assess_failed", quarantined=res.quarantined, error=res.error)
        if res.quarantined and res.error and res.error.startswith("hosted send refused"):
            out.error = ("Not sent: confidential mode is on and tier 2 runs on a hosted model, "
                         "but no current employer is chosen, so your employer's name would "
                         "leave this machine. Choose it under Settings, Core, run tier 2 on a "
                         "local model, or turn off confidential mode.")
        elif res.quarantined:
            out.error = ("The model's answer could not be used, even after one retry, so no "
                         "assessment is shown. It was kept for review on the Quarantine page. "
                         "Try again, or pick a different model for tier 2 on the Models page.")
        else:
            out.error = f"Could not reach the model: {res.error}"
        return out
    out.value = res.value  # type: ignore[assignment]
    return out
