"""What the Offers screen shows, worked out from plain values (D-UI-122, D-UI-136).

Everything here is pure: no database, no model, no request. `kestrel.web.pages.offers` loads the
rows and hands them over; this module decides the words, the bar widths and the "Ahead on" lines,
so each rule can be tested without a page. The rules that matter:

  - Money rows show bars with the best highlighted. Judgment rows are the operator's own star
    ratings beside the source fact; nothing here rates anything for them.
  - "Ahead on ..." counts only rows the operator weights at Matters or higher, and only where an
    offer is the single best. There is no winner and no total (spec 4.4).
  - A similar-roles range needs five observations (hard rule 6). Under five the range is hidden and
    the count is said in words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from kestrel import ui_settings as ui_settings
from kestrel.date_display import format_display
from kestrel.negotiation.valuation import (
    bonus_amount,
    engagement_value,
    first_year_comp,
    four_year_value,
)
from kestrel.services.offer_outcome import OPEN_STATUSES, OUTCOMES  # noqa: F401
from kestrel.services.pursuit_board import parse_day

MIN_OBSERVATIONS = 5
SOON_DAYS = 7  # the deadline pill turns amber at this many days or fewer
RECENT_CLOSED_DAYS = 14  # a closed offer stays on Compare this long, dimmed, with Reopen
WEIGHT_WORDS = ("Ignore", "A little", "Some", "Matters", "A lot", "Most")
DEFAULT_WEIGHT = 3
LEAD_WEIGHT = 3  # "Ahead on" counts rows weighted at least this much
NOT_GIVEN = "Not given"

# The stars the operator can set, per track. Each is a key in the offer's qual_json.
RATING_KEYS = {
    "employment": ("level_scope", "reporting_line", "location_travel", "org_risk"),
    "consulting": ("payment_terms", "expenses", "renewal_terms", "exclusivity"),
}
# (key, label, what it is, kind). kind: money | rating | score. The employment keys are the
# comparison axes of kestrel.negotiation.compare, so saved weights and ratings keep their meaning.
MATRIX_ROWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "employment": (
        ("total_first_year", "First-year total", "Base, bonus, signing, equity", "money"),
        ("four_year_value", "Four-year value", "No raises assumed", "money"),
        ("level_scope", "Level and scope", "Your rating", "rating"),
        ("reporting_line", "Reporting line", "Your rating", "rating"),
        ("location_travel", "Location and travel", "Your rating", "rating"),
        ("org_risk", "Company risk", "From research, your rating", "rating"),
        ("trajectory_fit", "Fit with your direction", "From the evaluation", "score"),
    ),
    "consulting": (
        ("engagement_value", "Engagement value", "Rate × days", "money"),
        ("day_rate", "Day rate", "Per day", "money"),
        ("payment_terms", "Payment terms", "Your rating", "rating"),
        ("expenses", "Expenses", "Your rating", "rating"),
        ("renewal_terms", "Renewal option", "Your rating", "rating"),
        ("exclusivity", "Exclusivity", "Your rating", "rating"),
        ("trajectory_fit", "Fit with your work", "From the evaluation", "score"),
    ),
}
WEIGHT_KEYS = tuple(
    dict.fromkeys(k for rows in MATRIX_ROWS.values() for k, *_ in rows)
)

# (key, name, hint). The scenario keys are those of kestrel.negotiation.scripts.
SITUATIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "employment": (
        ("counter_below_target", "Counter below target", "Ask for a specific number, with reasons"),
        ("competing_offer", "Use a competing offer", "State it plainly, no bluffing"),
        ("equity_vs_cash", "Equity or cash", "Trade one for the other"),
        ("start_date", "Start date and notice", "Move the start date"),
        ("title_level", "Title and level", "Often worth more than base"),
        ("geo_discount", "Location discount", "Push back on a lower local rate"),
        ("first_number_deflection", "The first-number question", "When they ask what you want"),
    ),
    "consulting": (
        ("rate_vs_scope", "Rate versus scope", "Hold the rate, adjust the scope"),
        ("payment_terms", "Payment terms", "Ask for shorter terms"),
        ("renewal_option", "Renewal option", "Ask for a support option"),
        ("exclusivity", "Exclusivity", "Narrow or remove it"),
    ),
}

OUTCOME_WORDS = {"accepted": "Accepted", "declined": "Declined", "expired": "Expired"}


# --------------------------------------------------------------------------------- formatting
def money(v: float | None) -> str:
    """Whole dollars: 185000 -> "$185,000". The one spelled-out form."""
    return "" if v is None else f"${round(v):,}"


def money_k(v: float | None) -> str:
    """The short form used on cards and bars: 136000 -> "$136k", 650 -> "$650"."""
    if v is None:
        return ""
    return f"${round(v / 1000):,}k" if abs(v) >= 1000 else f"${round(v):,}"


def day_rate(v: float | None) -> str:
    return "" if v is None else f"${round(v):,} a day"


def initials(name: str | None) -> str:
    words = [w for w in (name or "").split() if w]
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def mono_index(name: str | None) -> int:
    """Which of the six token colors a company's mark uses; stable for a given name."""
    return sum(ord(c) for c in (name or "")) % 6


