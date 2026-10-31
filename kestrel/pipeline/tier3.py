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

import asyncio
import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
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

_VERSION = {"employment": "evaluate/v2", "consulting": "evaluate_consulting/v2"}
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
            "employer under Settings > Privacy and access); refusing to transmit the employer "
            "unredacted"
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
        # A prior row blocks a re-send by default, and it should: `sent` and `quarantined` mean
        # the payload left the machine (a quarantine is TWO sends: attempt plus repair).
        #
        # `failed` is different, and blocking it wedged the operator permanently
        # (review). A transport failure -- a 401, a refused connection, a timeout -- means
        # nothing was transmitted, so the same payload is safe to try again. Without the token and
        # cost check, a partial response could be counted twice. And because the token is
        # deterministic when confidential mode is off, re-previewing produced the SAME token, so
        # "open a fresh preview to escalate again" could never have worked -- the message sent
        # the operator in a loop that was guaranteed to fail.
        # The payload only provably never left the machine if the request failed to ESTABLISH the
        # connection: refused, DNS failure, connect timeout. `GuardResult.egressed` says so
        # explicitly, decided by exception type in `llm.client._never_left`.
        #
        # It was previously decided by matching the `request error:` prefix (review),
        # and that was wrong: `run_guarded` writes that same prefix for EVERY exception that is
        # not a `ModelDeclined`, so a real HTTP 401/429/500 -- where the provider received the
        # entire payload -- and a timeout waiting for the reply both looked retryable. The
        # operator re-confirmed, the payload went out a second time, and the one-egress guarantee
        # this whole reservation exists to provide was undone by the fix for T1. The test that
        # missed it hand-seeded `error="HTTP 500"`, a string the real code never writes.
        #
        # Tokens and cost are also checked: if the provider billed anything, it saw the payload.
        retryable = (
            already.status == "failed"
            and not already.egressed
            and not already.prompt_tokens
            and not already.completion_tokens
            and not already.cost_usd
        )
        if not retryable:
            # The message must not tell the operator to open a fresh preview: the token is
            # deterministic, so a fresh preview yields the SAME one and the suggestion cannot ever
            # work (review). Say what would actually unblock them.
            if already.status == "failed":
                # Name only what really changes the token (_token_of: model, base_url, messages):
                # the profile feeds the messages and the online model is part of the hash. The
                # posting text is not editable in the app, and the earlier wording pointed at
                # offers, which Tier 3 never touches (D-RR1-5).
                blocked = (
                    # "may have": a cancelled or locally failed send is recorded as egressed
                    # because it cannot be proven otherwise, not because it is known to have gone.
                    f"this payload may already have reached the provider (record #{already.id}, "
                    "failed) "
                    "and is never sent twice. Editing your profile, or choosing a different "
                    "online model in Settings, makes a new request that can be sent."
                )
            else:
                blocked = (
                    "this exact payload has already been sent (record "
                    f"#{already.id}, {already.status}) and is never sent twice."
                )
            return EscalationResult(
                sent=False, transmission_id=already.id, error=blocked, preview=preview
            )
        # Nothing was billed and nothing left the machine: re-reserve THIS row rather than
        # inserting a second one, which the unique index would refuse anyway. The update is a
        # compare-and-swap on status, so two concurrent retries cannot both take the row.
        claimed = await session.execute(
            update(FrontierTransmission)
            .where(
                FrontierTransmission.id == already.id,
                FrontierTransmission.status == "failed",
                FrontierTransmission.egressed.is_(False),
            )
            # egressed=True from the moment the retry is claimed: the send is about to happen,
            # so a kill mid-send must leave a row that reads "may have gone", never the stale
            # False from the earlier connect failure (D-RR1-5). The attempt's own result
            # overwrites it once the call returns.
            .values(status="pending", error=None, egressed=True)
        )
        await session.commit()
        if claimed.rowcount != 1:
            return EscalationResult(
                sent=False,
                transmission_id=already.id,
                error="another retry of this exact payload started a moment ago; not sent twice.",
                preview=preview,
            )
        trans = already
        await session.refresh(trans)
    else:
        # RESERVE BEFORE SENDING. The guard above is a READ, and a read cannot close the window
        # between "has this payload gone out?" and "record that it went out". Two concurrent
        # confirmations -- a double-click, or an htmx retry racing the first response -- both saw
        # no prior row and both sent, so the operator's data left the machine twice and the
        # provider billed twice, against a comment that promises exactly one egress. So the audit
        # row is written and COMMITTED FIRST, as a durable reservation, and only then does the
        # payload go out. The unique index on (opportunity_id, payload_sha256) makes the second
        # reservation fail in the DATABASE rather than in a race between two coroutines.
        #
        # A crash between the reservation and the send is handled by the `pending` recovery below,
        # not by leaving the row in flight forever.
        trans = FrontierTransmission(
            opportunity_id=opp.id,
            provider=preview.provider,
            base_url=preview.base_url,
            model=preview.model,
            confidential=preview.confidential,
            payload=preview.messages,
            payload_sha256=preview.confirmation_token,
            prompt_tokens_estimate=preview.prompt_tokens_estimate,
            status="pending",
        )
        session.add(trans)
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent confirmation reserved this exact payload first. It goes out once, from
            # that request; this one transmits nothing.
            await session.rollback()
            return EscalationResult(
                sent=False,
                error=(
                    "another confirmation of this exact payload was reserved moments ago; "
                    "it was not sent twice. Refresh to see that result."
                ),
                preview=preview,
            )
        await session.refresh(trans)

    spec = prompt_spec(_VERSION[opp.track])
    hook = make_quarantine_hook(session, tier=3, opportunity_id=opp.id)
    # The SEND is inside the guard too (review): the previous fix only wrapped the work
    # that follows it, so a connection dropped DURING the model call -- the most common way a slow
    # hosted request is cancelled, and a client disconnect cancels exactly here -- left the
    # reservation `pending` forever. It is the same stuck state, reached by the most likely path.
    try:
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
    except asyncio.CancelledError:
        await _fail_reservation(session, trans, "cancelled during the send")
        raise
    except Exception as exc:  # noqa: BLE001 - the reservation must reflect what happened
        await _fail_reservation(session, trans, f"send failed: {type(exc).__name__}: {exc}")
        raise
    if preview.mapping is not None:
        await _forget_codebook(session, opp.id)  # the next escalation draws fresh codes

    # Record the usage the provider actually billed, whatever happens next. Before this the
    # token/cost fields were set only on the paths below, so a crash in the post-send work lost
    # the real spend (review).
    trans.prompt_tokens = result.prompt_tokens or None
    trans.completion_tokens = result.completion_tokens or None
    trans.cost_usd = result.cost_usd
    # Carried on the row so a later retry can tell a never-left failure from one the provider
    # received. Defaults True on the column: an unknown outcome counts as egressed.
    trans.egressed = result.egressed

    if result.quarantined or result.value is None:
        trans.status = "quarantined" if result.quarantined else "failed"
        trans.error = result.error
        await session.commit()
        await session.refresh(trans)
        return EscalationResult(
            sent=True, transmission_id=trans.id, quarantined=result.quarantined,
            error=result.error, preview=preview,
        )

    # The payload HAS left the machine at this point. Everything below -- `load_profile`,
    # `scoring.post_process`, building and flushing the Evaluation -- is local work that can still
    # raise, and a client disconnect cancels the handler outright. Left unguarded, any of those
    # left the reservation `pending` forever: indistinguishable from a send still in flight, and
    # permanently blocking that payload from ever being re-sent, with the billed cost unrecorded.
    # A post-send failure is recorded as `failed`, which carries its tokens and cost and so stays
    # non-retryable -- the data really did egress, which is exactly the distinction the guard uses.
    try:
        return await _record_escalation(
            session, opp, preview, trans, result, spec, settings
        )
    except asyncio.CancelledError:
        await _fail_reservation(session, trans, "cancelled after the payload was sent")
        raise
    except Exception as exc:  # noqa: BLE001 - the audit row must reflect what actually happened
        await _fail_reservation(session, trans, f"{type(exc).__name__}: {exc}")
        raise


async def _fail_reservation(session: AsyncSession, trans: Any, reason: str) -> None:
    """Close out a reservation that did not complete.

    `egressed` is left True: whatever the outcome, the operator must not be offered a re-send of
    this payload, because the request may well have reached the provider before it failed. A stuck
    `pending` row is the one state that is unambiguously unrecoverable, so the row is always
    resolved to something the guard can reason about."""
    try:
        trans.status = "failed"
        trans.error = reason[:2000]
        trans.egressed = True
        await session.commit()
    except Exception:  # noqa: BLE001 - never mask the original failure with the cleanup's
        with contextlib.suppress(Exception):
            await session.rollback()


async def _record_escalation(
    session: AsyncSession,
    opp: Any,
    preview: Any,
    trans: Any,
    result: Any,
    spec: Any,
    settings: Any,
) -> EscalationResult:
    """Everything after a completed send: scoring, the Tier 3 Evaluation, the pursuit's stage."""
    value = result.value
    dim_fields = type(value.dimensions).model_fields
    dim_scores = {n: getattr(value.dimensions, n).score for n in dim_fields}
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
    trans.status = "sent"
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
