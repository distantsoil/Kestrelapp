"""What the bell panel shows for a notification row (D-UI-94, D-UI-152): a human severity, a plain
relative time, a day group, one context action chosen by kind, and whether the problem is still
open. Pure functions over a row and a clock, so the tests need no database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kestrel.services.updater import DOWNLOAD_PAGE

TABS = ("all", "attention")

# A raw severity is never shown (D-UI-94): (word, pill tone, needs attention?).
SEVERITY: dict[str, tuple[str, str, bool]] = {
    "info": ("For your information", "info", False),
    "success": ("Good news", "good", False),
    "warning": ("Needs attention", "warn", True),
    "error": ("Problem", "bad", True),
}

# Kinds that follow a pursuit: once its next action is no longer due, or it has ended, the
# problem is resolved without anyone pressing anything (D-UI-94: "automatic when the underlying
# condition clears").
PURSUIT_KINDS = ("next_action_due", "ghosting")


def human_severity(severity: str | None) -> tuple[str, str, bool]:
    return SEVERITY.get(severity or "info", SEVERITY["info"])


def context_action(
    kind: str, opportunity_id: int | None, *, follow_up: bool = False,
) -> dict[str, Any] | None:
    """One action by kind: {"label", "href", "external"} or None. A due follow-up opens
    Applications with its draft dialog when the opportunity still has a due follow-up."""
    role = f"/opportunities/{opportunity_id}" if opportunity_id else "/opportunities"
    table: dict[str, tuple[str, str, bool]] = {
        "next_action_due": (
            "Draft follow-up", f"/pursuits?follow_up={opportunity_id}", False,
        ) if opportunity_id and follow_up else ("Open in Applications", "/pursuits", False),
        "ghosting": ("Open in Applications", "/pursuits", False),
        "source_disabled": ("Open Sources", "/sources", False),
        "liveness_closed": ("Go to job detail", role, False),
        "high_score": ("Go to job detail", role, False),
        "strong_match": ("Go to job detail", role, False),
        "run_finished": ("View run details", "/runs", False),
        "run_done": ("View run details", "/runs", False),
        "scan_done": ("Go to results", "/opportunities?new_today=1", False),
        "quarantine": ("Open Needs another try", "/quarantine", False),
        "reranker_ready": ("Open Calibrate", "/calibrate", False),
        "reranker_refused": ("Open Calibrate", "/calibrate", False),
        "reranker_drift": ("Open Calibrate", "/calibrate", False),
        "calibration_ready": ("Open Calibrate", "/calibrate", False),
        "credit_low": ("Open Models", "/models", False),
        "model_check": ("Open Models", "/models", False),
        "update": ("Get it from kestrelapp.co", DOWNLOAD_PAGE, True),
    }
    found = table.get(kind)
    if found is None:
        return None
    label, href, external = found
    return {"label": label, "href": href, "external": external}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def relative_time(value: str | None, now: datetime) -> str:
    """'just now', '5 minutes ago', '3 hours ago', 'Yesterday', '4 days ago', or a short date."""
    when = parse_iso(value)
    if when is None:
        return ""
    secs = max(0, int((now - when).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        n = secs // 60
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if secs < 86400:
        n = secs // 3600
        return f"{n} hour{'s' if n != 1 else ''} ago"
    days = (now.date() - when.date()).days
    if days <= 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    from kestrel.date_display import format_display

    return format_display(when, "short")


def day_group(value: str | None, now: datetime) -> str:
    """Today, Yesterday or Earlier (local calendar days, the same clock as the stamp)."""
    when = parse_iso(value)
    if when is None:
        return "Earlier"
    days = (now.date() - when.date()).days
    return "Today" if days <= 0 else "Yesterday" if days == 1 else "Earlier"


def is_open_problem(severity: str | None, resolved_at: str | None) -> bool:
    return human_severity(severity)[2] and not resolved_at