def day_text(raw: str | None, fmt: str) -> str:
    """An ISO date in the operator's chosen format (Region); anything else comes back as typed."""
    d = parse_day(raw)
    if d is None:
        return (raw or "").strip()
    return format_display(d, "region", date_format=fmt)


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


# ----------------------------------------------------------------------------------- deadline
def _short_day(d: date, *, today: date, fmt: str) -> str:
    """"Nov 3" (the year only when it is not this one), unless the operator chose a date format
    in Region, which then wins everywhere."""
    style = "short" if d.year == today.year else "short_year"
    return format_display(d, style, date_format=fmt)


def deadline_view(raw: str | None, *, today: date, fmt: str) -> dict[str, Any] | None:
    """The deadline pill: "Decide by Nov 3 · 9 days", amber at seven days or fewer."""
    d = parse_day(raw)
    if d is None:
        return None
    days = (d - today).days
    when = _short_day(d, today=today, fmt=fmt)
    if days < 0:
        n = -days
        text = f"Decide by {when} · passed {n} {plural(n, 'day')} ago"
    elif days == 0:
        text = f"Decide by {when} · today"
    else:
        text = f"Decide by {when} · {days} {plural(days, 'day')}"
    return {"text": text, "days": days, "soon": days <= SOON_DAYS}


# ------------------------------------------------------------------------------------ pay band
def _pct(v: float, lo: float, hi: float) -> float:
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100)) if hi > lo else 50.0


def band_view(
    *, value: float | None, floor: float | None, target: float | None,
    similar: tuple[float, float] | None, similar_n: int, unit: str, noun: str,
) -> dict[str, Any] | None:
    """The Role page pay band: the operator's floor and target, the similar-roles bracket when
    there are five or more, and a marker at the offer. The scale fits what is drawn, so a day rate
    and a salary each fill the bar. None when there is nothing to draw against."""
    if floor is None and target is None and similar is None:
        return None
    pts = [p for p in (value, floor, target, *(similar or ())) if p is not None]
    lo, hi = min(pts), max(pts)
    pad = (hi - lo) * 0.18 or hi * 0.1 or 1.0
    lo, hi = max(0.0, lo - pad), hi + pad
    fmt = money_k
    out: dict[str, Any] = {
        "marker": None if value is None else round(_pct(value, lo, hi), 1),
        "marker_title": "" if value is None else f"{unit} {fmt(value)}",
        "ticks": [],
        "similar": None,
    }
    for label, v in (("Floor", floor), ("Target", target)):
        if v is not None:
            out["ticks"].append({"label": f"{label} {fmt(v)}", "at": round(_pct(v, lo, hi), 1)})
    if len(out["ticks"]) == 2:  # keep two nearby labels from sitting on each other
        a, b = out["ticks"]
        if abs(a["at"] - b["at"]) < 22:
            lft, rgt = (a, b) if a["at"] <= b["at"] else (b, a)
            lft["align"], rgt["align"] = "end", "start"
    if similar is not None:
        a, b = _pct(similar[0], lo, hi), _pct(similar[1], lo, hi)
        out["similar"] = {
            "left": round(a, 1), "width": round(max(b - a, 1.0), 1),
            "mid": round((a + b) / 2, 1), "label": f"Similar {noun} · {similar_n}",
        }
    return out


