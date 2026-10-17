"""Post-processing of a Tier 2 evaluation (SPEC.md 7.2/7.3, PROMPTS.md P2/P2C post-processing).

The model returns dimension scores, a holistic score, its own verdict, blockers, and a legitimacy
object. The system, never the model, then: computes the weighted score, flags divergence for
review, applies the override rules (any blocker or a `reject` legitimacy forces `pass`; `suspect`
caps the displayed score at 3.5), and derives the authoritative verdict from the score thresholds
while preserving the model's own verdict for context. Nothing here calls a model or invents data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kestrel.config import Settings

# Employment uses apply/consider/pass; consulting uses pursue/consider/pass (SPEC.md 6.3/7.3).
_TOP_VERDICT = {"employment": "apply", "consulting": "pursue"}
_SUSPECT_CAP = 3.5


@dataclass
class PostResult:
    weighted: float
    holistic: float
    displayed_score: float
    needs_review: bool
    verdict: str  # threshold-derived, authoritative for filtering
    verdict_model: str  # the model's own verdict, for context
    forced_by: str | None
    blockers: list[dict] = field(default_factory=list)
    legitimacy_status: str = "clean"
    verdict_disagreement: bool = False


def weights_for(track: str, settings: Settings) -> dict[str, float]:
    block = getattr(settings.scoring.weights, track)
    return block.model_dump()


def weighted_score(dimension_scores: dict[str, float], weights: dict[str, float]) -> float:
    """Σ(dimension.score × weight). Only dimensions with a weight contribute; a missing dimension
    contributes nothing rather than raising."""
    return round(sum(score * weights.get(key, 0.0) for key, score in dimension_scores.items()), 4)


def verdict_from_score(score: float, track: str, settings: Settings) -> str:
    if score >= settings.thresholds.apply:
        return _TOP_VERDICT[track]
    if score >= settings.thresholds.consider:
        return "consider"
    return "pass"


def post_process(
    *,
    track: str,
    dimension_scores: dict[str, float],
    holistic: float,
    model_verdict: str,
    blockers: list[dict],
    legitimacy_status: str,
    work_auth_signal: str | None,
    requires_sponsorship: bool,
    settings: Settings,
) -> PostResult:
    """Apply SPEC.md 7.3 overrides. `blockers` is the model's list of {kind, detail} dicts; a
    work-authorization blocker is added when the posting explicitly refuses sponsorship the profile
    needs. The returned verdict is authoritative; verdict_model is kept for the disagreement log."""
    weights = weights_for(track, settings)
    weighted = weighted_score(dimension_scores, weights)
    needs_review = abs(holistic - weighted) > settings.scoring.divergence_flag

    effective_blockers = list(blockers)
    if work_auth_signal == "explicit_no_sponsorship" and requires_sponsorship:
        effective_blockers.append(
            {
                "kind": "work_authorization",
                "detail": "Posting states no sponsorship; profile requires it",
            }
        )

    # Legitimacy "suspect" caps the DISPLAYED score before the verdict is derived from it.
    displayed = min(holistic, _SUSPECT_CAP) if legitimacy_status == "suspect" else holistic
    verdict = verdict_from_score(displayed, track, settings)

    reasons: list[str] = []
    if effective_blockers:
        verdict = "pass"
        kinds = ", ".join(b.get("kind", "other") for b in effective_blockers)
        reasons.append(f"blocker:{kinds}")
    if legitimacy_status == "reject":
        verdict = "pass"
        reasons.append("legitimacy_reject")
    if legitimacy_status == "suspect":
        reasons.append("legitimacy_suspect_cap")

    return PostResult(
        weighted=weighted,
        holistic=holistic,
        displayed_score=displayed,
        needs_review=needs_review,
        verdict=verdict,
        verdict_model=model_verdict,
        forced_by="; ".join(reasons) if reasons else None,
        blockers=effective_blockers,
        legitimacy_status=legitimacy_status,
        verdict_disagreement=(verdict != model_verdict),
    )
