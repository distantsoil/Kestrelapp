"""The near-floor margin on the pay and day-rate floors (D-RULES-2, hard rule 4).

A role whose posted maximum sits a little under the operator's floor is kept, and tagged, rather
than rejected: a strong match should not be lost over a small gap, and a wrongly rejected role is
invisible. The margin is chosen from fixed stops. A role with no posted pay never reaches these
functions' "below" branch (the comparison helpers return False for a missing number)."""

from __future__ import annotations

from typing import Any

# Stored percent values for Exact, 2, 5, 10 and 15 percent. 0 means exact.
MARGIN_STOPS: tuple[int, ...] = (0, 2, 5, 10, 15)
DEFAULT_MARGIN_PCT = 5

# The tag a kept near-floor role carries (the soft signal id is in `SOFT_ID`, written to the
# role's stage_reason, from which `tags_from_stage_reason` reads it back).
NEAR_FLOOR_TAG = "Below your minimum"
SOFT_ID = "near_floor"


def normalize_margin(pct: Any) -> int:
    """A stored margin as one of the stops. A missing, non-numeric or off-stop value reads as
    the default, never as an error (a hand-edited profile must not break Tier 0)."""
    if isinstance(pct, bool):
        return DEFAULT_MARGIN_PCT
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return DEFAULT_MARGIN_PCT
    return int(value) if value in MARGIN_STOPS else DEFAULT_MARGIN_PCT


def margin_floor(floor: Any, pct: Any) -> float | None:
    """The lowest posted maximum that still passes: `floor` less the margin. None when there is
    no floor (nothing to enforce)."""
    if floor is None or isinstance(floor, bool):
        return None
    try:
        base = float(floor)
    except (TypeError, ValueError):
        return None
    return base * (100 - normalize_margin(pct)) / 100.0


def posted_comp_max(opp: Any) -> Any:
    """The pay ceiling the posting itself states, or None. A job-board estimate (comp_source
    "inferred", e.g. Glassdoor) is not posted pay, so it never decides a pay rule: uncertainty
    escalates, never rejects (hard rule 4)."""
    if getattr(opp, "comp_source", None) == "inferred":
        return None
    return getattr(opp, "comp_max", None)


def comp_max_near_floor(opp: Any, floor: Any, pct: Any) -> bool:
    """True when the posted maximum is under the floor but inside the margin, so the role is
    kept and tagged. False for no posted pay, no floor, or exact mode (margin 0)."""
    cap = posted_comp_max(opp)
    if cap is None or floor is None:
        return False
    low = margin_floor(floor, pct)
    if low is None:
        return False
    try:
        return low <= float(cap) < float(floor)
    except (TypeError, ValueError):
        return False


def tags_from_stage_reason(stage_reason: str | None) -> list[str]:
    """The operator-facing tags a Tier 0 pass left on a role, read from its stage_reason."""
    text = stage_reason or ""
    return [NEAR_FLOOR_TAG] if SOFT_ID in _soft_ids(text) else []


def _soft_ids(stage_reason: str) -> set[str]:
    head = stage_reason.split(";")[0]
    if not head.startswith("soft:"):
        return set()
    return {part.strip() for part in head[len("soft:"):].split(",") if part.strip()}
