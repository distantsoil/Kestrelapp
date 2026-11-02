"""The honest summary (P12, SPEC-MODULES 7). A short generated narrative grounded strictly in the
metrics on the page. It exists to prevent the one failure that makes a self-reporting system worse
than useless: always reporting good news. So the discrimination verdict and the insufficient-data
verdict are computed in CODE from the outcome analysis and returned alongside the model's prose; the
narrative cannot quietly claim the score is working when the numbers say it is not.

Structured-output-or-failure (hard rule 3): the P12 call is schema-validated; a failed call
returns a code-built summary from the metrics rather than fabricating prose."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.analytics.metrics import collect_metrics
from kestrel.db.models import Quarantine
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import load_prompt

_MIN_OUTCOMES = 10  # P12 rule 5: below this there is not yet enough signal to draw conclusions


@dataclass
class AnalyticsSummary:
    track: str
    metrics: dict
    discriminating: bool | None  # code-computed: True / False / None (insufficient)
    insufficient_data: bool
    summary_md: str
    headline_finding: str
    one_thing_to_change: str = ""
    evidence_for_it: str = ""
    sample_size_warnings: list[str] = field(default_factory=list)
    # True when there is enough data for a narrative and the model has not been asked for one
    # yet: the page offers to write it rather than paying for it on every visit (D-UI-19).
    narrative_pending: bool = False
    # True only when the model's own answer passed validation (an empty one_thing_to_change is
    # still a real answer). A code-built fallback is False.
    from_model: bool = False
    # True when the send was refused before it left (confidential mode on, no current employer
    # chosen to redact with): nothing went to the model, which is not "no usable answer".
    held: bool = False
    # Plain-writing marks left after the one re-ask (issue #93); the text is never rewritten.
    style_flags: list[dict] = field(default_factory=list)

    @property
    def not_discriminating(self) -> bool:
        """True only when there is enough evidence AND the score does not separate outcomes. This is
        the flag the page leads with; it is never masked by the model's prose."""
        return self.discriminating is False


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=None, tier=12, model=model, prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000], error=(error or "")[:4000], attempts=2,
            )
        )

    return hook


def _fallback_md(metrics: dict, discriminating: bool | None, insufficient: bool) -> str:
    n = metrics["score_vs_outcome"]["n_outcomes"]
    if insufficient:
        return (
            f"Too few to judge: {n} recorded outcomes ({n} of {_MIN_OUTCOMES} needed) on the "
            f"{metrics['track']} track. "
            "No conclusion can be drawn until more of the pipeline resolves."
        )
    if discriminating is False:
        return (
            "The score is not discriminating: items across score bands are converting at "
            "indistinguishable rates, so the score is not yet doing its job. That is the finding "
            "that matters most on this page."
        )
    return metrics["score_vs_outcome"]["headline"]


async def honest_summary(
    session: AsyncSession, client: Any, track: str, *, period: dict | None = None
) -> AnalyticsSummary:
    """Assemble the metrics, compute the discrimination and insufficient-data verdicts in code, and
    ask P12 for the narrative. The code-computed verdicts are authoritative and returned regardless
    of what the model says; a failed model call falls back to a code-built narrative.

    The model is asked only when there is something for it to say and the caller wants it:
    with fewer than _MIN_OUTCOMES outcomes the code verdict is the whole story (asking anyway
    cost the operator ten seconds and a billed call on an empty install), and with `client`
    None the page shows the verdicts at once and offers the narrative on demand (D-UI-19)."""
    metrics = await collect_metrics(session, track)
    if period:  # the page's date range and its counts, so the narrative names the same period
        metrics["period"] = period
    svo = metrics["score_vs_outcome"]
    discriminating = svo["discriminating"]
    insufficient = svo["n_outcomes"] < _MIN_OUTCOMES
    if insufficient or client is None:
        return AnalyticsSummary(
            track=track, metrics=metrics, discriminating=discriminating,
            insufficient_data=insufficient,
            summary_md=_fallback_md(metrics, discriminating, insufficient),
            headline_finding=_fallback_md(metrics, discriminating, insufficient),
            narrative_pending=not insufficient,
        )

    spec = load_prompt("analytics_summary/v2")
    user = spec.user.replace("{metrics_json}", json.dumps(metrics, default=str))
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]
    # Through the Hosted mode plan (D-PRIV-1): on an online tier the confidential employer is
    # redacted before the send, or the send is refused when nothing is marked.
    from kestrel.pipeline._shared import HOSTED_REFUSAL, run_prompt_hosted

    hook = _quarantine_hook(session)

    async def run(msgs: list[dict], reask: bool = False) -> Any:
        return await run_prompt_hosted(
            session, client, spec, msgs, quarantine_tier=2,
            on_quarantine=None if reask else hook,  # a failed re-ask leaves no quarantine row
        )

    result = await run(messages)

    if not result.ok or result.value is None:
        await session.commit()  # persist any quarantine row
        return AnalyticsSummary(
            track=track, metrics=metrics, discriminating=discriminating,
            insufficient_data=insufficient,
            summary_md=_fallback_md(metrics, discriminating, insufficient),
            headline_finding=_fallback_md(metrics, discriminating, insufficient),
            held=result.error == HOSTED_REFUSAL,
        )

    plain = await enforce(spec, result.value, messages=messages, run=run)
    v = plain.value
    # The CODE verdicts win over the model's prose, and not only as a boolean: when the score is not
    # discriminating (or the sample is too thin), the honest finding LEADS the narrative and becomes
    # the headline, so the page can never read cheerful while the numbers say the score is not
    # working (P12 rule 3: "say so plainly and first").
    lead = _fallback_md(metrics, discriminating, insufficient) if (
        discriminating is False or insufficient
    ) else None
    return AnalyticsSummary(
        track=track,
        metrics=metrics,
        discriminating=discriminating,
        insufficient_data=insufficient or bool(v.insufficient_data),
        summary_md=f"{lead}\n\n{v.summary_md}" if lead else v.summary_md,
        headline_finding=lead or v.headline_finding,
        one_thing_to_change=v.one_thing_to_change,
        evidence_for_it=v.evidence_for_it,
        sample_size_warnings=list(v.sample_size_warnings),
        from_model=True,
        style_flags=plain.flags,
    )
