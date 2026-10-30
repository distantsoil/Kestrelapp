"""What the Role page prints (D-UI-150, SPEC-INTERFACES 2.1 `/opportunities/{id}`), as plain data.

Pure functions over a role, its evaluation and the redacted evaluation JSON `get_detail` builds.
Nothing here reads the database or the network, so every sentence, bracket and ring segment is
unit-testable, and the confidential-mode copy (never the raw ORM field) is the only prose the page
can show. Every piece the model did not write is left out or labeled "Not recorded"; nothing is
guessed (hard rules 2, 6 and 7)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from kestrel.web import pipeline_view as pv
from kestrel.web.calibrate_view import DIM_WORDS

# Fewer than this many similar roles is "Too few" and no bracket (hard rule 6).
MIN_SIMILAR = 5
WHEEL_R = 54
WHEEL_C = 2 * math.pi * WHEEL_R
WHEEL_GAP = 3.0
# The three states of a thing the posting asks for, as the wheel and the lists name them.
MET, PART, GAP = "met", "part", "gap"
STATE_WORDS = {MET: "Met", PART: "Partly met", GAP: "Not met"}
MARKS = {MET: "+", PART: "~", GAP: "!"}
# A match the model could not check against the profile (strength "unverified", PROMPTS.md P2
# rule 6): Partly met, listed under Gaps with this line, never Met and never shown as proof
# (hard rule 4, O20).
UNVERIFIED = pv.UNVERIFIED
UNVERIFIED_HOW = pv.UNVERIFIED_HOW

_ORDER = ("evaluation", "posting", "organization", "documents", "history")
TAB_LABELS = {
    "evaluation": "Evaluation",
    "posting": "Posting",
    "organization": "Organization",
    "documents": "Documents",
    "history": "History",
}


def tab_order(name: str) -> int:
    return _ORDER.index(name) if name in _ORDER else 0


def _s(value: Any) -> str:
    return " ".join(str(value).split()) if value else ""


def _blocks(ev_json: dict | None) -> dict:
    return (ev_json or {}).get("blocks") or {}


def _sentence(text: str) -> str:
    text = _s(text)
    if text and text[-1] not in ".!?":
        text += "."
    return text


# ----------------------------------------------------------------------------- the score card
def dimension_rows(ev_json: dict | None, track: str | None) -> list[dict[str, Any]]:
    """The five dimension bars in the plain words Calibrate uses (Career direction, Pay and
    logistics): name, score, a 0 to 100 width and the model's reason. A dimension the model left
    out is skipped, never invented."""
    names = DIM_WORDS["consulting" if track == "consulting" else "employment"]
    raw = (ev_json or {}).get("dimensions") or {}
    out: list[dict[str, Any]] = []
    for key, name in names.items():
        d = raw.get(key)
        score = d.get("score") if isinstance(d, dict) else d
        try:
            score = max(0.0, min(5.0, float(score)))
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "key": key,
                "name": name,
                "score": score,
                "pct": round(score / 5 * 100),
                "why": _s(d.get("why")) if isinstance(d, dict) else "",
            }
        )
    return out


_MATCH_TITLES = {
    "strong fit": "Why it is a strong match",
    "good fit": "Why it is a good match",
    "maybe": "Why it is a partial match",
    "your call": "What the model saw",
    "not a fit": "Why it is a weak match",
}
_PAY_SENTENCES = {
    "above_target": "The pay sits above your target.",
    "above_floor": "The pay is above your minimum.",
    "at_floor": "The pay is at your minimum.",
    "below_floor": "The pay is below your minimum.",
}
_LEVEL_SENTENCES = {
    "at": "The level matches your current role.",
    "above": "The level is a step above your current role.",
    "below": "The level is a step below your current role.",
}


def read_card(opp, ev_json: dict | None, fit_word: str | None) -> dict[str, str] | None:
    """The model-written explanation of the fit, which names its evidence. The model's own
    evidence sentences come first (its strongest matches), then what it said about pay and level,
    then the first gap worth watching. Nothing is composed beyond joining them; None when the
    model gave nothing to join (the card then shows only the bars)."""
    b = _blocks(ev_json)
    title = _MATCH_TITLES.get(fit_word or "", "What the model saw")
    parts: list[str] = []
    if getattr(opp, "track", None) == "consulting":
        thesis = b.get("b_thesis") or {}
        situ = b.get("a_situation") or {}
        econ = b.get("d_economics") or {}
        if isinstance(situ, dict) and situ.get("stated"):
            parts.append(_sentence(situ["stated"]))
        if isinstance(thesis, dict) and thesis.get("engagement"):
            parts.append(_sentence(thesis["engagement"]))
        if isinstance(econ, dict) and econ.get("vs_floor") in _PAY_SENTENCES:
            parts.append(_PAY_SENTENCES[econ["vs_floor"]].replace("The pay", "The rate"))
    else:
        fit = b.get("b_fit") or {}
        matches = (
            [m for m in (fit.get("matches") or []) if isinstance(m, dict)]
            if isinstance(fit, dict)
            else []
        )
        # An unverified match's evidence says what the profile lacks: it is not a reason (O20).
        shown = [m for m in matches if m.get("evidence") and m.get("strength") != UNVERIFIED]
        strong = [m for m in shown if m.get("strength") == "strong"]
        for m in (strong or shown)[:2]:
            parts.append(_sentence(m["evidence"]))
        comp = b.get("d_comp") or {}
        if isinstance(comp, dict) and comp.get("vs_floor") in _PAY_SENTENCES:
            parts.append(_PAY_SENTENCES[comp["vs_floor"]])
        level = b.get("c_level") or {}
        if isinstance(level, dict) and level.get("relative_level") in _LEVEL_SENTENCES:
            parts.append(_LEVEL_SENTENCES[level["relative_level"]])
        # The same gaps the Pipeline panel names, unverified matches included (O20).
        gaps = pv.all_gap_rows(ev_json)
        if gaps and fit_word in ("strong fit", "good fit", "maybe"):
            parts.append(f"Worth watching: {_s(gaps[0]['text']).rstrip('.')}.")
    if not parts:
        for d in sorted(
            dimension_rows(ev_json, getattr(opp, "track", None)), key=lambda r: -r["score"]
        ):
            if d["why"]:
                parts.append(_sentence(d["why"]))
                break
    if not parts:
        return None
    return {"title": title, "text": " ".join(parts)}


def gap_rows(ev_json: dict | None) -> list[dict[str, Any]]:
    """Everything the posting asks for that the evidence does not cover, each with how to handle
    it. Noise is left out; a fatal gap says so."""
    fit = _blocks(ev_json).get("b_fit") or {}
    out = []
    for g in (fit.get("gaps") if isinstance(fit, dict) else None) or []:
        if not isinstance(g, dict) or not g.get("requirement") or g.get("severity") == "noise":
            continue
        out.append(
            {
                "text": _s(g["requirement"]),
                "how": _s(g.get("mitigation")),
                "fatal": g.get("severity") == "fatal",
            }
        )
    return out


# ------------------------------------------------------------------------------- match wheel
LEGACY_SERVICE_HOW = (
    "The earlier read named this without saying what shows it. Score again to check it."
)


def _match_rows(matches: list) -> list[dict[str, Any]]:
    """P2 `b_fit.matches` or P2C `b_thesis.matches` as rows: only `strong` is Met; partial,
    adjacent and unverified are Partly met. An unverified match carries no evidence (what the
    profile lacks is not proof: no Evidence row, no source lookup) and says Kestrel can't tell
    (hard rule 4, O20)."""
    rows: list[dict[str, Any]] = []
    for m in matches:
        if not isinstance(m, dict) or not m.get("requirement"):
            continue
        unverified = m.get("strength") == UNVERIFIED
        rows.append(
            {
                "text": _s(m["requirement"]),
                "state": MET if m.get("strength") == "strong" else PART,
                "evidence": "" if unverified else _s(m.get("evidence")),
                "how": UNVERIFIED_HOW if unverified else "",
                "strength": _s(m.get("strength")),
                "unverified": unverified,
            }
        )
    return rows


def requirements(opp, ev_json: dict | None) -> list[dict[str, Any]]:
    """One row per thing the posting asks for: Met (a strong match), Partly met (partial,
    adjacent, or unverified: the profile neither shows nor rules it out) or Not met (a gap).
    From the model's `b_fit` for a role; for a lead from `b_thesis.matches` graded the same way
    (an older read's bare service lines are Partly met) and its delivery read (solo-feasibility
    is met, delivery gaps and a partner need are not). Each row carries its `k` (the wheel
    segment), its state and the model's evidence or how to handle it."""
    rows: list[dict[str, Any]] = []
    b = _blocks(ev_json)
    if getattr(opp, "track", None) == "consulting":
        thesis = b.get("b_thesis") or {}
        delivery = b.get("f_delivery") or {}
        graded = thesis.get("matches") if isinstance(thesis, dict) else None
        if isinstance(graded, list):
            rows += _match_rows(graded)
        else:
            # A read from before b_thesis.matches named service lines with no evidence: nothing
            # to count as Met, so each is Partly met under Gaps (PROMPTS.md P2C, O20).
            for line in (thesis.get("service_lines") if isinstance(thesis, dict) else None) or []:
                if _s(line):
                    rows.append(
                        {
                            "text": f"{_s(line)} work",
                            "state": PART,
                            "evidence": "",
                            "how": LEGACY_SERVICE_HOW,
                        }
                    )
        if isinstance(delivery, dict):
            if delivery.get("solo_feasible"):
                rows.append(
                    {
                        "text": "Can be delivered on your own",
                        "state": MET,
                        "evidence": _s(delivery.get("effort_shape")),
                        "how": "",
                    }
                )
            for gap in delivery.get("gaps") or []:
                if _s(gap):
                    rows.append({"text": _s(gap), "state": GAP, "evidence": "", "how": ""})
            if _s(delivery.get("partner_needed")):
                rows.append(
                    {
                        "text": f"A partner is needed: {_s(delivery['partner_needed'])}",
                        "state": GAP,
                        "evidence": "",
                        "how": "",
                    }
                )
    else:
        fit = b.get("b_fit") or {}
        rows += _match_rows((fit.get("matches") if isinstance(fit, dict) else None) or [])
        for g in gap_rows(ev_json):
            rows.append(
                {
                    "text": g["text"],
                    "state": GAP,
                    "evidence": "",
                    "how": g["how"],
                    "fatal": g["fatal"],
                }
            )
    for k, row in enumerate(rows):
        row["k"] = k
        row["mark"] = MARKS[row["state"]]
        row["word"] = STATE_WORDS[row["state"]]
    return rows


