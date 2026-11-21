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
# The allowlist of currency marks (round 4). A bare "$" is USD only for a US posting, which
# normalize decides from the location (`Comp.bare_dollar`).
_CURRENCY = {"$": "USD", "US$": "USD", "USD$": "USD", "£": "GBP", "€": "EUR", "CA$": "CAD",
             "C$": "CAD", "CAD$": "CAD", "A$": "AUD", "AU$": "AUD", "AUD$": "AUD", "NZ$": "NZD",
             "NZD$": "NZD", "S$": "SGD", "SG$": "SGD", "SGD$": "SGD", "HK$": "HKD", "HKD$": "HKD",
             "R$": "BRL", "MX$": "MXN", "MXN$": "MXN"}
# A three-letter code written with spaces, tabs or no-break spaces (never a line break: a code
# ending one line is not the currency of a figure starting the next, round 7) before its dollar
# sign ("CAD $100k"): only the codes the allowlist spells as CODE$, so an ordinary word before a
# "$" ("pay $90k") stays a bare "$".
_SPACED_CODES = sorted(k[:-1] for k in _CURRENCY if k.endswith("$") and len(k) == 4)
# A currency mark: a pound or euro sign, or a dollar sign with an optional country prefix
# (CA$, C$, A$, AU$, NZ$, US$), so a Canadian or Australian dollar is never read as USD (#208).
# Any letter prefix is captured, then looked up in the allowlist below: an unlisted prefix is a
# currency nobody verified, never USD (round 4).
# Any other capitalized three-letter code before a spaced "$" ("BRL $5k") is captured too and,
# being unlisted, is a currency nobody verified: never a bare "$" and never USD.
_SYM = (r"(?:(?<![A-Za-z])(?:" + "|".join(_SPACED_CODES) + r"|(?-i:[A-Z]{3}))[ \t\u00a0]+\$"
        r"|(?<![A-Za-z])[A-Za-z]{1,3}\$|\$|£|€)")
_RANGE_RE = re.compile(
    r"(?P<c1>" + _SYM + r")?\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>[kK])?\s*(?:-|–|—|to)\s*"
    r"(?P<c2>" + _SYM + r")?\s*(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<k2>[kK])?" + _UNIT,
    re.IGNORECASE,
)
# A single figure MUST carry a currency symbol to count (otherwise any lone number would match).
_SINGLE_RE = re.compile(
    r"(?P<c1>" + _SYM + r")\s*(?P<n1>\d[\d,]*(?:\.\d+)?)\s*(?P<k1>[kK])?" + _UNIT, re.IGNORECASE
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
    # False when no currency was stated and USD was assumed ("80k-100k"; a board that sends no
    # currency), an unlisted mark, or a band whose two ends name different currencies: the
    # blocker check then never compares it with a USD floor (#208).
    currency_known: bool = True
    # The mark was a bare "$": USD only when the posting is in the US (normalize decides).
    bare_dollar: bool = False
    # False when no interval was stated and a year was assumed.
    interval_known: bool = True


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _interval(unit: str | None) -> str:
    return _INTERVAL_MAP.get((unit or "").lower(), "year") if unit else "year"


def _plausible(lo: float, hi: float, interval: str) -> bool:
    low, high = _PLAUSIBLE.get(interval, (0.0, 1e12))
    return lo >= low and hi <= high and hi >= lo


def _make(lo: float, hi: float, symbol: str | None, unit: str | None,
          other: str | None = None) -> Comp | None:
    interval = _interval(unit)
    lo, hi = min(lo, hi), max(lo, hi)
    if not _plausible(lo, hi, interval):
        return None
    first, second = (re.sub(r"\s+", "", m).upper() if m else None for m in (symbol, other))
    if first and first != "$" and first.endswith("$") and first in _CURRENCY and second == "$":
        # "CAD $100k - $120k": the code on the first figure names the band's dollar. A bare "$"
        # first and a code second ("$80k - C$100k") stays a mixed band.
        second = first
    marks = [m for m in (first, second) if m]
    codes = {_CURRENCY.get(m) for m in marks}
    known = bool(marks) and None not in codes and len(codes) == 1
    code = (codes.pop() or "USD") if known else "USD"
    return Comp(lo, hi, code, interval, currency_known=known,
                bare_dollar=known and all(m == "$" for m in marks), interval_known=bool(unit))


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
            m.group("c1"),
            m.group("unit"),
            m.group("c2"),
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