def bench_lines(
    *, track: str, value: float | None, floor: float | None, target: float | None,
    similar: tuple[float, float] | None, similar_n: int, posted: tuple[float | None, float | None]
    | None, inside_posted: bool | None,
) -> dict[str, Any]:
    """The words under the band. Under five similar roles the range is not shown and the count is
    said (hard rule 6); the corpus's own sentence stays behind Show details."""
    con = track == "consulting"
    what = "Day rate" if con else "Base"
    noun, nouns = ("work", "engagements") if con else ("roles", "roles")
    first = ""
    if value is not None:
        if target is not None and value >= target:
            first = f"{what} is <b>above your target</b>"
        elif floor is not None and value >= floor:
            first = f"{what} is <b>between your floor and target</b>" if target is not None \
                else f"{what} is <b>above your floor</b>"
        elif floor is not None:
            first = f"{what} is <b>below your floor</b>"
    posted_text = posted_exact = ""
    if posted and any(p is not None for p in posted):
        lo, hi = posted
        posted_exact = (
            f"Posted range {money(lo)} to {money(hi)}" if lo is not None and hi is not None
            else f"Posted range {money(lo if lo is not None else hi)}"
        )
        # One short clause on the card ("inside the posted range"); the figures sit behind
        # Show details so the sentence stays on one line.
        posted_text = (
            f"{'inside' if inside_posted else 'outside'} the posted range"
            if inside_posted is not None else posted_exact
        )
    sim = None
    if similar is not None:
        spans = (
            f"{day_rate(similar[0]).replace(' a day', '')} to {day_rate(similar[1])}" if con
            else f"{money_k(similar[0])} to {money_k(similar[1])}"
        )
        sim = f"Similar {noun} pay {spans} ({similar_n} {nouns})"
    return {
        "first": first, "posted": posted_text, "posted_exact": posted_exact, "similar": sim,
        "too_few": None if similar is not None else
        f"Too few similar {nouns} ({similar_n})",
        "too_few_note": None if similar is not None else
        f"Under {MIN_OBSERVATIONS}, so Kestrel shows no range.",
    }


# ------------------------------------------------------------------------------------- values
def num(v: Any) -> float | None:
    return None if isinstance(v, bool) or not isinstance(v, (int, float)) else float(v)


def stack_parts(offer: Any) -> tuple[float | None, list[dict[str, Any]]]:
    """The first-year total and its stacked bar: base, bonus, signing and a fourth of equity."""
    total = first_year_comp(offer)
    if not total:
        return total, []
    base = num(getattr(offer, "base", None)) or 0.0
    eq = (num(getattr(offer, "equity_value_estimate", None)) or 0.0) / 4
    signing = num(getattr(offer, "signing", None)) or 0.0
    pct = num(getattr(offer, "bonus_target_pct", None))
    parts = [
        {"cls": "k1", "amount": base, "label": f"Base {money_k(base)}"},
        {"cls": "k2", "amount": bonus_amount(offer),
         "label": f"Bonus {pct:g}%" if pct else "Bonus none"},
    ]
    if signing:
        parts.append({"cls": "k3", "amount": signing, "label": f"Signing {money_k(signing)}"})
    if eq:
        parts.append({"cls": "k4", "amount": eq, "label": f"Equity {money_k(eq)}/yr"})
    for p in parts:
        p["pct"] = round(p["amount"] / total * 100, 2)
    return total, parts