def wheel(reqs: list[dict[str, Any]]) -> dict[str, Any]:
    """The ring: one segment per row (dash and offset in circumference units), the counts, and
    the center text "3 of 6" (met of all)."""
    n = len(reqs)
    counts = {MET: 0, PART: 0, GAP: 0}
    for r in reqs:
        counts[r["state"]] += 1
    seg = WHEEL_C / n if n else 0.0
    arcs = []
    _d = max(seg - WHEEL_GAP, 0.5)
    for r in reqs:
        arcs.append(
            {
                "k": r["k"],
                "state": r["state"],
                "text": r["text"],
                "word": r["word"],
                "dash": f"{_d:.2f} {WHEEL_C - _d:.2f}",
                "offset": f"{-r['k'] * seg:.2f}",
            }
        )
    return {
        "n": n,
        "met": counts[MET],
        "part": counts[PART],
        "gap": counts[GAP],
        "arcs": arcs,
        "center": f"{counts[MET]} of {n}",
        "r": WHEEL_R,
        "c": round(WHEEL_C, 2),
    }


# ------------------------------------------------------------------------------------- evidence
def evidence_rows(reqs: list[dict[str, Any]], sources: dict[int, dict[str, str]]) -> list[dict]:
    """For each met or partly met thing: the posting's words, the operator's matching proof (the
    model's `evidence`), and a link to the exact resume bullet or story when one could be found
    (`sources` maps a row's `k` to {label, href}). With none found the row says "Source not
    recorded" rather than pointing at a guess."""
    out = []
    for r in reqs:
        if r["state"] == GAP or not r.get("evidence"):
            continue
        out.append(
            {"k": r["k"], "quote": r["text"], "proof": r["evidence"], "source": sources.get(r["k"])}
        )
    return out[:6]


