"""Lever postings adapter (SPEC.md Appendix A, verified live 2026-07-30).

GET https://api.lever.co/v0/postings/{company}?mode=json -> 200, a **bare JSON array** (not an
object with a "jobs" key). Verified against company=ro: 51 postings. An unknown slug returns 404.

GOTCHAS this adapter handles:
  - **The job title is `text`, NOT `title`** — the single most common Lever integration bug.
  - Location is `categories.location`; `categories.commitment` is the employment type;
    `categories.team` is the department.
  - `createdAt` is epoch **milliseconds**; convert to an ISO date string for date_posted, guarding
    against None.
  - `salaryRange` is frequently null; map min/max/currency/interval to comp_* when present.
    Interval strings ("per-year-salary", "per-hour", ...) are mapped best-effort to the canonical
    'year'|'month'|'day'|'hour'; an unknown interval leaves comp_interval None.
  - Prefer `descriptionPlain` for description_md; else fall back to `description` (HTML).
  - `workplaceType` -> remote_hint; is_remote is True only when workplaceType is exactly "remote".
  - Lever has no company_name field, so org_name is the config `company` slug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_BASE = "https://api.lever.co/v0/postings"


class LeverAdapter:
    kind = "lever"

    def validate_config(self, config: dict) -> None:
        if not config.get("company"):
            raise SourceConfigError("lever source requires a 'company' (the Lever slug)")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        company = config["company"]
        url = f"{_BASE}/{company}?mode=json"
        try:
            data = await client.get_json(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise AdapterError(f"Lever slug not found: {company}") from exc
            raise AdapterError(f"Lever fetch failed for {company}: {exc}") from exc
        postings = data if isinstance(data, list) else []
        return [self._to_posting(p, company) for p in postings if isinstance(p, dict)]

    def _to_posting(self, p: dict, company: str) -> RawPosting:
        categories = p.get("categories") if isinstance(p.get("categories"), dict) else {}
        workplace_type = _str(p.get("workplaceType"))

        description_plain = _str(p.get("descriptionPlain"))
        description_html = None if description_plain else _str(p.get("description"))

        comp_min, comp_max, comp_currency, comp_interval = _salary(p.get("salaryRange"))

        return RawPosting(
            external_id=_str(p.get("id")),
            url=_str(p.get("hostedUrl")),
            apply_url=_str(p.get("applyUrl")),
            title=_str(p.get("text")),  # NOT `title` — the Lever gotcha
            org_name=company,  # Lever exposes no company name; the slug stands in
            location_raw=_str(categories.get("location")),
            description_html=description_html,
            description_md=description_plain,  # preferred when present
            employment_type=_str(categories.get("commitment")),
            is_remote=True if workplace_type == "remote" else None,
            remote_hint=workplace_type,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=comp_currency,
            comp_interval=comp_interval,
            date_posted=_epoch_ms_to_iso(p.get("createdAt")),
            raw=p,
        )


def _epoch_ms_to_iso(created_at: Any) -> str | None:
    """Lever's createdAt is epoch milliseconds; return an ISO 8601 UTC string, or None."""
    if not isinstance(created_at, (int, float)):
        return None
    return datetime.fromtimestamp(created_at / 1000, tz=UTC).isoformat()


def _salary(salary_range: Any) -> tuple[float | None, float | None, str | None, str | None]:
    if not isinstance(salary_range, dict):
        return None, None, None, None
    return (
        _num(salary_range.get("min")),
        _num(salary_range.get("max")),
        _str(salary_range.get("currency")),
        _interval(salary_range.get("interval")),
    )


def _interval(value: Any) -> str | None:
    """Best-effort map Lever interval strings ("per-year-salary", "per-hour") to canonical."""
    text = str(value).lower() if value not in (None, "") else ""
    if not text:
        return None
    for needle, canonical in (
        ("hour", "hour"),
        ("year", "year"),
        ("annual", "year"),
        ("month", "month"),
        ("day", "day"),
    ):
        if needle in text:
            return canonical
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