def card_facts(offer: Any, track: str, terms: dict[str, str | None], fmt: str) -> list[tuple]:
    def put(v: Any) -> str:
        return str(v).strip() if v not in (None, "") else NOT_GIVEN

    if track == "consulting":
        weeks = getattr(offer, "duration_weeks", None)
        return [
            ("Length", f"{weeks} {plural(weeks, 'week')}" if weeks is not None else NOT_GIVEN),
            ("Payment", put(terms.get("payment_terms"))),
            ("Expenses", put(terms.get("expenses"))),
            ("Renewal", put(terms.get("renewal_terms"))),
            ("Exclusivity", put(terms.get("exclusivity"))),
            ("Your work", put(terms.get("ip_terms"))),
        ]
    pto = getattr(offer, "pto_days", None)
    start = getattr(offer, "start_date", None)
    return [
        ("Level", put(terms.get("level_title"))),
        ("Reports to", put(terms.get("reports_to"))),
        ("Where", put(terms.get("location_policy"))),
        ("Time off", f"{pto} days" if pto is not None else NOT_GIVEN),
        ("Starts", day_text(start, fmt) if start else NOT_GIVEN),
        ("Equity", put(terms.get("equity_description"))),
    ]


def card_total(offer: Any, track: str) -> dict[str, Any]:
    """The big number on a card and the sentence beside it."""
    if track == "consulting":
        value = engagement_value(offer)
        rate, days = num(getattr(offer, "day_rate", None)), getattr(offer, "days_committed", None)
        note = (
            f"engagement value · {day_rate(rate)} × {days} {plural(days or 0, 'day')}"
            if value is not None else "engagement value · needs a day rate and days"
        )
        return {"text": money(value) if value is not None else "Not enough to value", "note": note,
                "parts": [], "value": value}
    total, parts = stack_parts(offer)
    return {"text": money_k(total) if total is not None else "No base given",
            "note": "first year, all in" if total is not None else "add a base to value this offer",
            "parts": parts, "value": total}


def row_value(card: dict[str, Any], key: str) -> dict[str, Any]:
    """One cell of the matrix for one offer: a number to rank, the words to show, and (for a
    judgment row) the star rating and the fact beside it."""
    offer = card["offer"]
    track = card["track"]
    qual = offer.qual_json or {}
    out: dict[str, Any] = {"n": None, "text": NOT_GIVEN, "rating": None, "fact": None}
    if key in ("total_first_year", "engagement_value"):
        v = card["total"]["value"]
        if v is not None:
            out.update(n=v, text=money_k(v) if key == "total_first_year" else money(v))
    elif key == "four_year_value":
        v = four_year_value(offer)
        if v is not None:
            out.update(n=v, text=money_k(v))
    elif key == "day_rate":
        v = num(getattr(offer, "day_rate", None))
        if v is not None:
            out.update(n=v, text=day_rate(v))
    elif key == "trajectory_fit":
        v = card.get("fit")
        if v is not None:
            out.update(n=v, text=f"{v:.1f} of 5")
        else:
            out["text"] = "Not scored yet"
    else:
        fact = {
            "level_scope": card["terms"].get("level_title"),
            "reporting_line": card["terms"].get("reports_to"),
            "location_travel": card["terms"].get("location_policy"),
            "org_risk": card.get("risk_fact"),
            "payment_terms": card["terms"].get("payment_terms"),
            "expenses": card["terms"].get("expenses"),
            "renewal_terms": card["terms"].get("renewal_terms"),
            "exclusivity": card["terms"].get("exclusivity"),
        }.get(key)
        r = num(qual.get(key))
        out.update(
            n=r, rating=None if r is None else max(0, min(5, round(r))),
            fact=str(fact).strip() if fact else NOT_GIVEN, text="",
        )
        if track == "employment" and key == "org_risk" and not fact:
            out["fact"] = "No research yet"
    return out


@dataclass
class Matrix:
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    summary: list[dict[str, Any]]


def weight_of(weights: dict[str, float], key: str) -> int:
    v = weights.get(key)
    return DEFAULT_WEIGHT if v is None else max(0, min(5, round(v)))