# ------------------------------------------------------------------------------------ pay band
def _k(value: float, per_day: bool) -> str:
    if per_day:
        return f"${value:,.0f}"
    return f"${value / 1000:,.0f}k" if value >= 10_000 else f"${value:,.0f}"


def pay_band(
    opp,
    ev_json: dict | None,
    stats: dict | None,
    n_seen: int,
    floor: float | None,
    target: float | None,
) -> dict[str, Any]:
    """The Pay box: the posted pay (labeled when it is an estimate), the band with the floor and
    target, the similar-roles bracket (the middle half of the operator's own corpus, drawn only
    from five observations) and the role's marker, the one-sentence read against the floor, and
    the similar-roles sentence. Fewer than five similar roles reads "Too few (n)" and draws no
    bracket (hard rule 6)."""
    consulting = getattr(opp, "track", None) == "consulting"
    base = pv.pay_box(opp, ev_json)
    per_day = consulting or getattr(opp, "comp_interval", None) == "day"
    lo_v, hi_v = getattr(opp, "comp_min", None), getattr(opp, "comp_max", None)
    interval_ok = consulting or getattr(opp, "comp_interval", None) in (None, "year")
    out: dict[str, Any] = {
        "label": base["label"],
        "text": base["text"],
        "estimate": base["estimate"],
        "note": base["note"],
        "near_floor": bool(base.get("near_floor")),
        "band": None,
        "similar": None,
        "tone": (
            "warn"
            if base.get("near_floor") or str(base["note"]).startswith("Below")
            else "good"
            if str(base["note"]).startswith("Above")
            else ""
        ),
    }
    mid = ((lo_v or hi_v) + (hi_v or lo_v)) / 2 if (lo_v or hi_v) else None
    if not interval_ok:
        mid = None  # an hourly or monthly figure does not sit on a yearly band
    marks = [v for v in (floor, target, mid) if v]
    bracket = None
    if stats and not consulting:
        bracket = (stats["p25"], stats["p75"])
        marks.extend(bracket)
        out["similar"] = {
            "text": f"Most similar roles pay {pv._money(stats['p25'], None)} to "
            f"{pv._money(stats['p75'], None)} ({stats['n']} roles).",
            "n": stats["n"],
            "enough": True,
        }
    else:
        out["similar"] = {
            "text": f"Similar roles: Too few ({n_seen}).",
            "n": n_seen,
            "enough": False,
        }
    if len(marks) >= 2 or (mid and (floor or target)):
        lo, hi = min(marks) * 0.9, max(marks) * 1.08
        span = (hi - lo) or 1.0

        def pos(v: float) -> float:
            return round(max(0.0, min(100.0, (v - lo) / span * 100)), 1)

        ticks = []
        for label, v in (("Floor", floor), ("Target", target)):
            if v:
                ticks.append({"label": f"{label} {_k(v, per_day)}", "pos": pos(v), "low": False})
        if len(ticks) == 2 and abs(ticks[0]["pos"] - ticks[1]["pos"]) < 16:
            ticks[1]["low"] = True
        out["band"] = {
            "ticks": ticks,
            "marker": pos(mid) if mid else None,
            "bracket": (
                {"left": pos(bracket[0]), "width": max(pos(bracket[1]) - pos(bracket[0]), 1.0)}
                if bracket
                else None
            ),
        }
    return out


