"""What Pipeline's rows and role panel say (D-UI-96), as plain data a template prints.

Pure functions over a role, its evaluation and the redacted evaluation JSON `get_detail` builds.
Nothing here reads the database, so every sentence and number is unit-testable and the
confidential-mode copy (never the raw ORM field) is the only prose a panel can show."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from kestrel.date_display import format_display
from kestrel.rules.margin import NEAR_FLOOR_TAG, tags_from_stage_reason

# The seven colors a company's monogram can take, as theme-token classes (pl-mono-N).
MONO_TONES = 7
# A role found within this many hours shows "Found ..." in the brand color.
FRESH_HOURS = 6
# The ghost score at or above which a row carries the warning icon.
GHOST_WARN = 0.6

# "Not for me because..." (D-UI-96). key -> (button text, the dimension it teaches, or None).
# Role fit, comp and logistics, and career direction are the Tier 2 dimension names; "Just not
# now" teaches nothing about the role.
REASONS_EMPLOYMENT: tuple[tuple[str, str, str | None], ...] = (
    ("kind", "Not my kind of role", "role_fit"),
    ("pay_place", "Pay or location", "comp_logistics"),
    ("level", "Wrong level", "trajectory"),
    ("later", "Just not now", None),
)
REASONS_CONSULTING: tuple[tuple[str, str, str | None], ...] = (
    ("kind", "Not my kind of work", "engagement_fit"),
    ("rate", "Rate too low", "economics"),
    ("deliver", "Can't deliver it", "delivery_feasibility"),
    ("later", "Just not now", None),
)
# A signal's "Not relevant because..." (Consulting, Signals tab).
REASONS_SIGNAL: tuple[tuple[str, str, str | None], ...] = (
    ("kind", "Not my kind of work", "engagement_fit"),
    ("size", "Wrong size company", "engagement_fit"),
    ("need", "No real need", "strategic_value"),
    ("later", "Just not now", None),
)


def reasons_for(track: str | None, *, signal: bool = False):
    if signal:
        return REASONS_SIGNAL
    return REASONS_CONSULTING if track == "consulting" else REASONS_EMPLOYMENT


def reason_by_key(track: str | None, key: str | None, *, signal: bool = False):
    """(key, text, dimension) for a reason key, or None when the key is not one of the four."""
    for r in reasons_for(track, signal=signal):
        if r[0] == key:
            return r
    return None


# ------------------------------------------------------------------------------- the row's parts
def monogram(name: str | None) -> dict[str, Any]:
    """Two letters and one of seven tone classes, the same for the same company every time."""
    words = [w for w in (name or "").replace("-", " ").split() if w]
    if not words:
        return {"letters": "?", "tone": 6}
    letters = words[0][0] + (words[1][0] if len(words) > 1 else "")
    return {
        "letters": letters.upper(),
        "tone": sum(ord(c) for c in (name or "")) % MONO_TONES,
    }


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def ago_words(hours: float) -> str:
    """'just now', '3h ago', '5d ago'."""
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def posted_words(date_posted: str | None, now: datetime | None = None) -> str:
    when = _parse(date_posted)
    if when is None:
        return "Posting date unknown"
    days = ((now or datetime.now(UTC)) - when).days
    if days <= 0:
        return "Posted today"
    if days == 1:
        return "Posted yesterday"
    return f"Posted {days} days ago"


def found_age(opp, now: datetime | None = None) -> dict[str, Any]:
    """"Found 3h ago" from `first_seen_at`, brand-colored under six hours; the hover says both
    when Kestrel found the role and how old the posting is (D-UI-96)."""
    now = now or datetime.now(UTC)
    when = _parse(getattr(opp, "first_seen_at", None))
    if when is None:
        return {"text": "", "fresh": False, "title": ""}
    hours = max(0.0, (now - when).total_seconds() / 3600)
    ago = ago_words(hours)
    posted = posted_words(getattr(opp, "date_posted", None), now).replace("Posted", "posted")
    return {
        "text": f"Found {ago}",
        "fresh": hours < FRESH_HOURS,
        "title": f"Found by Kestrel {ago}; {posted}.",
    }


_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$"}


def _money(value: float, currency: str | None) -> str:
    sym = _SYMBOL.get((currency or "USD").upper())
    head = sym if sym is not None else f"{currency} "
    return f"{head}{value:,.0f}"


def pay_text(opp) -> str:
    """"$118,000 to $142,000", "$552 to $650 a day", or "" when no pay was posted or inferred.
    A pay the model or the pay corpus estimated (comp_source "inferred") ends in "est.": it is
    never shown as if the employer had posted it."""
    lo, hi = getattr(opp, "comp_min", None), getattr(opp, "comp_max", None)
    if not lo and not hi:
        return ""
    cur = getattr(opp, "comp_currency", None)
    if lo and hi and lo != hi:
        text = f"{_money(lo, cur)} to {_money(hi, cur)}"
    else:
        text = _money(hi or lo, cur)
    unit = {"day": " a day", "hour": " an hour", "month": " a month"}.get(
        getattr(opp, "comp_interval", None) or "", ""
    )
    est = " est." if getattr(opp, "comp_source", None) == "inferred" else ""
    return f"{text}{unit}{est}"


def is_estimate(opp) -> bool:
    return getattr(opp, "comp_source", None) == "inferred" and pay_text(opp) != ""


def place_text(opp) -> str:
    """"Austin, TX · Hybrid", "Remote (US)" style: the posted place, then the work type."""
    loc = getattr(opp, "location_raw", None) or ", ".join(
        p for p in (getattr(opp, "city", None), getattr(opp, "state", None)) if p
    )
    rt = getattr(opp, "remote_type", None)
    kind = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(rt or "")
    if loc and kind and kind.lower() not in loc.lower():
        return f"{loc} · {kind}"
    return loc or kind or ""


def engagement_value(opp, ev) -> str:
    """"about $18,000 total" for a lead whose read gave an engagement value (Consulting); the
    model's estimate, so it always says "about"."""
    if getattr(opp, "track", None) != "consulting" or ev is None:
        return ""
    econ = (getattr(ev, "blocks", None) or {}).get("d_economics")
    value = econ.get("engagement_value") if isinstance(econ, dict) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return ""
    return f"about {_money(float(value), getattr(opp, 'comp_currency', None))} total"