def build_matrix(cards: list[dict[str, Any]], track: str, weights: dict[str, float]) -> Matrix:
    """The comparison of open offers. `cards` are the open ones only."""
    rows: list[dict[str, Any]] = []
    for key, label, sub, kind in MATRIX_ROWS[track]:
        vals = [row_value(c, key) for c in cards]
        ns = [v["n"] for v in vals if v["n"] is not None]
        mx = max(ns) if ns else None
        top = mx is not None and sum(1 for n in ns if n == mx) == 1
        w = weight_of(weights, key)
        cells = []
        for c, v in zip(cards, vals, strict=True):
            is_top = top and v["n"] == mx
            pct = 0.0
            if kind == "money" and v["n"] is not None and mx:
                pct = round(v["n"] / mx * 100, 1)
            elif kind == "score" and v["n"] is not None:
                pct = round(v["n"] / 5 * 100, 1)
            cells.append({**v, "pct": pct, "top": is_top, "offer_id": c["id"],
                          "track": track, "key": key})
        rows.append({"key": key, "label": label, "sub": sub, "kind": kind, "weight": w,
                     "word": WEIGHT_WORDS[w], "cells": cells, "top": top, "max": mx})
    summary = []
    for i, c in enumerate(cards):
        leads = [
            r["label"].lower() for r in rows
            if r["weight"] >= LEAD_WEIGHT and r["cells"][i]["top"]
        ]
        summary.append({"name": c["org"], "leads": leads})
    cols = [{"id": c["id"], "org": c["org"], "title": c["title"], "mono": c["mono"],
             "mono_i": c["mono_i"]} for c in cards]
    return Matrix(columns=cols, rows=rows, summary=summary)


def ahead_text(leads: list[str]) -> str:
    return ("Ahead on " + ", ".join(leads)) if leads else \
        "Not clearly ahead on anything you weight highly"