def level_box(opp, ev_json: dict | None) -> dict[str, Any]:
    """The Level box: the sentence, a three-step ladder (below, your level, above) with the role
    and the operator marked, and who it reports to and the team size where the model read them.
    A lead reads Buyer access instead and has no ladder."""
    base = pv.level_box(opp, ev_json)
    if not base:
        return {}
    out = dict(base)
    out["ladder"] = None
    if getattr(opp, "track", None) != "consulting":
        rel = (_blocks(ev_json).get("c_level") or {}).get("relative_level")
        at = {"below": 0, "at": 1, "above": 2}.get(rel)
        if at is not None:
            steps = []
            for i, (word, height) in enumerate((("Below", 45), ("Your level", 70), ("Above", 95))):
                steps.append(
                    {
                        "word": word,
                        "height": height,
                        "role": i == at,
                        "you": i == 1,
                        "pin": (
                            "This role · You"
                            if i == 1 == at
                            else "This role"
                            if i == at
                            else "You"
                            if i == 1
                            else ""
                        ),
                    }
                )
            out["ladder"] = steps
    return out


# ------------------------------------------------------------------------- facts and the summary
def facts_row(opp, org_name: str | None) -> list[dict[str, str]]:
    """The facts under the title: company, location, pay, type, posted and whether it is still
    live. Each is {icon, text}; a missing one is left out (never guessed)."""
    out: list[dict[str, str]] = []
    out.append({"icon": "apps", "text": org_name or "Unknown company", "kind": "org"})
    place = pv.place_text(opp)
    if place:
        out.append({"icon": "pin", "text": place, "kind": "place"})
    pay = pv.pay_text(opp)
    if pay:
        out.append({"icon": "offers", "text": pay, "kind": "pay"})
    kind = _s(getattr(opp, "employment_type", None)) or {
        "contract_posting": "Contract posting",
        "rfp": "RFP",
        "referral": "Referral",
        "manual": "Pasted by you",
    }.get(getattr(opp, "opp_type", None) or "", "")
    if kind:
        out.append(
            {
                "icon": "profile",
                "text": kind.replace("_", " ").capitalize() if kind.islower() else kind,
                "kind": "type",
            }
        )
    posted = pv.posted_words(getattr(opp, "date_posted", None))
    if posted == "Posting date unknown":
        found = pv.found_age(opp)
        posted = found["text"] or posted
    out.append({"icon": "clock", "text": posted, "kind": "posted"})
    return out