def near_floor(opp) -> bool:
    """The role was kept with the "Below your minimum" tag (D-RULES-2); a role with no posted pay
    never carries it."""
    return bool(
        (getattr(opp, "comp_max", None) or getattr(opp, "comp_min", None))
        and NEAR_FLOOR_TAG in tags_from_stage_reason(getattr(opp, "stage_reason", None))
    )


@lru_cache(maxsize=1)
def _current_t2_versions() -> dict[str, str]:
    """The packaged Tier 2 prompt version per track, for the staleness warning. Empty when the
    prompts cannot be read: a warning is never worth a failed page."""
    try:
        from kestrel.pipeline._shared import prompt_spec
        from kestrel.pipeline.tier2 import _VERSION

        return {track: prompt_spec(v).version for track, v in _VERSION.items()}
    except Exception:  # a display hint only
        return {}


def warning_text(opp, ev) -> str | None:
    """The one tooltip behind a row's single warning icon, or None for a healthy row (a healthy
    row carries no badges). Ghost score, legitimacy, a needs-review divergence, liveness and an
    out-of-date scoring prompt, in that order of how much they change what the operator does."""
    parts: list[str] = []
    legit = getattr(ev, "legitimacy", None) if ev is not None else None
    status = legit.get("status") if isinstance(legit, dict) else None
    if status == "reject":
        parts.append("This may not be a real opening.")
    elif status == "suspect":
        parts.append("Check the posting before you rely on it.")
    if (getattr(opp, "ghost_score", 0.0) or 0.0) >= GHOST_WARN:
        n = getattr(opp, "seen_count", 1) or 1
        parts.append(
            "Possible ghost posting: Kestrel has seen it "
            + (f"{n} times." if n > 1 else "with warning signs.")
        )
    if ev is not None and getattr(ev, "needs_review", False):
        parts.append("The model and your past ratings disagree on this one. Worth a second look.")
    if getattr(opp, "liveness", None) == "closed":
        parts.append("This posting looks closed.")
    if ev is not None and getattr(ev, "tier", 0) and ev.tier >= 2 and ev.prompt_version:
        current = _current_t2_versions().get(getattr(opp, "track", "") or "")
        if current and ev.prompt_version != current:
            parts.append("Scored with older scoring instructions. Score again to refresh it.")
    return " ".join(parts) or None


