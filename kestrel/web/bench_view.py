"""What Model check shows, in plain words (D-UI-105, D-DRIFT-11, D-UI-147).

Turns a stored check (kestrel.bench.check: `steps` verdicts and `results`) into a card per step
that has a change, one quiet line per unchanged step, and one table per step behind "See the
numbers". Numbers that measure different things are never put side by side: a Quick screen
keep-or-skip agreement and a Rating verdict agreement each get their own table and caption. Every
figure sits beside the number of rated roles it rests on, and under five it says too few (hard
rule 6). Model names are friendly, never raw ids."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kestrel.bench.check import MIN_RATINGS, STEP_LABEL, STEP_TIER
from kestrel.llm import providers as prov
from kestrel.web.models_view import friendly_model

CAPTIONS = {
    "quick_screen": "How often each model agrees with you on keep or skip",
    "rating": "How often each model reaches the same verdict you did",
    "deeper_read": "How often each model reaches the same verdict you did on a deeper read",
}


def date_words(iso: str | None) -> str:
    """"Oct 12" for a stored UTC timestamp; the year only when it is not this one."""
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        return "an earlier date"
    from kestrel.date_display import format_display

    return format_display(d, "short_year_optional")


def minutes_words(ms_per_role: float) -> str:
    """"about 4 more minutes per 100 roles" size of a latency gap, in plain units."""
    total_s = abs(ms_per_role) * 100 / 1000.0
    if total_s < 90:
        return f"about {max(1, round(total_s))} {'second' if round(total_s) == 1 else 'seconds'}"
    mins = round(total_s / 60)
    return f"about {mins} {'minute' if mins == 1 else 'minutes'}"


def _name(row: dict[str, Any] | None, base: str | None, bases: dict[str, str | None]) -> str:
    if not row:
        return "No model"
    # A derived model reads as the model it is built from (tuned ones are named in `bases`).
    return friendly_model(row.get("model"), base=bases.get(row.get("model") or "") or base)


def _is_free(model: str) -> bool:
    return prov.is_free_variant(model or "")


def step_view(v: dict[str, Any], bases: dict[str, str | None], *, current_model: str = "") -> dict:
    """One step's card (or quiet line). `bases` maps a tier setting name to the model Kestrel's
    own model for it is built from; `current_model` is what the step runs now (settings), so a
    switch already made reads "In use now"."""
    step = v["step"]
    tier = STEP_TIER[step]
    base = bases.get(tier)
    cur, best, best_local = v.get("current"), v.get("best"), v.get("best_local")
    cur_name, best_name = _name(cur, base, bases), _name(best, base, bases)
    best_model = (best or {}).get("model")  # In use now: that model, or one tuned from it
    n = int(v.get("n") or 0)
    out: dict[str, Any] = {
        "step": step, "label": v["label"], "tier": tier, "n": n, "enough": bool(v.get("enough")),
        "change": bool(v.get("change")), "cur_name": cur_name, "best_name": best_name,
        "caption": CAPTIONS[step], "online": bool(best and best.get("online")),
        "model": (best or {}).get("model", ""), "backend": (best or {}).get("backend", ""),
        "applied": bool(
            best_model and current_model
            and (current_model == best_model or bases.get(current_model) == best_model)
        ),
    }
    if not out["enough"]:
        out["text"] = (f"Too few rated roles to compare {v['label']} ({n} of {MIN_RATINGS} "
                       "needed).")
        return out
    if not out["change"]:
        out["text"] = f"{v['label']}: keep {cur_name}. Nothing tested matched you better."
        return out
    slower = ((best or {}).get("mean_latency_ms") or 0) - ((cur or {}).get("mean_latency_ms") or 0)
    has_lat = (best or {}).get("mean_latency_ms") is not None \
        and (cur or {}).get("mean_latency_ms") is not None
    gap_note = ""
    if has_lat and slower > (cur or {}).get("mean_latency_ms", 0) * 0.1:
        gap_note = ", and it is a little slower"
    elif has_lat and slower < -(cur or {}).get("mean_latency_ms", 0) * 0.1:
        gap_note = ", and it is faster"
    out["text"] = f"It matches the answer key more often than {cur_name}{gap_note}."
    out["title"] = f"{v['label']}: switch from {cur_name} to {best_name}"
    out["points"] = v.get("points")
    out["points_line"] = (f"Matches the answer key on {v.get('points')} more roles in every 100, "
                          f"from {n} roles in the answer key")
    if has_lat and abs(slower) > 0:
        out["time_line"] = (f"{'A little slower' if slower > 0 else 'Faster'}: "
                            f"{minutes_words(slower)} {'more' if slower > 0 else 'less'} "
                            "per 100 roles")
    free = bool(best) and (not out["online"] or _is_free(out["model"]))
    out["where_line"] = ("Free, runs on your computer" if not out["online"]
                         else ("Free, runs online with your key" if free
                               else "Runs online with your key and uses your credit"))
    if out["online"] and best_local:
        gain = round((best_local.get("points") or 0) - ((cur or {}).get("points") or 0))
        more = f", matching you on {gain} more roles in every 100" if gain > 0 else ""
        out["local_line"] = (f"The best option on this computer is {_name(best_local, base, bases)}"
                             f"{more}. You can stay private.")
    return out


def numbers_view(check: dict[str, Any], bases: dict[str, str | None]) -> list[dict]:
    """One table per step: its own caption, its own measure, every model that was tested."""
    tables = []
    for sv in check.get("steps", []):
        step = sv["step"]
        base = bases.get(STEP_TIER[step])
        rows = [r for r in check.get("results", []) if r.get("step") == step]
        tables.append({
            "label": STEP_LABEL[step], "caption": CAPTIONS[step], "n": sv.get("n", 0),
            "rows": [{
                "name": _name(r, base, bases), "online": bool(r.get("online")),
                "agree": None if r.get("agreement") is None else round(r["agreement"] * 100),
                "n": r.get("n", 0),
                # A rate from fewer than five roles is not shown as a rate (hard rule 6).
                "few": int(r.get("n") or 0) < MIN_RATINGS,
                "secs": None if r.get("mean_latency_ms") is None
                else round(r["mean_latency_ms"] / 1000.0, 1),
                "valid": None if r.get("first_attempt_valid") is None
                else round(r["first_attempt_valid"] * 100),
            } for r in rows],
        })
    return tables


def history_view(history: list[dict[str, Any]]) -> list[dict]:
    """Earlier checks, newest first, dated in words."""
    out = []
    for c in history:
        steps = [STEP_LABEL.get(s["step"], s["step"]) for s in c.get("steps", [])]
        out.append({
            "when": date_words(c.get("at")),
            "what": "All steps" if len(steps) > 2 else " and ".join(steps) or "No steps",
            "roles": c.get("answer_key_roles", 0),
            "outcome": c.get("summary", ""),
            "changes": c.get("changes", 0),
        })
    return out