def summary_card(opp, ev_json: dict | None, org) -> dict[str, Any]:
    """What the role is: the model's summary (labeled AI-written by the template) and tags built
    from facts on file, never from the model's imagination: the company's sector, the work
    arrangement, the employment type, and the short names of what it matched."""
    text = pv.summary_text(opp, ev_json)
    tags: list[str] = []
    for v in (
        getattr(org, "sector", None),
        getattr(opp, "remote_type", None),
        getattr(opp, "employment_type", None),
    ):
        v = _s(v)
        if v and v.lower() not in ("unknown", "none"):
            tag = v.replace("_", " ")
            tag = tag[:1].upper() + tag[1:]
            if tag not in tags:
                tags.append(tag)
    for m in (_blocks(ev_json).get("b_fit") or {}).get("matches") or []:
        req = _s(m.get("requirement")) if isinstance(m, dict) else ""
        if req and len(req) <= 26 and req not in tags:
            tags.append(req)
        if len(tags) >= 6:
            break
    return {"text": text, "tags": tags[:6]}


# --------------------------------------------------------------------------------- the history
def when_words(iso: str | None, now: datetime | None = None) -> str:
    """ "just now", "2 days ago", or the date once it is over a month old."""
    when = pv._parse(iso)
    if when is None:
        return ""
    now = now or datetime.now(UTC)
    secs = (now - when).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{round(secs / 60)} minutes ago"
    if secs < 86400:
        return f"{round(secs / 3600)} hours ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 31:
        return f"{days} days ago"
    from kestrel.date_display import format_display

    return format_display(when, "short_year")


