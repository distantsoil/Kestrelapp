"""Offer comparison matrix (SPEC-MODULES 4.4). When more than one offer is open, lay the trade-offs
out across total first-year comp, four-year value, level and scope, reporting line, location and
travel, organizational risk from research signals, and trajectory fit, each normalized across the
offers and weighted by the operator's OWN adjustable weights.

Presented as ANALYSIS, never a recommendation to accept. This is a life decision; the tool's job is
to make the trade-offs legible, so the result carries scores and raw values but never an
'accept this one' verdict."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kestrel.negotiation.valuation import (
    _VEST_YEARS,
    annualized_engagement,
    first_year_comp,
    four_year_value,
)

QUANT_AXES = ("total_first_year", "four_year_value")
QUAL_AXES = ("level_scope", "reporting_line", "location_travel", "org_risk", "trajectory_fit")
AXES = QUANT_AXES + QUAL_AXES
# org_risk is a RISK rating (higher = worse), so it is inverted before weighting; every other
# qualitative axis is rated by the operator on 0-5 where higher is better.
_LOWER_IS_BETTER = frozenset({"org_risk"})
_QUAL_MAX = 5.0

_ANALYSIS_NOTE = (
    "This is analysis to make the trade-offs legible, not a recommendation to accept. "
    "The weights are yours to change."
)


@dataclass
class OfferComp:
    offer_id: int
    label: str
    first_year: float | None = None
    four_year: float | None = None
    qual: dict[str, float] = field(default_factory=dict)  # QUAL_AXES -> 0..5 (operator rated)
    # What the two quantitative axes actually are, per offer. The matrix mixes employment offers
    # (annual cash comp) and consulting offers (an annualized engagement, D-RR1-1), so a reader
    # must be told which basis each figure is on rather than inferring it from a column header.
    # "employment" = annual cash comp; "consulting_annualized" = engagement value scaled by
    # 52 / duration_weeks, NOT guaranteed; "unknown" = not recorded.
    basis: str = "unknown"

    def raw(self, axis: str) -> float | None:
        if axis == "total_first_year":
            return self.first_year
        if axis == "four_year_value":
            return self.four_year
        v = self.qual.get(axis)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def basis_label(self) -> str:
        if self.basis == "employment":
            return "annual cash comp"
        if self.basis == "consulting_annualized":
            return "annualized engagement (not guaranteed)"
        return "not recorded"


@dataclass
class ComparisonMatrix:
    axes: list[str]
    weights: dict[str, float]
    rows: list[dict]
    note: str = _ANALYSIS_NOTE


def _default_weights() -> dict[str, float]:
    return {a: 1.0 / len(AXES) for a in AXES}


def _normalized_weights(weights: dict[str, float] | None) -> dict[str, float]:
    w = _default_weights()
    if weights:
        for k, v in weights.items():
            if k in w and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                w[k] = float(v)
    total = sum(w.values())
    return {k: (v / total if total > 0 else 0.0) for k, v in w.items()}


def _axis_normalizer(axis: str, values: list[float]):
    """Return a function mapping a raw value to [0,1] for this axis, higher = better. Quantitative
    axes are scaled to the max across the offers; qualitative axes to 0-5, with risk inverted."""
    if axis in QUANT_AXES:
        hi = max(values) if values else 0.0
        return lambda x: (x / hi) if hi > 0 else 0.0
    if axis in _LOWER_IS_BETTER:
        return lambda x: max(0.0, min(1.0, (_QUAL_MAX - x) / _QUAL_MAX))
    return lambda x: max(0.0, min(1.0, x / _QUAL_MAX))


def compare_offers(
    offers: list[OfferComp], weights: dict[str, float] | None = None
) -> ComparisonMatrix:
    """Build the weighted comparison. Each axis is normalized across the offers; each offer's
    weighted total averages its present axes (weights renormalized over what it actually has), so a
    missing figure lowers no score unfairly. Never returns a recommendation, only the numbers."""
    w = _normalized_weights(weights)
    normalizers = {
        axis: _axis_normalizer(
            axis, [o.raw(axis) for o in offers if o.raw(axis) is not None]
        )
        for axis in AXES
    }

    rows: list[dict] = []
    for o in offers:
        raw: dict[str, float | None] = {}
        norm: dict[str, float | None] = {}
        wsum = 0.0
        wscore = 0.0
        for axis in AXES:
            val = o.raw(axis)
            raw[axis] = val
            if val is None:
                norm[axis] = None
                continue
            n = normalizers[axis](val)
            norm[axis] = round(n, 4)
            wsum += w[axis]
            wscore += w[axis] * n
        rows.append(
            {
                "offer_id": o.offer_id,
                "label": o.label,
                # D-RR1-1: the matrix mixes annual cash comp and an annualized engagement, so the
                # basis travels with the row and the template labels it rather than letting a
                # reader assume every figure is a salary.
                "basis": o.basis,
                "basis_label": o.basis_label(),
                "raw": raw,
                "normalized": norm,
                "weighted_total": round(wscore / wsum, 4) if wsum > 0 else None,
            }
        )
    return ComparisonMatrix(axes=list(AXES), weights=w, rows=rows)


def offer_comp_from(
    offer: Any, *, offer_id: int, label: str, qual: dict | None = None, track: str = "employment"
) -> OfferComp:
    """Build an OfferComp from an offer row: quantitative axes from valuation, qualitative axes from
    the operator's supplied ratings (level/scope, reporting line, location/travel, risk, trajectory
    are judgments the tool does not make for the operator).

    A consulting `EngagementOffer` has no `base`, so `first_year_comp` and `four_year_value` both
    returned None and the row was ranked on qualitative axes alone -- against annual-salary rows,
    on no quantitative scale at all. D-RR1-1: its engagement value is scaled to an annual figure
    (`52 / duration_weeks`) and the basis is recorded on the comp so the matrix can LABEL it, rather
    than filing a total engagement value under a first-year heading and silently mixing units.

    Without `duration_weeks` there is no defensible annualization (a 6-week engagement would
    annualize to a large, misleading number), so the consulting row stays unvalued and the basis
    says why."""
    qual_clean = {k: v for k, v in (qual or {}).items() if k in QUAL_AXES}
    if track == "consulting":
        annualized = annualized_engagement(offer)
        return OfferComp(
            offer_id=offer_id,
            label=label,
            first_year=annualized,
            # Four years of an engagement is not a promise either, but it is the same arithmetic
            # and the same caveat, so it is scaled the same way.
            four_year=(annualized * _VEST_YEARS) if annualized is not None else None,
            qual=qual_clean,
            basis="consulting_annualized" if annualized is not None else "unknown",
        )
    return OfferComp(
        offer_id=offer_id,
        label=label,
        first_year=first_year_comp(offer),
        four_year=four_year_value(offer),
        qual=qual_clean,
        basis="employment" if first_year_comp(offer) is not None else "unknown",
    )