# ----------------------------------------------------------------------------------- the panel
_DIM_NAMES = {
    "role_fit": "Role fit",
    "domain_match": "Domain match",
    "company_context": "Company context",
    "comp_logistics": "Pay and logistics",
    "trajectory": "Career direction",
    "engagement_fit": "Engagement fit",
    "buyer_access": "Buyer access",
    "economics": "Economics",
    "delivery_feasibility": "Delivery",
    "strategic_value": "Strategic value",
}
_STRENGTH = {"strong": ("g", "+", "Strong"), "partial": ("b", "~", "Partial"),
             "adjacent": ("b", "~", "Adjacent")}
_LEVEL_WORDS = {
    "above": "A step above your level",
    "at": "Same level as now",
    "below": "A step below your level",
    "unclear": "Level is unclear",
}
# Where the pay marker sits on the red-to-green bar, from the Tier 2 vs_floor read.
_PAY_SCALE = {
    "below_floor": (12, "Below your minimum"),
    "at_floor": (36, "At your minimum"),
    "above_floor": (58, "Above your minimum"),
    "above_target": (84, "Above your target"),
}
_CONSULT_PAY_SCALE = {
    "below_floor": (12, "Below your day-rate floor"),
    "at_floor": (36, "At your day-rate floor"),
    "above_floor": (58, "Above your day-rate floor"),
    "above_target": (84, "Above your day-rate target"),
}
_ACCESS = {"easy": "Easy", "moderate": "Moderate", "hard": "Hard", "unknown": "Not clear yet"}


def _blocks(ev_json: dict | None) -> dict:
    return (ev_json or {}).get("blocks") or {}


def _s(value: Any) -> str:
    return " ".join(str(value).split()) if value else ""


def dimension_rows(ev_json: dict | None) -> list[dict[str, Any]]:
    """The five dimension bars: name, score and a 0 to 100 width, in the order the model gave."""
    out = []
    for key, d in ((ev_json or {}).get("dimensions") or {}).items():
        score = d.get("score") if isinstance(d, dict) else d
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        score = max(0.0, min(5.0, score))
        out.append({
            "key": key,
            "name": _DIM_NAMES.get(key, key.replace("_", " ").capitalize()),
            "score": score,
            "pct": round(score / 5 * 100),
        })
    return out


def pay_box(opp, ev_json: dict | None) -> dict[str, Any]:
    """The pay box: the posted pay (or "Pay not listed"), the marker on the bar and the sentence
    under it. The position and sentence are the model's read of the pay against the operator's
    floor and target (d_comp.vs_floor, or d_economics.vs_floor for a lead); when it has no read
    the bar is left out rather than drawn at a guess."""
    consulting = getattr(opp, "track", None) == "consulting"
    b = _blocks(ev_json)
    comp = b.get("d_economics" if consulting else "d_comp") or {}
    vs = comp.get("vs_floor") if isinstance(comp, dict) else None
    scale = (_CONSULT_PAY_SCALE if consulting else _PAY_SCALE).get(vs)
    posted = pay_text(opp)
    if not posted and isinstance(comp, dict) and not consulting and comp.get("stated"):
        posted = _s(comp["stated"])
    out = {
        "label": "Rate" if consulting else "Pay",
        "text": posted or "Pay not listed",
        "estimate": is_estimate(opp),
        "pos": scale[0] if scale else None,
        "note": scale[1] if scale else ("Kestrel has no read on this pay." if posted else
                                        "No pay posted. Kestrel kept it."),
    }
    if near_floor(opp):
        out["note"] = NEAR_FLOOR_TAG
        out["near_floor"] = True
    return out


