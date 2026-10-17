"""Pure valuation of an offer, shared by benchmarking and the comparison matrix. All figures are
best-effort from the fields the operator actually recorded: a missing component contributes zero
rather than making the whole figure None, but if there is no base at all the first-year figure is
None (there is nothing to value). Equity is assumed to vest evenly over four years unless the offer
says otherwise, which is the standard grant and is stated in the private decision log."""

from __future__ import annotations

from typing import Any

_VEST_YEARS = 4


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def bonus_amount(offer: Any) -> float:
    base = _num(getattr(offer, "base", None)) or 0.0
    pct = _num(getattr(offer, "bonus_target_pct", None)) or 0.0
    return base * pct / 100.0


def first_year_comp(offer: Any) -> float | None:
    """Base + target bonus + signing + one year of equity vest. None when there is no base to
    anchor on (an offer with no base number is not yet a comparable figure)."""
    base = _num(getattr(offer, "base", None))
    if base is None:
        return None
    signing = _num(getattr(offer, "signing", None)) or 0.0
    equity = _num(getattr(offer, "equity_value_estimate", None)) or 0.0
    return base + bonus_amount(offer) + signing + equity / _VEST_YEARS


def four_year_value(offer: Any) -> float | None:
    """Four years of base + bonus, the full equity grant, and the one-time signing bonus."""
    base = _num(getattr(offer, "base", None))
    if base is None:
        return None
    signing = _num(getattr(offer, "signing", None)) or 0.0
    equity = _num(getattr(offer, "equity_value_estimate", None)) or 0.0
    return (base + bonus_amount(offer)) * _VEST_YEARS + equity + signing


def engagement_value(engagement: Any) -> float | None:
    """Consulting engagement value: day rate times days committed. None without a day rate."""
    rate = _num(getattr(engagement, "day_rate", None))
    days = _num(getattr(engagement, "days_committed", None))
    if rate is None or days is None:
        return None
    return rate * days
