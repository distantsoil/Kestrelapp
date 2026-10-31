"""Needs another try: the words a failed role is shown in (D-UI-104). A quarantine row stores a
developer's error string; this turns it into the plain reason, the step that failed, a short
"when" and the tries so far. Pure functions, no I/O, so the template tests can call them."""

from __future__ import annotations

import re
from datetime import UTC, datetime

# The three scoring steps in the operator's words (tier 1 screen, tier 2 review, tier 3 read).
# Anything else (research, interview and the like) shows under All only, named by its own step
# (D-UI-224); an unknown tier reads "Other step".
STEPS: dict[int, tuple[str, str]] = {
    1: ("quick", "Quick sort"),
    2: ("fit", "Fit score"),
    3: ("deep", "Deep read"),
}
OTHER_STEP = ("other", "Other step")
OTHER_LABELS: dict[int, str] = {
    4: "Resume import",
    5: "Company research",
    6: "Contacts",
    8: "Story bank",
    9: "Mock interview",
    13: "Resume review",
    14: "Interview questions",
    15: "Job title ideas",
    16: "Company summary",
}
# Where a failure with no role runs again (D-UI-224): (page name, link, what to press there).
PLACES: dict[int, tuple[str, str, str]] = {
    15: ("Profile", "/profile?tab=employment", "Suggest again"),
}
FILTERS = (("all", "All"), ("quick", "Quick sort"), ("fit", "Fit score"), ("deep", "Deep read"))

REASON_FALLBACK = "Scoring did not finish"
HELD_REASON = "Waiting for you to choose your current employer"

# Checked in this order: a loop's message also names the max_tokens it ran into, and the loop
# is the truer reason; a refused key also says "http 4". Plain words, no jargon, no model names.
_REASONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("generation loop", "degenerate", "repeated"), "The AI repeated itself"),
    (("finish_reason: length", "finish_reason length", "max_tokens", "hit the cap"),
     "The AI ran out of room"),
    ((r"\b40[13]\b", "unauthorized", "invalid_api_key", "api key", "was rejected"),
     "Online service refused the key"),
    ((r"\b429\b", "rate limit"), "Online service asked Kestrel to slow down"),
    ((r"(http|status)\s*4\d\d\b",), "Online service turned the request down"),
    (("timeout", "timed out", "no answer after"), "No answer in time"),
    (("connecterror", "connection", "unreachable", r"(http|status)\s*5\d\d\b"),
     "Could not reach the AI"),
    (("schema", "validation", "invalid json", "json", "field required", "not valid", r"\benum\b"),
     "The AI’s answer couldn’t be read"),
)


def step_for(tier: int | None) -> tuple[str, str]:
    """(filter key, label) for the tier that failed."""
    t = int(tier or 0)
    if t in STEPS:
        return STEPS[t]
    return (OTHER_STEP[0], OTHER_LABELS.get(t, OTHER_STEP[1]))


def place_for(tier: int | None) -> tuple[str, str, str] | None:
    """Where the step behind a failure with no role runs again, or None when it has no one page."""
    return PLACES.get(int(tier or 0))


def is_held(error: str | None) -> bool:
    """True for a send Kestrel held back (nothing was sent): confidential mode is on, an online
    model was picked and no current employer is chosen to hide (pipeline._shared.HOSTED_REFUSAL).
    That is a wait for the operator's choice, not a failed try."""
    return "hosted send refused" in (error or "").lower()


def plain_reason(error: str | None) -> str:
    """The failure in a few plain words. Never empty, never the raw error text."""
    if is_held(error):
        return HELD_REASON
    low = (error or "").lower()
    for needles, words in _REASONS:
        if any(re.search(n, low) for n in needles):
            return words
    return REASON_FALLBACK


def refused_key(error: str | None) -> bool:
    """True when the online service refused the key, so the row can point at Models."""
    return plain_reason(error) == "Online service refused the key"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def when_words(ts: str | None, now: datetime | None = None) -> str:
    """"9:41 AM" today, "Yesterday", the weekday within the last week, else "Oct 14", on the
    operator's clock (no platform-specific strftime flags)."""
    dt = _parse(ts)
    if dt is None:
        return ""
    local = dt.astimezone()
    today = (now or datetime.now(UTC)).astimezone().date()
    days = (today - local.date()).days
    from kestrel.date_display import format_display

    if days <= 0:
        return format_display(local, kind="time")
    if days == 1:
        return "Yesterday"
    if days < 7:
        return format_display(local, "weekday")
    return format_display(local, "short")


def tries_words(attempts: int | None) -> str:
    n = max(int(attempts or 0), 1)
    return f"{n} {'try' if n == 1 else 'tries'}"