def level_box(opp, ev_json: dict | None) -> dict[str, Any]:
    """The second box: Level for a role, Buyer access for a lead. Empty when nothing was read."""
    b = _blocks(ev_json)
    if getattr(opp, "track", None) == "consulting":
        buyers = b.get("c_buyers") or {}
        if not isinstance(buyers, dict) or not buyers:
            return {}
        return {
            "label": "Buyer access",
            "text": _ACCESS.get(buyers.get("access_difficulty"), "Not clear yet"),
            "note": _s(buyers.get("access_path")) or _s(buyers.get("economic_buyer")),
        }
    level = b.get("c_level") or {}
    if not isinstance(level, dict) or not level:
        return {}
    bits = [x for x in (
        f"Reports to {_s(level['reports_to'])}" if level.get("reports_to") else "",
        f"team of {_s(level['team_size'])}" if level.get("team_size") else "",
    ) if x]
    return {
        "label": "Level",
        "text": _LEVEL_WORDS.get(level.get("relative_level"), "Level is unclear"),
        "note": " · ".join(bits) or _s(level.get("scope_read")),
    }


# A P2 match the profile neither shows nor rules out (PROMPTS.md P2 rule 6 and post-processing
# rule 8): Partly met, listed with the gaps, never a reason the role fits (hard rule 4, O20).
UNVERIFIED = "unverified"
UNVERIFIED_HOW = "Your profile does not show this yet, so Kestrel can't tell."
# The same gap when the whole profile is empty (issue #64): say what to do about it.
UNVERIFIED_HOW_THIN = (
    "Your profile has no proof points yet. Add them on Profile > Documents, then score again."
)


def unverified_how(thin: bool = False) -> str:
    return UNVERIFIED_HOW_THIN if thin else UNVERIFIED_HOW


def fit_reasons(ev_json: dict | None) -> list[dict[str, str]]:
    """Why it fits: the model's matches, each with how strong it is. A match the profile does
    not show ("unverified") is not a reason it fits; it is listed under gaps (O20)."""
    fit = _blocks(ev_json).get("b_fit") or {}
    out = []
    for m in (fit.get("matches") if isinstance(fit, dict) else None) or []:
        if not isinstance(m, dict) or not m.get("requirement"):
            continue
        if m.get("strength") == UNVERIFIED:
            continue
        tone, mark, word = _STRENGTH.get(m.get("strength"), ("b", "~", "Partial"))
        out.append({"tone": tone, "mark": mark, "word": word, "text": _s(m["requirement"]),
                    "evidence": _s(m.get("evidence"))})
    return out[:5]


def all_gap_rows(ev_json: dict | None, *, thin: bool = False) -> list[dict[str, Any]]:
    """Every gap to address, each with how to handle it: the model's gaps (a fatal gap says so;
    noise is left out), then each match the profile does not show yet ("unverified"), which is
    never counted in the role's favor (hard rule 4, O20)."""
    fit = _blocks(ev_json).get("b_fit") or {}
    out: list[dict[str, Any]] = []
    for g in (fit.get("gaps") if isinstance(fit, dict) else None) or []:
        if not isinstance(g, dict) or not g.get("requirement") or g.get("severity") == "noise":
            continue
        out.append({"text": _s(g["requirement"]), "how": _s(g.get("mitigation")),
                    "fatal": g.get("severity") == "fatal", "unverified": False})
    for m in (fit.get("matches") if isinstance(fit, dict) else None) or []:
        if isinstance(m, dict) and m.get("requirement") and m.get("strength") == UNVERIFIED:
            out.append({"text": _s(m["requirement"]), "how": unverified_how(thin), "fatal": False,
                        "unverified": True})
    return out


