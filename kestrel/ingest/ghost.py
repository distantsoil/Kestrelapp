"""Ghost-job scoring (SPEC-MODULES.md 6.2). Pure arithmetic over history the ingestion pipeline
already has; it is weak in week one and genuinely good by month three, but capture starts on day
one. Combined into a 0.0-1.0 score stored on the opportunity."""

from __future__ import annotations

from datetime import date, datetime


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _span_days(first: str | None, last: str | None) -> int | None:
    a, b = _to_date(first), _to_date(last)
    if a is None or b is None:
        return None
    return (b - a).days


def compute_ghost_score(
    *,
    seen_count: int | None,
    first_seen_at: str | None,
    last_seen_at: str | None,
    version: int | None,
    has_comp: bool,
    has_location: bool,
    has_team: bool,
    reposted_identical: bool = False,
    req_reappeared: bool = False,
    org_simultaneous: int = 0,
) -> float:
    score = 0.0
    if (seen_count or 0) >= 5:  # strong
        score += 0.3
    span = _span_days(first_seen_at, last_seen_at)
    if span is not None and span > 90 and (version or 1) <= 1:  # live >90d, no description change
        score += 0.3
    if reposted_identical:  # very strong
        score += 0.5
    if req_reappeared:  # very strong
        score += 0.5
    if not has_comp and not has_location and not has_team:  # moderate
        score += 0.2
    if org_simultaneous > 20:  # moderate
        score += 0.2
    return round(min(1.0, score), 3)
