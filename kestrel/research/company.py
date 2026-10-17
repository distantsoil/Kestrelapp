"""Company research (P5, SPEC-MODULES 2.1). Turns acquired excerpts (kestrel.research.excerpts)
into a structured OrgResearch record via the schema-guarded P5 prompt (kestrel.llm.guard).

Two constraints are enforced HERE, in code, not merely stated in the prompt:
  - Fewer than two excerpts never reaches the model. The model synthesizes supplied excerpts and
    does not browse, so with fewer than two there is nothing to synthesize; the caller gets
    insufficient_sources=True rather than a fabricated answer (SPEC-MODULES 2.1).
  - Every signal's source_index is resolved against the supplied excerpt set
    (kestrel.research.validation.valid_source_index). An out-of-range index is DROPPED, never
    written as an unattributed claim (hard rule 2: the model selects, it never invents).

Validated signals are written back onto org.signals so evidence accumulates across every
opportunity at that organization (feeding the consulting track's multi-signal promotion rule), and
size_band/sector/ownership are enriched via keyword inference over the research summary -- filling
only what Phase 3 normalization left as None, NEVER overwriting a more specific existing value."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Org, OrgResearch, Quarantine
from kestrel.ingest.orgs import infer_org_attrs
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline._shared import run_prompt_hosted
from kestrel.research.excerpts import ExcerptSet, acquire_excerpts
from kestrel.research.validation import valid_source_index

log = structlog.get_logger()

_VERSION = "research/v1"
# The literal placeholder in kestrel/llm/prompts/research.v1.md's USER template, replaced with the
# rendered excerpt block (ExcerptSet.prompt_block()).
_FOR_EACH_SOURCE = '{for each source: "[{index}] {url}\\n{excerpt}"}'


def _render_user(
    template: str, *, org_name: str, excerpts: ExcerptSet, operator_paste: str | None
) -> str:
    text = template.replace("{org.name}", org_name)
    text = text.replace(_FOR_EACH_SOURCE, excerpts.prompt_block())
    text = text.replace('{operator_paste or "none"}', operator_paste or "none")
    return text


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    """Record a failed P5 call to the SAME session the caller commits, so the quarantine row and
    the (insufficient_sources) OrgResearch it produces are written atomically."""

    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=None,
                tier=5,
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error or "")[:4000],
                attempts=2,
            )
        )

    return hook


def _validate_signals(signals: list, excerpts: ExcerptSet) -> list[dict]:
    """Keep a signal only if its source_index resolves to a supplied excerpt. An out-of-range
    index is dropped rather than written as an unattributed claim (hard rule 2)."""
    out: list[dict] = []
    for sig in signals:
        if not valid_source_index(sig.source_index, len(excerpts)):
            continue
        excerpt = excerpts.resolve(sig.source_index)
        out.append(
            {
                "category": sig.category,
                "statement": sig.statement,
                "source_index": sig.source_index,
                "implication": sig.implication,
                "url": excerpt.url if excerpt is not None else None,
            }
        )
    return out


def _enrich_org(org: Org, validated_signals: list[dict], summary_md: str | None) -> None:
    """(a) append each validated signal to org.signals, deduped by (category, statement) against
    what is already there, so re-researching the same org does not inflate the signal count.
    (b) derive size_band/sector/ownership from the research summary and set them ONLY where the
    org's current value is None -- never overwrite a more specific existing value with a vaguer
    one set by keyword inference."""
    now = utcnow_iso()
    existing = org.signals or []
    seen = {(s.get("category"), s.get("statement")) for s in existing if isinstance(s, dict)}
    new = [
        {
            "category": s["category"],
            "statement": s["statement"],
            "source_url": s["url"],
            "observed_at": now,
            "source": "research",
        }
        for s in validated_signals
        if (s["category"], s["statement"]) not in seen
    ]
    if new:
        org.signals = existing + new

    raw = SimpleNamespace(org_name=org.name, title=None)
    attrs = infer_org_attrs(raw, summary_md)
    for key in ("size_band", "sector", "ownership"):
        if getattr(org, key) is None and attrs.get(key):
            setattr(org, key, attrs[key])


async def research_org(
    session: AsyncSession,
    client: LLMClient,
    org: Org,
    *,
    settings: Settings,
    http: Any = None,
    operator_paste: str | None = None,
) -> OrgResearch:
    """Acquire excerpts, synthesize them into a structured OrgResearch record (P5), validate every
    signal's source attribution, and enrich the org record. Never fabricates: fewer than two
    excerpts, or a not-ok/quarantined model call, returns insufficient_sources=True instead of a
    synthesized answer."""
    excerpts = await acquire_excerpts(
        session, http, org, settings=settings, operator_paste=operator_paste
    )

    if len(excerpts) < 2:
        record = OrgResearch(
            org_id=org.id,
            summary_md=None,
            signals=[],
            unknowns=[],
            angle=None,
            sources=excerpts.source_records(),
            insufficient_sources=True,
        )
        session.add(record)
        await session.flush()
        return record

    spec = load_prompt(_VERSION)
    user = _render_user(
        spec.user, org_name=org.name or "", excerpts=excerpts, operator_paste=operator_paste
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]

    result = await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=5,
        on_quarantine=_quarantine_hook(session),
    )

    if not result.ok or result.value is None:
        record = OrgResearch(
            org_id=org.id,
            summary_md=f"Research failed: {result.error or 'unknown error'}",
            signals=[],
            unknowns=[],
            angle=None,
            sources=excerpts.source_records(),
            insufficient_sources=True,
        )
        session.add(record)
        await session.flush()
        return record

    value = result.value
    validated = _validate_signals(value.signals, excerpts)

    record = OrgResearch(
        org_id=org.id,
        summary_md=value.summary_md,
        signals=validated,
        unknowns=value.unknowns,
        angle=value.angle_for_candidate,
        sources=excerpts.source_records(),
        insufficient_sources=False,
    )
    session.add(record)
    await session.flush()

    _enrich_org(org, validated, value.summary_md)

    return record