PANEL_GAPS = 4


def gap_rows(ev_json: dict | None, *, thin: bool = False) -> list[dict[str, Any]]:
    """The panel's gaps: at most four rows. When there are more, the fourth says how many more
    and how many of those the profile does not show yet, so nothing is dropped silently (O20)."""
    rows = all_gap_rows(ev_json, thin=thin)
    if len(rows) <= PANEL_GAPS:
        return rows
    shown, rest = rows[: PANEL_GAPS - 1], rows[PANEL_GAPS - 1 :]
    unknown = sum(1 for r in rest if r["unverified"])
    how = "The Evaluation tab lists them all."
    if unknown:
        how = (
            f"{unknown} of them {'is' if unknown == 1 else 'are'} not shown in your profile yet, "
            "so Kestrel can't tell. " + how
        )
    more = {"text": f"{len(rest)} more", "how": how, "fatal": False, "unverified": bool(unknown)}
    return [*shown, more]


def lead_with(opp, ev_json: dict | None) -> dict[str, str]:
    """"Lead with" for a role, "Opening move" for a lead; empty when the model gave none."""
    b = _blocks(ev_json)
    if getattr(opp, "track", None) == "consulting":
        ap = b.get("e_approach") or {}
        text = _s(ap.get("opening_move")) if isinstance(ap, dict) else ""
        return {"title": "Opening move", "text": text} if text else {}
    ang = b.get("e_angle") or {}
    text = _s(ang.get("lead_with")) if isinstance(ang, dict) else ""
    return {"title": "Lead with", "text": text} if text else {}


def summary_text(opp, ev_json: dict | None) -> str:
    """The model-written summary of the role (labeled as such by the template)."""
    b = _blocks(ev_json)
    if getattr(opp, "track", None) == "consulting":
        sit = b.get("a_situation") or {}
        return _s(sit.get("stated")) if isinstance(sit, dict) else ""
    summ = b.get("a_role_summary") or {}
    return _s(summ.get("what_this_actually_is")) if isinstance(summ, dict) else ""


def liveness_fact(opp) -> dict[str, str]:
    live = getattr(opp, "liveness", None)
    if live == "live":
        return {"text": "Still live", "tone": "good"}
    if live == "closed":
        return {"text": "Closed", "tone": "bad"}
    return {"text": "Not checked yet", "tone": "idle"}


def ring_tone(word: str | None) -> str:
    return "good" if word in ("strong fit", "good fit") else "warn"


def row_view(r) -> dict[str, Any]:
    """The parts of one list row a template prints, from a queries.OppRow."""
    o = r.opp
    org = r.org_name or o.org_name
    return {
        "org": org,
        "mono": monogram(org),
        "found": found_age(o),
        "pay": pay_text(o),
        "estimate": is_estimate(o),
        "place": place_text(o),
        "value": engagement_value(o, r.ev),
        "warn": warning_text(o, r.ev),
        "near_floor": near_floor(o),
    }


def signal_view(sig, now: datetime | None = None) -> dict[str, Any]:
    """The same for a Consulting signal row (queries.SignalRow). Found within the week reads
    "This week" in the list; the source and date are the stored ones."""
    o = sig.opp
    found = found_age(o, now)
    when = _parse(getattr(o, "first_seen_at", None))
    hours = ((now or datetime.now(UTC)) - when).total_seconds() / 3600 if when else 1e9
    return {
        "org": sig.org_name,
        "found": found,
        "this_week": hours < 168,
        "date": format_display(getattr(o, "event_date", None)),
    }