_TIER_TITLES = {1: "Passed the quick screen", 2: "Scored", 3: "Deeper read"}
_MODEL_WORDS = {1: "Quick screen", 2: "Full review", 3: "Deeper read"}


def history_timeline(
    items: list[dict[str, Any]],
    opp,
    source_name: str | None,
    also_on: int,
    rule_sentence: str | None,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """The History tab, newest first: shortlisted, the operator's rating, scored (with the model
    and the prompt version), the quick screen, any rule that set it aside, and found. `items` is
    queries.opportunity_history's list. Each entry is {title, sub, kind, at}."""
    out: list[dict[str, Any]] = []
    for it in items:
        kind, at = it.get("kind"), it.get("at") or ""
        if kind == "evaluation":
            tier = it.get("tier") or 2
            if tier == 1:
                killed = it.get("verdict") == "kill"
                title = "Screened out at the quick screen" if killed else _TIER_TITLES[1]
                sub = ""
            else:
                score = it.get("score")
                step = _MODEL_WORDS.get(tier, "the model")
                title = (
                    f"Scored {score:.1f} by {step}" if score is not None else f"Scored by {step}"
                )
                bits = [
                    b
                    for b in (
                        it.get("model"),
                        (f"prompt version {_prompt_number(it['prompt'])}"
                         if it.get("prompt") else ""),
                    )
                    if b
                ]
                sub = " · ".join(bits)
            out.append({"title": title, "sub": sub, "kind": "evaluation", "at": at})
        elif kind == "label":
            out.append(
                {
                    "title": it.get("label") or "You rated the fit",
                    "sub": it.get("detail") or "",
                    "kind": "label",
                    "at": at,
                }
            )
        elif kind == "status":
            out.append(
                {
                    "title": it.get("label") or "Status",
                    "sub": it.get("detail") or "",
                    "kind": "status",
                    "at": at,
                }
            )
        elif kind == "transmission":
            sub = it.get("detail") or ""
            cost = it.get("cost_usd")
            sub += f" · ${cost:.4f}" if cost is not None else " · cost not reported"
            out.append(
                {
                    "title": it.get("label") or "Deeper read",
                    "sub": sub,
                    "kind": "transmission",
                    "at": at,
                }
            )
    if rule_sentence:
        out.append(
            {
                "title": "Set aside by your rules",
                "sub": rule_sentence,
                "kind": "rule",
                "at": getattr(opp, "first_seen_at", None) or "",
            }
        )
    found_bits = [f"Found on {source_name}" if source_name else "Found by Kestrel"]
    if also_on:
        found_bits[0] += f" and {also_on} other place{'s' if also_on != 1 else ''}"
    out.append(
        {
            "title": found_bits[0],
            "sub": "",
            "kind": "found",
            "at": getattr(opp, "first_seen_at", None) or "",
        }
    )
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    for e in out:
        e["when"] = when_words(e["at"], now)
    return out


def _prompt_number(prompt: str) -> str:
    """"evaluate/v7" reads as "7" (the mockup's "prompt version 7"); anything else as given."""
    tail = str(prompt).rsplit("/", 1)[-1]
    return tail[1:] if tail[:1] in ("v", "V") and tail[1:].isdigit() else str(prompt)
