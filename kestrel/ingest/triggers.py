"""Trigger classification and org-signal accumulation (SPEC.md 5.3, PROMPTS.md P10). This is the
differentiating mechanism of the consulting track and is written by the parent, never delegated.

Each raw signal runs through the P10 `trigger_classify` prompt (structured-output-or-failure via the
guard; one repair then quarantine). The result is appended to `org.signals` so evidence ACCUMULATES
across opportunities. Promotion into the evaluation pipeline:

  - A signal at or above `settings.consulting.min_trigger_confidence` (and is_relevant) promotes on
    its own: it becomes a `trigger_event` opportunity on the consulting track.
  - A signal below that bar is stored but NOT promoted on its own. THREE weak signals on one org
    within 90 days promote the org regardless of individual confidence. Compounding weak evidence is
    the entire point: one 0.35 signal means little, three of them mean something is happening.

Promotion is idempotent (hard rule 5): a `trigger_event` opportunity is keyed by a content hash, so
re-observing the same filing increments seen_count instead of creating a duplicate, and the
compounded promotion for an org happens once (re-runs re-see it)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Opportunity, Org, Quarantine
from kestrel.ingest.orgs import normalized_name, resolve_org
from kestrel.ingest.signal_base import RawSignal
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.prompts import PromptSpec, load_prompt
from kestrel.pipeline._shared import run_prompt_hosted
from kestrel.rules.helpers import age_days, org_signal_count

log = structlog.get_logger()

_P10_VERSION = "trigger_classify/v1"
_PROMOTION_WINDOW_DAYS = 90
_WEAK_SIGNALS_TO_PROMOTE = 3

_EVENT_LABEL = {
    "cio_departure": "CIO departure",
    "cto_departure": "CTO departure",
    "acquisition": "Acquisition",
    "divestiture": "Divestiture",
    "pe_investment": "PE investment",
    "funding_round": "Funding round",
    "security_incident": "Security incident",
    "control_weakness": "Control weakness",
    "compliance_obligation": "Compliance obligation",
    "rapid_it_hiring": "Rapid IT hiring",
    "expansion": "Expansion",
    "restructuring": "Restructuring",
    "other": "Signal",
}


@dataclass
class TriggerOutcome:
    # promoted_strong | promoted_compounded | accumulated | reseen | quarantined | error
    action: str
    opportunity_id: int | None = None
    confidence: float | None = None
    event_type: str | None = None
    error: str | None = None


@lru_cache(maxsize=1)
def _p10_spec() -> PromptSpec:
    return load_prompt(_P10_VERSION)


def _render_user(spec: PromptSpec, *, source_kind: str, source_url: str, signal_text: str) -> str:
    return (
        spec.user.replace("{source_kind}", source_kind or "unknown")
        .replace("{source_url}", source_url or "n/a")
        .replace("{signal_text}", signal_text or "")
    )


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def make_trigger_quarantine_hook(
    session: AsyncSession, *, opportunity_id: int | None = None
) -> QuarantineHook:
    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=opportunity_id,
                tier=0,  # P10 runs on the "trigger" tier, ahead of the Tier 0-3 pipeline
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error or "")[:4000],
                attempts=2,
            )
        )

    return hook


def _signal_key(signal: RawSignal, org: Org, event_type: str, evidence: str) -> str:
    """A STABLE identity for a signal so re-observation dedups even when it carries no external_id
    and no source_url (e.g. an RSS item with neither <guid> nor <link>). Falls back to a hash of
    org + event_type + evidence, so the same recurring newsroom item does not re-accumulate every
    scan and fabricate a compounded promotion (hard rule 5)."""
    return (
        signal.external_id
        or signal.source_url
        or _sha(normalized_name(org.name), event_type, (evidence or "")[:160])
    )


def _has_signal_key(org: Org, key: str) -> bool:
    return any(isinstance(s, dict) and s.get("key") == key for s in (org.signals or []))


def _append_signal(org: Org, record: dict) -> None:
    org.signals = (org.signals or []) + [record]  # reassign: the JSON column is not a MutableList


async def _compounded_hash(session: AsyncSession, org: Org) -> str:
    """A hash that is stable within a promotion wave but distinct across waves: it folds in the
    number of trigger_event opportunities the org already has. Re-runs within a wave dedup (the
    active-opp gate prevents reaching this path while one is live), while a NEW wave after a prior
    opportunity was rejected or aged out gets a fresh hash and a fresh, re-evaluable opportunity."""
    n = (
        await session.execute(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.org_id == org.id, Opportunity.opp_type == "trigger_event")
        )
    ).scalar_one()
    return _sha(normalized_name(org.name), "compounded_signals", str(n))


async def _active_trigger_opp(session: AsyncSession, org: Org) -> Opportunity | None:
    """A recent, non-rejected trigger_event opportunity for this org, used to gate the compounded
    promotion so an org is not promoted for compounding again and again as weak signals land."""
    rows = (
        (
            await session.execute(
                select(Opportunity).where(
                    Opportunity.org_id == org.id,
                    Opportunity.opp_type == "trigger_event",
                )
            )
        )
        .scalars()
        .all()
    )
    for opp in rows:
        if opp.stage in ("t0_reject", "t1_reject"):
            continue
        if age_days(opp.first_seen_at) <= _PROMOTION_WINDOW_DAYS:
            return opp
    return None


async def _create_or_reseen_trigger_opp(
    session: AsyncSession,
    *,
    source,
    org: Org,
    content_hash: str,
    title: str,
    description: str | None,
    event_date: str | None,
    trigger_data: dict,
    external_id: str | None = None,
) -> tuple[Opportunity, bool]:
    existing = (
        await session.execute(
            select(Opportunity).where(Opportunity.content_hash == content_hash).limit(1)
        )
    ).scalar_one_or_none()
    now = utcnow_iso()
    if existing is not None:
        existing.seen_count += 1
        existing.last_seen_at = now
        existing.trigger_data = trigger_data
        return existing, False
    opp = Opportunity(
        track="consulting",
        opp_type="trigger_event",
        org_id=org.id,
        source_id=getattr(source, "id", None),
        external_id=external_id,
        title=title,
        org_name=org.name,
        url=None,  # a trigger event has nothing to apply to (hard rule 1)
        event_date=event_date,
        trigger_data=trigger_data,
        description_md=description,
        first_seen_at=now,
        last_seen_at=now,
        seen_count=1,
        version=1,
        content_hash=content_hash,
        stage="new",
    )
    session.add(opp)
    await session.flush()
    return opp, True


async def classify_and_store(
    session: AsyncSession,
    client: LLMClient,
    signal: RawSignal,
    *,
    source,
    settings: Settings,
) -> TriggerOutcome:
    """Classify one raw trigger signal, accumulate it on the org, and promote per the rules. Returns
    an outcome; never raises (a transport error is reported as action='error' for the runner to
    retry). The caller commits."""
    text = (signal.text or "").strip()
    if not text:
        return TriggerOutcome("accumulated", error="empty signal text")

    spec = _p10_spec()
    user = _render_user(
        spec,
        source_kind=getattr(source, "kind", "unknown"),
        source_url=signal.source_url or "",
        signal_text=text,
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    hook = make_trigger_quarantine_hook(session)
    result = await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=0, on_quarantine=hook
    )

    if not result.ok and not result.quarantined:
        return TriggerOutcome("error", error=result.error)  # transport error: runner retries
    if result.quarantined or result.value is None:
        return TriggerOutcome("quarantined", error=result.error)

    trigger = result.value
    org_name = (trigger.org_name or signal.org_name or "").strip() or None
    org = await resolve_org(session, name=org_name, domain=None)

    event_date = trigger.event_date or signal.published_date
    key = _signal_key(
        org=org, signal=signal, event_type=trigger.event_type, evidence=trigger.evidence_quote
    )
    already = _has_signal_key(org, key)
    record = {
        "key": key,
        "category": trigger.event_type,
        "statement": trigger.implied_need or trigger.evidence_quote,
        "event_date": (event_date or "")[:10] or None,
        "confidence": trigger.confidence,
        "evidence_quote": trigger.evidence_quote,
        "urgency_window_days": trigger.urgency_window_days,
        "source_kind": getattr(source, "kind", None),
        "source_url": signal.source_url,
        "external_id": signal.external_id,
        "observed_at": utcnow_iso(),
    }
    # Carry the investor identity through when the source knows it (PE portfolio signals set
    # raw={"firm": ...}). This is what the networking graph keys tier-3 warm paths on, so it must
    # survive onto the org.signals record rather than being lost at classification.
    firm = (signal.raw or {}).get("firm") if isinstance(signal.raw, dict) else None
    if firm:
        record["firm"] = firm
    # Record the signal on the org exactly once (idempotent accumulation). A re-observation still
    # advances the promoted opportunity's liveness below (seen_count/last_seen), but never
    # re-appends and never re-counts toward the three-weak-signals promotion.
    if not already:
        _append_signal(org, record)

    strong = (
        trigger.is_relevant and trigger.confidence >= settings.consulting.min_trigger_confidence
    )
    trigger_data = {
        "event_type": trigger.event_type,
        "org_name": trigger.org_name,
        "event_date": record["event_date"],
        "evidence_quote": trigger.evidence_quote,
        "source_url": signal.source_url,
        "confidence": trigger.confidence,
        "implied_need": trigger.implied_need,
        "urgency_window_days": trigger.urgency_window_days,
        "reasoning": trigger.reasoning,
    }

    if strong:
        label = _EVENT_LABEL.get(trigger.event_type, "Signal")
        # Distinct filings (distinct external_id/source_url) get distinct opportunities; the SAME
        # filing re-observed reseens and advances liveness.
        content_hash = _sha(
            normalized_name(org.name),
            trigger.event_type,
            signal.external_id or signal.source_url or record["event_date"] or "",
        )
        opp, created = await _create_or_reseen_trigger_opp(
            session,
            source=source,
            org=org,
            content_hash=content_hash,
            title=f"{label}: {org.name}",
            description=f"{trigger.evidence_quote}\n\n{trigger.reasoning}".strip(),
            event_date=record["event_date"],
            trigger_data=trigger_data,
            external_id=signal.external_id,
        )
        return TriggerOutcome(
            "promoted_strong" if created else "reseen",
            opportunity_id=opp.id,
            confidence=trigger.confidence,
            event_type=trigger.event_type,
        )

    if already:  # a re-observed weak signal: nothing new to count or promote
        return TriggerOutcome(
            "reseen", confidence=trigger.confidence, event_type=trigger.event_type
        )

    # weak: accumulate, and promote the ORG if it now has 3+ signals within the window.
    if org_signal_count(org, _PROMOTION_WINDOW_DAYS) >= _WEAK_SIGNALS_TO_PROMOTE:
        if await _active_trigger_opp(session, org) is None:
            content_hash = await _compounded_hash(session, org)
            compounded = {
                "event_type": trigger.event_type,
                "org_name": org.name,
                "event_date": record["event_date"],
                "evidence_quote": trigger.evidence_quote,
                "source_url": signal.source_url,
                "confidence": trigger.confidence,
                "implied_need": "Multiple weak signals within 90 days imply an emerging IT need",
                "urgency_window_days": trigger.urgency_window_days,
                "reasoning": "Promoted by the three-weak-signals rule, not individual confidence.",
                "compounded": True,
            }
            opp, created = await _create_or_reseen_trigger_opp(
                session,
                source=source,
                org=org,
                content_hash=content_hash,
                title=f"Multiple signals: {org.name}",
                description="Three or more public signals on this organization within 90 days.",
                event_date=record["event_date"],
                trigger_data=compounded,
            )
            return TriggerOutcome(
                "promoted_compounded" if created else "reseen",
                opportunity_id=opp.id,
                confidence=trigger.confidence,
                event_type=trigger.event_type,
            )

    return TriggerOutcome(
        "accumulated", confidence=trigger.confidence, event_type=trigger.event_type
    )
