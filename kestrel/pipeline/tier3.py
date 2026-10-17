"""Tier 3 — operator-confirmed escalation to the hosted frontier (SPEC.md 6.4, hard rule 1).

Tier 3 is the ONLY path that transmits operator data off the machine, and it is NEVER automatic.
The worker never runs it; there is no Tier 3 task kind. The operator escalates a single opportunity
and is shown a PREVIEW: the exact payload that would be transmitted, the destination (provider, base
URL, model), and an estimated token cost. Nothing is sent until they confirm, and confirmation is
gated on a hash of the exact payload, so what the operator approved is byte-for-byte what is sent.
Confidential mode redacts the operator's current employer BEFORE the payload is built. Every actual
transmission is recorded in `frontier_transmission`.

The escalation reuses the Tier 2 deep-evaluation prompt and schema, run against the stronger hosted
model, and post-processes identically (the SYSTEM decides the verdict, never the model)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.models import Evaluation, FrontierTransmission, Opportunity, Org, Source
from kestrel.ingest.tokens import count_tokens
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import run_guarded
from kestrel.pipeline import scoring
from kestrel.pipeline._shared import jd_budget, make_quarantine_hook, prompt_spec
from kestrel.pipeline.context import (
    build_tier2_values,
    comp_intel_summary,
    org_signals_lines,
    render_user,
)
from kestrel.pipeline.profile import load_profile
from kestrel.web.redact import confidential

log = structlog.get_logger()

_VERSION = {"employment": "evaluate/v1", "consulting": "evaluate_consulting/v1"}
# The tier role for the hosted escalation. Separate from "tier3" (the LOCAL deep model used by cover
# letters and local work) so nothing local is ever routed to the external backend.
_FRONTIER_TIER = "frontier"


# The provider as the operator knows it, for messages (never the internal backend name).
_PROVIDER_NAMES = {"openrouter": "OpenRouter", "openmodel": "OpenModel", "anthropic": "Anthropic",
                   "openai": "OpenAI"}


@dataclass
class EscalationPreview:
    """Everything the operator must see BEFORE anything leaves the machine. `messages` is the exact
    payload; `confirmation_token` gates the send and binds it to this payload byte-for-byte."""

    opportunity_id: int
    provider: str
    base_url: str
    model: str
    confidential: bool
    messages: list[dict]
    prompt_tokens_estimate: int
    confirmation_token: str
    unavailable: str | None = None  # e.g. missing API key -> preview only, cannot transmit
    error: str | None = None
    # D-PRIV-1: the stand-in codes the payload was built with (a privacy.CodeBook). They are part
    # of the confirmation token, so a transmit restores the reply with exactly these codes.
    mapping: Any = None


@dataclass
class EscalationResult:
    sent: bool
    transmission_id: int | None = None
    evaluation_id: int | None = None
    verdict: str | None = None
    score: float | None = None  # the displayed score, so the fit word can say strong or good
    quarantined: bool = False
    error: str | None = None
    preview: EscalationPreview | None = field(default=None)


_CODEBOOK_KEY = "tier3.codebook.{opp}"


async def _codebook(session: AsyncSession, opp_id: int, mapping, messages: list[dict]):
    """The escalation's stand-in codes (D-PRIV-1): drawn fresh for each escalation and kept,
    in the setting table, from the preview until the transmit, so the payload the operator
    confirmed is the payload sent and the reply is restored with the same codes. A changed
    employer map draws new codes (and so a new confirmation token)."""
    from kestrel.db.models import Setting
    from kestrel.privacy import CodeBook

    row = await session.get(Setting, _CODEBOOK_KEY.format(opp=opp_id))
    book = CodeBook.from_json(mapping, row.value) if row is not None else None
    return book or mapping.codebook(m.get("content") or "" for m in messages)


async def _keep_codebook(session: AsyncSession, opp_id: int, book) -> None:
    """Store the codes this preview used (after encoding, so every one is in it)."""
    from kestrel.db.models import Setting

    key = _CODEBOOK_KEY.format(opp=opp_id)
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=book.to_json()))
    elif row.value != book.to_json():
        row.value = book.to_json()
    else:
        return
    await session.commit()


async def _forget_codebook(session: AsyncSession, opp_id: int) -> None:
    from kestrel.db.models import Setting

    row = await session.get(Setting, _CODEBOOK_KEY.format(opp=opp_id))
    if row is not None:
        await session.delete(row)


def _canonical(model: str, base_url: str, messages: list[dict], token_map: str = "") -> str:
    return json.dumps(
        {"model": model, "base_url": base_url, "messages": messages,
         **({"token_map": token_map} if token_map else {})},
        sort_keys=True,
        ensure_ascii=False,
    )


def _token_of(model: str, base_url: str, messages: list[dict], token_map: str = "") -> str:
    return hashlib.sha256(
        _canonical(model, base_url, messages, token_map).encode("utf-8")).hexdigest()


async def build_escalation_preview(
    session: AsyncSession, settings: Settings, opportunity_id: int
) -> EscalationPreview:
    """Build (but never send) the Tier 3 payload for one opportunity. Read-only."""
    tcfg = settings.llm.tiers.get(_FRONTIER_TIER)
    backend_name = tcfg.backend if tcfg else "frontier"
    backend = settings.llm.backends.get(backend_name) if tcfg else None
    base_url = backend.base_url if backend else ""
    model = tcfg.model if tcfg else ""

    def _err(msg: str) -> EscalationPreview:
        return EscalationPreview(
            opportunity_id, backend_name, base_url, model, False, [], 0, "", error=msg
        )

    opp = await session.get(Opportunity, opportunity_id)
    if opp is None:
        return _err("opportunity missing")
    if opp.track not in _VERSION:
        return _err(f"track '{opp.track}' has no Tier 3 evaluation prompt")

    enabled, mapping = await confidential(session)
    org = await session.get(Org, opp.org_id) if opp.org_id else None
    source = await session.get(Source, opp.source_id) if opp.source_id else None
    profile = load_profile(opp.track)
    spec = prompt_spec(_VERSION[opp.track])
    comp_intel = await comp_intel_summary(session, opp)
    values = build_tier2_values(
        opp=opp,
        org=org,
        profile=profile,
        source_name=source.name if source else "",
        # The frontier tier is hosted in either mode, so its slice is sized to the model's
        # window like a hosted Tier 2 call (SPEC.md 6.4, D-LLM-3).
        max_jd_tokens=jd_budget(
            settings, _FRONTIER_TIER, settings.pipeline.max_jd_tokens,
            external=bool(backend is not None and backend.external),
        ),
        few_shot="",
        comp_intel=comp_intel,
    )
    user = render_user(
        spec.user,
        values,
        trigger_event=bool(opp.trigger_data or opp.event_date),
        posted=bool(opp.description_md) or not (opp.trigger_data or opp.event_date),
        org_signals=org_signals_lines(org),
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    book = None
    if enabled and mapping:
        book = await _codebook(session, opportunity_id, mapping, messages)
        messages = book.encode_messages(messages)
        await _keep_codebook(session, opportunity_id, book)

    token = _token_of(model, base_url, messages,
                      json.dumps(book.to_json(), ensure_ascii=False) if book else "")
    est = sum(count_tokens(m["content"]) for m in messages)

    # Only claim "redacted" when a redaction map actually exists; confidential mode with an empty
    # map redacts NOTHING, and a badge saying otherwise is a false privacy assurance before an
    # external send.
    redaction_applied = enabled and bool(mapping)

    unavailable = None
    if backend is None:
        unavailable = f"no backend '{backend_name}' configured"
    elif not backend.external:
        unavailable = f"backend '{backend_name}' is not marked external; refusing as Tier 3"
    elif enabled and not mapping:
        # Confidential search on, but nothing to redact with: refuse rather than transmit the
        # current employer unredacted to a hosted model.
        unavailable = (
            "confidential mode is on but no redaction map is configured (choose your current "
            "employer under Settings, Core); refusing to transmit the employer unredacted"
        )
    elif backend.api_key_env and not _has_key(backend.api_key_env) and not backend.api_key:
        unavailable = (
            f"No {_PROVIDER_NAMES.get(backend.provider, backend_name)} key is saved; add it in "
            "Settings > Hosted before escalating (preview only until then)"
        )

    return EscalationPreview(
        opportunity_id=opportunity_id,
        provider=backend_name,
        base_url=base_url,
        model=model,
        confidential=redaction_applied,
        messages=messages,
        prompt_tokens_estimate=est,
        confirmation_token=token,
        unavailable=unavailable,
        mapping=book,
    )


def _has_key(env_name: str) -> bool:
    from kestrel.config import get_secret

    return bool(get_secret(env_name))


async def transmit_escalation(
    session: AsyncSession,
    client: LLMClient,
    settings: Settings,
    opportunity_id: int,
    *,
    confirmation_token: str,
) -> EscalationResult:
    """Transmit the Tier 3 payload to the hosted frontier — ONLY after re-deriving the preview and
    confirming that `confirmation_token` matches the previewed MESSAGE payload (the operator
    content) byte-for-byte. A mismatch, a missing key, an already-sent payload, or an error
    means nothing is sent. The client wraps the messages in the request envelope (schema, params);
    the audit records the operator payload, which is the privacy-relevant part. Records the
    transmission in frontier_transmission and writes a Tier 3 Evaluation on success."""
    preview = await build_escalation_preview(session, settings, opportunity_id)
    if preview.error:
        return EscalationResult(sent=False, error=preview.error, preview=preview)
    if preview.unavailable:
        return EscalationResult(sent=False, error=preview.unavailable, preview=preview)
    if not confirmation_token or confirmation_token != preview.confirmation_token:
        # The payload changed since the preview (or the token is wrong): refuse, transmit nothing.
        return EscalationResult(
            sent=False,
            error="confirmation token does not match the current payload; re-preview and confirm",
            preview=preview,
        )

    opp = await session.get(Opportunity, opportunity_id)
    if opp is None:  # deleted between preview and confirm
        return EscalationResult(sent=False, error="opportunity missing", preview=preview)

    # Idempotency / replay guard: one confirmation authorizes ONE send of this exact payload. If an
    # identical payload was already transmitted, refuse rather than re-send operator data
    # off-machine (a double-click, an htmx retry, or a replayed token must not egress twice).
    # ANY prior record counts, whatever its status: a "failed" or "quarantined" row means the
    # payload already left the machine (a quarantine is two sends: attempt plus repair). Re-trying
    # needs a fresh preview and a fresh confirmation, never a replayed token.
    already = (
        await session.execute(
            select(FrontierTransmission)
            .where(
                FrontierTransmission.opportunity_id == opp.id,
                FrontierTransmission.payload_sha256 == confirmation_token,
            )
            .order_by(FrontierTransmission.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if already is not None:
        return EscalationResult(
            sent=False,
            transmission_id=already.id,
            error=(
                f"this exact payload was already transmitted (record #{already.id}, "
                f"{already.status}); not re-sent. Open a fresh preview to escalate again."
            ),
            preview=preview,
        )

    spec = prompt_spec(_VERSION[opp.track])
    hook = make_quarantine_hook(session, tier=3, opportunity_id=opp.id)
    result = await run_guarded(
        client,
        tier=_FRONTIER_TIER,
        messages=preview.messages,
        model_cls=spec.model_cls,
        temperature=None,  # fall back to the frontier tier config
        max_tokens=None,
        schema_name=spec.version.replace("/", "_"),
        on_quarantine=hook,
        allow_external=True,  # the ONE place this is permitted
        reply_filter=preview.mapping if preview.confidential else None,
    )
    if preview.mapping is not None:
        await _forget_codebook(session, opp.id)  # the next escalation draws fresh codes

    trans = FrontierTransmission(
        opportunity_id=opp.id,
        provider=preview.provider,
        base_url=preview.base_url,
        model=preview.model,
        confidential=preview.confidential,
        payload=preview.messages,
        payload_sha256=preview.confirmation_token,
        prompt_tokens_estimate=preview.prompt_tokens_estimate,
        status="sent",
        prompt_tokens=result.prompt_tokens or None,
        completion_tokens=result.completion_tokens or None,
        cost_usd=result.cost_usd,
    )

    if result.quarantined or result.value is None:
        trans.status = "quarantined" if result.quarantined else "failed"
        trans.error = result.error
        session.add(trans)
        await session.commit()
        await session.refresh(trans)
        return EscalationResult(
            sent=True, transmission_id=trans.id, quarantined=result.quarantined,
            error=result.error, preview=preview,
        )

    value = result.value
    dim_scores = {n: getattr(value.dimensions, n).score for n in value.dimensions.model_fields}
    blockers = [{"kind": b.kind, "detail": b.detail} for b in value.blockers]
    post = scoring.post_process(
        track=opp.track,
        dimension_scores=dim_scores,
        holistic=value.holistic,
        model_verdict=value.verdict,
        blockers=blockers,
        legitimacy_status=value.legitimacy.status,
        work_auth_signal=getattr(value, "work_auth_signal", None),
        requires_sponsorship=bool(getattr(load_profile(opp.track), "requires_sponsorship", False)),
        settings=settings,
    )
    ev = Evaluation(
        opportunity_id=opp.id,
        tier=3,
        track=opp.track,
        content_version=opp.version,
        model=preview.model,
        prompt_version=spec.version,
        verdict=post.verdict,
        verdict_model=post.verdict_model,
        score=post.displayed_score,
        weighted_score=post.weighted,
        needs_review=post.needs_review,
        forced_by=post.forced_by,
        dimensions={
            k: {
                "score": getattr(value.dimensions, k).score,
                "why": getattr(value.dimensions, k).why,
            }
            for k in dim_scores
        },
        blocks=value.blocks.model_dump(),
        blockers=post.blockers,
        legitimacy=value.legitimacy.model_dump(),
        raw_response=result.raw,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
    )
    session.add(ev)
    await session.flush()
    trans.evaluation_id = ev.id
    session.add(trans)
    opp.stage = "t3_done"
    opp.stage_reason = f"t3 {post.verdict} ({post.displayed_score:.2f}, hosted {preview.model})"
    await session.commit()
    await session.refresh(trans)
    return EscalationResult(
        sent=True, transmission_id=trans.id, evaluation_id=ev.id, verdict=post.verdict,
        score=post.displayed_score, preview=preview,
    )
