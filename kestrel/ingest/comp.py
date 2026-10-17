"""Compensation parsing (SPEC.md 5.4). Prefer structured fields; otherwise regex the description
for the documented formats ($180,000 - $220,000, 180k-220k, $95/hr, $1,200/day, $200,000).

Strictness matters: descriptions are full of number pairs ("3 to 5 years", "manage 5 to 10 direct
reports") that must NOT be read as pay. A match therefore REQUIRES a monetary signal (a currency
symbol, a `k`/`K` suffix, or an explicit rate like /hr or per year) AND must clear a plausibility
range for its interval. A false hit would poison the comp-intelligence corpus (SPEC-MODULES 6.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HOURS_PER_YEAR = 2080
_DAYS_PER_YEAR = 260
_WEEKS_PER_YEAR = 52
_MONTHS_PER_YEAR = 12

_UNIT = r"(?:\s*(?P<sep>/|per)\s*(?P<unit>hour|hr|day|week|month|year|yr|annum|annually))?"
_RANGE_RE = re.compile(
    r"(?P<c1>[$£€])?\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>[kK])?\s*(?:-|–|—|to)\s*"
    r"(?P<c2>[$£€])?\s*(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<k2>[kK])?" + _UNIT,
    re.IGNORECASE,
)
# A single figure MUST carry a currency symbol to count (otherwise any lone number would match).
_SINGLE_RE = re.compile(
    r"(?P<c1>[$£€])\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>[kK])?" + _UNIT, re.IGNORECASE
)

_INTERVAL_MAP = {
    "hour": "hour",
    "hr": "hour",
    "day": "day",
    "week": "week",
    "month": "month",
    "year": "year",
    "yr": "year",
    "annum": "year",
    "annually": "year",
}
_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}
# Plausibility bounds per interval; a match outside these is not compensation.
_PLAUSIBLE = {
    "year": (10_000, 5_000_000),
    "month": (500, 500_000),
    "week": (200, 100_000),
    "day": (50, 50_000),
    "hour": (5, 2_000),
}


@dataclass
class Comp:
    min: float | None
    max: float | None
    currency: str
    interval: str


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _interval(unit: str | None) -> str:
    return _INTERVAL_MAP.get((unit or "").lower(), "year") if unit else "year"


def _plausible(lo: float, hi: float, interval: str) -> bool:
    low, high = _PLAUSIBLE.get(interval, (0.0, 1e12))
    return lo >= low and hi <= high and hi >= lo


def _make(lo: float, hi: float, symbol: str | None, unit: str | None) -> Comp | None:
    interval = _interval(unit)
    lo, hi = min(lo, hi), max(lo, hi)
    if not _plausible(lo, hi, interval):
        return None
    return Comp(lo, hi, _CURRENCY.get(symbol or "$", "USD"), interval)


def parse_comp(text: str | None) -> Comp | None:
    if not text:
        return None
    text = re.sub(r"\\([-.$,])", r"\1", text)  # undo markdown escapes ("\-", "\.") from HTML
    for m in _RANGE_RE.finditer(text):
        if not (
            m.group("c1") or m.group("c2") or m.group("k1") or m.group("k2") or m.group("unit")
        ):
            continue  # a bare number pair (e.g. "3 to 5 years") is not compensation
        comp = _make(
            _amount(m.group("n1"), m.group("k1")),
            _amount(m.group("n2"), m.group("k2")),
            m.group("c1") or m.group("c2"),
            m.group("unit"),
        )
        if comp:
            return comp
    for m in _SINGLE_RE.finditer(text):  # single figure requires a currency symbol
        v = _amount(m.group("n1"), m.group("k1"))
        comp = _make(v, v, m.group("c1"), m.group("unit"))
        if comp:
            return comp
    return None


def to_annual(value: float | None, interval: str | None) -> float | None:
    """Convert a rate to an annual figure (SPEC.md 5.4: hourly at 2080h). Returns None unchanged."""
    if value is None:
        return None
    match interval:
        case "hour":
            return value * _HOURS_PER_YEAR
        case "day":
            return value * _DAYS_PER_YEAR
        case "week":
            return value * _WEEKS_PER_YEAR
        case "month":
            return value * _MONTHS_PER_YEAR
        case _:
            return value
