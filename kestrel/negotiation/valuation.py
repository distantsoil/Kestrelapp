"""Pure valuation of an offer, shared by benchmarking and the comparison matrix. All figures are
best-effort from the fields the operator actually recorded: a missing component contributes zero
rather than making the whole figure None, but if there is no base at all the first-year figure is
None (there is nothing to value). Equity is assumed to vest evenly over four years unless the offer
says otherwise, which is the standard grant and is stated in the private decision log."""

from __future__ import annotations

from typing import Any

_VEST_YEARS = 4
_WEEKS_PER_YEAR = 52


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


def annualized_engagement(engagement: Any) -> float | None:
    """The engagement value scaled to an annual figure, for the comparison matrix (D-RR1-1).

    An employment offer's `total_first_year` is annual cash comp, so a consulting offer has to be
    on an annual scale too or it cannot share a ranking column. This is an ARITHMETIC
    ANNUALIZATION of the agreed scope, not a forecast: a 6-week engagement at $50k scales to about
    $433k, which is why `OfferComp.basis` records `consulting_annualized` and the matrix labels the
    column "annualized engagement (not guaranteed)" wherever it renders.

    Requires `duration_weeks`. Without it there is no defensible denominator -- guessing from
    `days_committed` would invent a 5-day week and inflate short engagements further -- so the
    figure is None and the row stays unvalued rather than being given a number that reads as a
    salary."""
    total = engagement_value(engagement)
    if total is None:
        return None
    weeks = _num(getattr(engagement, "duration_weeks", None))
    if weeks is None or weeks <= 0:
        return None
    return total * _WEEKS_PER_YEAR / weeks