# --------------------------------------------------------------------------- walk-away and ask
@dataclass(frozen=True)
class Amount:
    """A typed figure read as a number. `unit` is "year" (months are x12 into it), "day", "hour"
    or None when the text names none; `currency` is an ISO code or None when none was typed."""

    value: float
    unit: str | None
    currency: str | None


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)\s*([km])?(?![a-z])")
_SYMBOLS = {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP"}
_CODES = (
    "usd", "eur", "gbp", "cad", "aud", "nzd", "chf", "jpy", "inr", "sgd", "mxn", "brl", "sek",
    "nok", "dkk", "pln", "zar", "hkd", "cny",
)
_PREFIXED = {"us$": "USD", "ca$": "CAD", "c$": "CAD", "au$": "AUD", "a$": "AUD", "nz$": "NZD"}
_UNITS = (  # (unit, multiplier into the unit, pattern)
    ("year", 1, r"/\s*(?:yr|year|annum)\b|\bper\s+(?:year|annum|yr)\b|\ba\s+year\b"
                r"|\bannual(?:ly)?\b|\byearly\b|\bp\.?a\.?(?![a-z])"),
    ("year", 12, r"/\s*(?:mo|month)\b|\bper\s+month\b|\ba\s+month\b|\bmonthly\b"),
    ("day", 1, r"/\s*day\b|\bper\s+day\b|\ba\s+day\b|\bdaily\b|\bday\s+rate\b"),
    ("hour", 1, r"/\s*(?:hr|hour)\b|\bper\s+hour\b|\ban\s+hour\b|\bhourly\b"),
)
_UNIT_WORDS = {"year": "a year", "day": "a day", "hour": "an hour"}


def parse_amount(text: str | None) -> Amount | None:
    """Read one money figure out of what the operator typed: "$150k/yr", "150,000 USD", "150k",
    "1.2m", "12k a month" (annualized), "550 a day", "140k base". None when the text holds no
    figure or more than one, or names two currencies or two units: Kestrel then cannot say what
    it means, and the caller refuses rather than guesses."""
    raw = (text or "").strip().lower()
    nums = _NUM.findall(raw)
    if len(nums) != 1:
        return None
    digits, suffix = nums[0]
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    value *= {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    currencies = {code.upper() for code in _CODES if re.search(rf"\b{code}\b", raw)}
    for sym, code in _PREFIXED.items():
        if re.search(rf"(?<![a-z]){re.escape(sym)}", raw):
            currencies.add(code)
    plain = re.sub(r"(?:us|ca|c|au|a|nz)\$", "", raw)
    currencies |= {code for sym, code in _SYMBOLS.items() if sym in plain}
    if len(currencies) > 1:
        return None
    units: set[tuple[str, int]] = set()
    for unit, mult, pat in _UNITS:
        if re.search(pat, raw):
            units.add((unit, mult))
    if len(units) > 1:
        return None
    unit, mult = next(iter(units)) if units else (None, 1)
    return Amount(value * mult, unit, next(iter(currencies), None))


def ask_problem(ask: str | None, walk_away: str | None) -> str | None:
    """Why an ask cannot go to the model, in plain words, or None. An ask is optional; when one is
    given it must be one amount, in the same unit and currency as the walk-away, and not below it.
    Nothing is converted between currencies and nothing is guessed."""
    if not (ask or "").strip():
        return None
    a, w = parse_amount(ask), parse_amount(walk_away)
    if a is None:
        return "Kestrel could not read your ask as one amount. Type it like 150k or $150,000."
    if w is None:
        return ("Kestrel could not read your walk-away as one amount, so it cannot check your "
                "ask against it. Type it like 140k or $140,000.")
    if a.unit and w.unit and a.unit != w.unit:
        return (f"Your walk-away is {_UNIT_WORDS[w.unit]} and your ask is {_UNIT_WORDS[a.unit]}. "
                "Use the same unit for both.")
    if a.currency and w.currency and a.currency != w.currency:
        return (f"Your walk-away is in {w.currency} and your ask is in {a.currency}. "
                "Use one currency for both.")
    if a.value < w.value:
        return "Your ask is below your walk-away. Kestrel keeps drafts above it."
    return None


def ask_below_walk_away(ask: str | None, walk_away: str | None) -> bool:
    a, w = parse_amount(ask), parse_amount(walk_away)
    return a is not None and w is not None and a.value < w.value


# ------------------------------------------------------------------------------- the draft text
def paragraphs(text: str | None) -> list[str]:
    """The draft as paragraphs: blank lines split them, a single newline stays inside one."""
    return [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]


def highlight(text: str, points: list[tuple[str, str]], *, min_words: int = 4) -> list[tuple]:
    """Split one paragraph into (text, source) pieces; source is set where the paragraph carries
    one of the operator's proof points (a claim and where it came from). A claim matches whole, or
    by its longest run of at least `min_words` words, since a draft often trims one. Display only:
    nothing here edits the draft or decides what it may say."""
    lowered = text.lower()
    spans: list[tuple[int, int, str]] = []
    for claim, evidence in points:
        words = claim.split()
        found = None
        for size in range(len(words), min_words - 1, -1):
            for i in range(0, len(words) - size + 1):
                phrase = " ".join(words[i:i + size]).strip(".,;:").lower()
                at = lowered.find(phrase) if phrase else -1
                if at >= 0:
                    found = (at, at + len(phrase))
                    break
            if found:
                break
        if found and not any(found[0] < b and a < found[1] for a, b, _ in spans):
            spans.append((*found, f"From your proof points: {claim}. Source: {evidence or 'you'}"))
    spans.sort()
    out: list[tuple] = []
    cur = 0
    for a, b, src in spans:
        if a > cur:
            out.append((text[cur:a], None))
        out.append((text[a:b], src))
        cur = b
    if cur < len(text):
        out.append((text[cur:], None))
    return out or [(text, None)]


def exact_line(offer: Any, track: str) -> str:
    """The figures as the operator recorded them, to the dollar, for Show details."""
    bits: list[str] = []
    if track == "consulting":
        for label, v, as_money in (
            ("day rate", getattr(offer, "day_rate", None), True),
            ("days", getattr(offer, "days_committed", None), False),
            ("weeks", getattr(offer, "duration_weeks", None), False),
        ):
            if v is not None:
                bits.append(f"{label} {money(v) if as_money else v}")
    else:
        for label, v, kind in (
            ("base", getattr(offer, "base", None), "$"),
            ("bonus target", getattr(offer, "bonus_target_pct", None), "%"),
            ("signing", getattr(offer, "signing", None), "$"),
            ("equity estimate", getattr(offer, "equity_value_estimate", None), "$"),
        ):
            if v is not None:
                bits.append(f"{label} {money(v) if kind == '$' else f'{v:g}%'}")
    return "As you recorded it: " + ", ".join(bits) + "." if bits else ""
