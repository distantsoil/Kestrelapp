"""P3 document-slot selection (SPEC-MODULES 1.3). Assemble the prompt from the opportunity's Block
B/C/E and the live catalog, call run_doc_select (which builds the DYNAMIC per-slot enum schema and
validates against the catalog), and map the result to a render config.

A hallucinated variant key is a validation failure, never a silent substitution: on validation or
repair failure the whole config falls back to the DEFAULTS and `defaults_used` is set so the UI can
flag it prominently. The chosen bad key is never written into the config."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Evaluation, Opportunity
from kestrel.docs.catalog import MasterCatalog, default_config, schema_inputs
from kestrel.llm.guard import GuardResult, run_doc_select
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline._shared import encode_send, plan_hosted_send

_DOCSEL_TIER = "docsel"


@dataclass
class Selection:
    config: dict
    rationale: dict
    defaults_used: bool
    coherence_check: str | None = None
    error: str | None = None


def _catalog_text(catalog: MasterCatalog) -> str:
    lines = ["SLOTS"]
    for sec in catalog.sections:
        s = sec.section
        for sl in sec.slots:
            lines.append(
                f"[{sl.key}] section={s.title or s.org_label} kind={s.section_kind} "
                f"optional={sl.optional} default={sl.default_variant_key}"
            )
            for v in sl.variants:
                lines.append(f'  - {v.key} | tags: {", ".join(v.tags or [])} | "{v.text}"')
    return "\n".join(lines)


def _render_user(spec, *, opp: Opportunity | None, ev: Evaluation | None, catalog: MasterCatalog):
    blocks = (ev.blocks if ev else None) or {}
    b = blocks.get("b_fit", {})
    matches = "; ".join(m.get("requirement", "") for m in (b.get("matches") or [])) or "n/a"
    gaps = "; ".join(g.get("requirement", "") for g in (b.get("gaps") or [])) or "n/a"
    e = blocks.get("e_angle") or blocks.get("e_approach") or {}
    c = blocks.get("c_level") or blocks.get("c_buyers") or {}
    summary_lines = "\n".join(
        f'- {s.get("key")}: "{s.get("text")}"' for s in catalog.summary_variants
    )
    group_lines = "\n".join(
        f"- {g.get('key')}: {g.get('label')} [{', '.join(g.get('items') or [])}]"
        for g in catalog.skill_groups
    )
    layout_lines = "\n".join(f"- {lay.key}: {lay.description or ''}" for lay in catalog.layouts)
    return (
        spec.user.replace("{job.title}", (opp.title if opp else None) or "n/a")
        .replace("{job.company}", (opp.org_name if opp else None) or "n/a")
        .replace("{block_b.matches[].requirement}", matches)
        .replace("{block_b.gaps[].requirement}", gaps)
        .replace("{block_e.lead_with}", e.get("lead_with") or e.get("opening_move") or "n/a")
        .replace("{block_c.scope_read}", c.get("scope_read") or c.get("access_path") or "n/a")
        .replace("{DOC_SLOT_CATALOG}", _catalog_text(catalog))
        .replace('{for each: "- {key}: \\"{text}\\""}', summary_lines)
        .replace('{for each: "- {key}: {label} [{items joined}]"}', group_lines)
        .replace('{for each: "- {key}: {description}"}', layout_lines)
    )


def _config_from_selection(value) -> tuple[dict, dict, str | None]:
    data = value.model_dump()
    config = {
        "layout": data.get("layout"),
        "summary": data.get("summary"),
        "slots": data.get("slots") or {},
        "skill_groups": data.get("skill_groups") or {},
        "redact_orgs": False,
    }
    return config, data.get("rationale") or {}, data.get("coherence_check")


async def select_config(
    session: AsyncSession,
    client,
    catalog: MasterCatalog,
    *,
    opportunity_id: int | None = None,
    on_quarantine=None,
) -> Selection:
    """Run P3 and return a render config. On any validation/repair failure, fall back to defaults
    (never the hallucinated key) and set defaults_used."""
    spec = load_prompt("doc_select/v1")
    opp = await session.get(Opportunity, opportunity_id) if opportunity_id else None
    ev = None
    if opportunity_id:
        ev = (
            await session.execute(
                sa_select(Evaluation)
                .where(Evaluation.opportunity_id == opportunity_id, Evaluation.tier == 2)
                .order_by(Evaluation.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": _render_user(spec, opp=opp, ev=ev, catalog=catalog)},
    ]
    # Hosted mode (SPEC.md 6.4): the catalog carries the operator's own resume text, so the
    # confidential employer is redacted before it leaves the machine, and a missing redaction
    # map refuses the send and renders the defaults (flagged in the UI), never a silent skip.
    settings = getattr(client, "settings", None)
    plan = (await plan_hosted_send(session, settings, _DOCSEL_TIER,
                                   master_id=getattr(catalog.master, "id", None))
            if settings else None)
    if plan is not None and plan.refuse:
        if on_quarantine is not None:
            await on_quarantine(messages, "", plan.refuse, settings.llm.tiers[_DOCSEL_TIER].model)
        return Selection(
            config=default_config(catalog), rationale={}, defaults_used=True,
            error=f"document selection skipped: {plan.refuse}",
        )
    book = None
    if plan is not None:
        messages, book = encode_send(messages, plan.mapping)
    si = schema_inputs(catalog)
    try:
        result: GuardResult = await run_doc_select(
            client,
            messages=messages,
            layouts=si["layouts"],
            summary_variants=si["summary_variants"],
            slots=si["slots"],
            skill_groups=si["skill_groups"],
            on_quarantine=on_quarantine,
            codebook=book,
        )
    except ValueError as exc:
        # A master whose slot has no variants cannot produce a satisfiable schema (an empty enum).
        # That is a catalog problem, not a model failure, so no call is made: render defaults and
        # say exactly which slot is empty (hard rule 3: a schema problem never crashes a run).
        return Selection(
            config=default_config(catalog), rationale={}, defaults_used=True,
            error=f"document selection skipped: {exc}",
        )
    if result.ok and result.value is not None:
        config, rationale, coherence = _config_from_selection(result.value)
        return Selection(
            config=config, rationale=rationale, defaults_used=False, coherence_check=coherence
        )
    # Validation or repair failure: render with DEFAULTS, flag it, never substitute the bad key.
    return Selection(
        config=default_config(catalog), rationale={}, defaults_used=True, error=result.error
    )
