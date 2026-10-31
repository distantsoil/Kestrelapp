"""Negotiation scripts (P11, SPEC-MODULES 4.3). Drafts for the standard situations, in the
operator's voice, always to edit and deliver themselves. The system never sends.

Three hard rules are enforced in CODE, not merely stated in the prompt:
  - The WALK-AWAY number is supplied by the operator and echoed back unchanged. Nothing here
    proposes, questions, or adjusts it; there is no code path that computes a walk-away.
  - Every justification must rest on a supplied proof point or a supplied market data point with its
    sample size. A script whose justification rests on neither is FLAGGED (supported=False);
    the operator sees which scripts stand on real evidence and which are the model reaching.
  - The competing-offer scenario is only ever scripted when a REAL competing offer exists. Absent
    one, the scenario is not even sent, so there is nothing to bluff with (never imply an offer that
    does not exist)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Pursuit, Quarantine
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.prompts import load_prompt
from kestrel.negotiation.benchmark import benchmark_offer
from kestrel.negotiation.valuation import bonus_amount, first_year_comp
from kestrel.pipeline._shared import run_prompt_hosted
from kestrel.pipeline.profile import load_profile

_SCENARIOS = {
    "first_number_deflection": "Deflecting the first-number question",
    "counter_below_target": "Countering below-target with a specific justified number",
    "geo_discount": "Pushing back on a geographic discount",
    "equity_vs_cash": "Equity versus cash trade",
    "start_date": "Start date and notice period",
    "title_level": "Title and level (for a senior-track candidate, often worth more than base)",
    "competing_offer": "Using a competing offer without bluffing",
    "rate_vs_scope": "Consulting: rate versus scope",
    "payment_terms": "Consulting: payment terms",
    "renewal_option": "Consulting: renewal option",
    "exclusivity": "Consulting: exclusivity",
}
_EMPLOYMENT = ["first_number_deflection", "counter_below_target", "geo_discount",
               "equity_vs_cash", "start_date", "title_level"]
_CONSULTING = ["rate_vs_scope", "payment_terms", "renewal_option", "exclusivity"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an of to in on for with and or is are was were it this that i we you they my our their "
    "at by as be from more than not no can could would should".split()
)
_MARKET_KW = frozenset(
    "market comp corpus median benchmark data observed range percentile salary compensation".split()
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def _is_supported(
    justification: str, proof_points: list, comp_sufficient: bool, competing_present: bool
) -> bool:
    """A justification is supported if it shares real content with a proof point's CLAIM, or (when
    the comp corpus has >=5 observations) invokes the market data, or (when a real competing offer
    exists) invokes it. Otherwise it rests on nothing supplied and is flagged. Overlap is against
    the claim alone, not its evidence: evidence strings ('FY23 budget') add generic tokens that
    would let an ungrounded market assertion sharing one of them read as supported."""
    jt = _tokens(justification)
    if not jt:
        return False
    for p in proof_points:
        if len(jt & _tokens(_pp(p, "claim") or "")) >= 2:
            return True
    low = (justification or "").lower()
    if comp_sufficient and (jt & _MARKET_KW):
        return True
    if competing_present and any(
        k in low for k in ("competing", "competitor", "another offer", "other offer")
    ):
        return True
    return False


def _pp(p: Any, attr: str) -> Any:
    return p.get(attr) if isinstance(p, dict) else getattr(p, attr, None)


@dataclass
class ScriptResult:
    situation_read: str
    walk_away: str
    scripts: list[dict] = field(default_factory=list)  # P11Script fields + "supported": bool
    leverage_inventory: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    unsupported_scenarios: list[str] = field(default_factory=list)


def _offer_summary(offer: Any, track: str) -> str:
    if offer is None:
        return "no offer recorded yet"
    if track == "consulting":
        return (
            f"day rate {getattr(offer, 'day_rate', None)}, "
            f"{getattr(offer, 'days_committed', None)} days over "
            f"{getattr(offer, 'duration_weeks', None)} weeks, "
            f"payment {getattr(offer, 'payment_terms', None)}, "
            f"exclusivity {getattr(offer, 'exclusivity', None)}"
        )
    fy = first_year_comp(offer)
    return (
        f"base ${getattr(offer, 'base', None):,.0f}" if getattr(offer, "base", None) else "base n/a"
    ) + (
        f", bonus ${bonus_amount(offer):,.0f}"
        f", equity {getattr(offer, 'equity_description', None) or 'n/a'}"
        f", level {getattr(offer, 'level_title', None) or 'n/a'}"
        f", location {getattr(offer, 'location_policy', None) or 'n/a'}"
        + (f", est. first-year ${fy:,.0f}" if fy is not None else "")
    )


def _render_user(
    template: str,
    *,
    offer_summary: str,
    walk_away: str,
    proof_points: list,
    comp_summary: str,
    comp_n: int,
    competing: list[dict],
    scenarios: list[str],
) -> str:
    proof_block = "\n".join(
        f"- {_pp(p, 'claim')} ({_pp(p, 'evidence')})" for p in proof_points
    ) or "(none provided)"
    competing_block = (
        "; ".join(
            f"pursuit {c['pursuit_id']} ({c['status']}, "
            + (f"${c['value']:,.0f}" if c.get("value") is not None else "value n/a")
            + ")"
            for c in competing
        )
        or "none"
    )
    scenario_block = "\n".join(f"- {_SCENARIOS[s]}" for s in scenarios)
    text = template.replace("{offer summary: base, bonus, equity, level, location}", offer_summary)
    text = text.replace("{walk_away}", walk_away)
    text = text.replace('{for each: "- {claim} ({evidence})"}', proof_block)
    text = text.replace('{comp_intel_summary or "insufficient data (n<5)"}', comp_summary)
    text = text.replace("{n}", str(comp_n))
    text = text.replace('{competing_offers or "none"}', competing_block)
    text = text.replace(
        "{list of scenarios, e.g. first-number deflection, counter below target, "
        "geo discount, title/level}",
        scenario_block,
    )
    return text


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=None, tier=11, model=model, prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000], error=(error or "")[:4000], attempts=2,
            )
        )

    return hook


async def draft_scripts(
    session: AsyncSession,
    client: Any,
    pursuit_id: int,
    *,
    walk_away: str,
    scenarios: list[str] | None = None,
    proof_points: list | None = None,
    ask: str | None = None,
) -> ScriptResult | None:
    """Draft negotiation scripts for the pursuit's offer. `walk_away` is REQUIRED and supplied by
    the operator; it is passed to the model verbatim and echoed back unchanged. Each returned script
    is grounded-checked and flagged if its justification rests on nothing supplied. `ask` is the
    figure the operator plans to ask for (also theirs); it is told to the model as a plain line of
    the offer summary and never adjusted."""
    pursuit = await session.get(Pursuit, pursuit_id)
    if pursuit is None:
        return None
    track = pursuit.track or "employment"

    benchmark = await benchmark_offer(session, pursuit_id)
    offer = await _load_offer(session, pursuit_id, track)
    if proof_points is None:
        profile = load_profile(track)
        proof_points = list(getattr(profile, "proof_points", None) or []) if profile else []

    competing = benchmark.competing if benchmark else []
    comp_sufficient = bool(benchmark and benchmark.comp.sufficient)
    if scenarios is None:
        scenarios = list(_CONSULTING if track == "consulting" else _EMPLOYMENT)
        if competing:  # only script a competing offer when a real one exists (no bluffs)
            scenarios.append("competing_offer")
    scenarios = [s for s in scenarios if s in _SCENARIOS]
    # The no-bluff guard runs on EVERY path, including an explicit `scenarios` arg: a competing
    # scenario is never sent to the model unless a real competing offer exists (never imply one).
    if not competing:
        scenarios = [s for s in scenarios if s != "competing_offer"]

    spec = load_prompt("negotiate/v1")
    user = _render_user(
        spec.user,
        offer_summary=_offer_summary(offer, track)
        + (f"; the operator plans to ask for {ask.strip()}" if ask and ask.strip() else ""),
        walk_away=walk_away,
        proof_points=proof_points,
        comp_summary=(benchmark.comp.summary() if benchmark else "insufficient data (n<5)"),
        comp_n=(benchmark.comp.n if benchmark else 0),
        competing=competing,
        scenarios=scenarios,
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    result = await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=11,
        on_quarantine=_quarantine_hook(session),
    )
    if not result.ok or result.value is None:
        await session.commit()  # persist any quarantine row the hook staged
        return None

    v = result.value
    scripts: list[dict] = []
    unsupported: list[str] = []
    for sc in v.scripts:
        # Defense in depth: even if the model volunteers a competing-offer script we never asked
        # for, drop it entirely when no real competing offer exists rather than shipping a bluff.
        if sc.scenario == "competing_offer" and not competing:
            continue
        supported = _is_supported(
            sc.justification_basis, proof_points, comp_sufficient, bool(competing)
        )
        scripts.append({**sc.model_dump(), "supported": supported})
        if not supported:
            unsupported.append(sc.scenario)

    return ScriptResult(
        situation_read=v.situation_read,
        walk_away=walk_away,  # echoed back exactly as supplied; never adjusted
        scripts=scripts,
        leverage_inventory=list(v.leverage_inventory),
        risks=list(v.risks),
        unsupported_scenarios=unsupported,
    )


async def _load_offer(session: AsyncSession, pursuit_id: int, track: str):
    from sqlalchemy import select

    from kestrel.db.models import EngagementOffer, Offer

    model = EngagementOffer if track == "consulting" else Offer
    return (
        await session.execute(
            select(model)
            .where(model.pursuit_id == pursuit_id)
            .order_by(model.received_at.desc(), model.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
