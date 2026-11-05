"""Jobicy Remote Jobs (free, no key): board-wide JSON feed of remote roles with pay (issue #135).

GET https://jobicy.com/api/v2/remote-jobs?count=N[&cursor=...] returns 200 application/json:
  {apiVersion, documentationUrl, friendlyNotice, jobCount, lastUpdate, nextCursor, hasMore,
   appliedFilters, jobs, statusCode, success}
Pagination is cursor-based: pass the previous `nextCursor` back as `cursor` while `hasMore` is true.
Each job carries its own `url` (the page the user applies at), optional salary (salaryMin/Max,
salaryCurrency, salaryPeriod), and `pubDate`. Every posting is remote. 429/5xx are a partial run.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

API_URL = "https://jobicy.com/api/v2/remote-jobs"
SOURCE_NAME = "Jobicy"
SOURCE_URL = "https://jobicy.com"
DEFAULT_COUNT = 100
MAX_COUNT = 500
DEFAULT_MAX_PAGES = 2
MAX_PAGES_LIMIT = 5
_RATE = 1.0

# Jobicy salaryPeriod words -> the RawPosting comp_interval vocabulary (year|month|day|hour).
_PERIOD_MAP = {
    "yearly": "year", "year": "year", "annual": "year",
    "monthly": "month", "month": "month",
    "daily": "day", "day": "day",
    "hourly": "hour", "hour": "hour",
}


def _text(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _number(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _date_only(iso: Any) -> str | None:
    if not isinstance(iso, str):
        return None
    # ISO datetime like "2026-11-03T14:22:29+00:00" -> "2026-11-03"; a bare date passes through.
    return iso[:10] if len(iso) >= 10 and iso[4] == "-" and iso[7] == "-" else (iso or None)


def to_posting(job: dict) -> RawPosting | None:
    """One `jobs` entry as a RawPosting; None for a malformed entry (no id and no link)."""
    if not isinstance(job, dict):
        return None
    raw_id = job.get("id")
    url = _text(job.get("url"))
    slug = _text(job.get("jobSlug"))
    if raw_id is None and not url and not slug:
        return None
    external_id = str(raw_id) if raw_id is not None else (url or slug)

    salary_min = _number(job.get("salaryMin"))
    salary_max = _number(job.get("salaryMax"))
    currency = _text(job.get("salaryCurrency"))
    period = _PERIOD_MAP.get(str(job.get("salaryPeriod") or "").lower())

    def employment(val: Any) -> str | None:
        if isinstance(val, list):
            parts = [p for p in (_text(t) for t in val) if p]
            return ", ".join(parts) if parts else None
        return _text(val)

    return RawPosting(
        external_id=external_id,
        url=url,
        apply_url=url,
        title=_text(job.get("jobTitle")),
        org_name=_text(job.get("companyName")),
        location_raw=_text(job.get("jobGeo")),
        description_html=_text(job.get("jobDescription")) or _text(job.get("jobExcerpt")),
        is_remote=True,  # the feed is a remote-jobs board; every posting is remote
        employment_type=employment(job.get("jobType")),
        comp_min=salary_min,
        comp_max=salary_max,
        comp_currency=currency,
        comp_interval=period,
        date_posted=_date_only(job.get("pubDate")),
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL,
             "jobIndustry": job.get("jobIndustry") or [],
             "jobLevel": job.get("jobLevel") or None,
             "slug": slug},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "Jobicy asked Kestrel to slow down; the rest is fetched on the next check"
    if status >= 500:
        return "Jobicy had a problem on their side; the rest is fetched on the next check"
    if status in (400, 404):
        return "Jobicy rejected the request; the job feed may have moved"
    return f"Jobicy answered with an unexpected status ({status})"


class JobicyAdapter:
    kind = "jobicy"

    def validate_config(self, config: dict) -> None:
        for name, default, limit in (("count", DEFAULT_COUNT, MAX_COUNT),
                                    ("max_pages", DEFAULT_MAX_PAGES, MAX_PAGES_LIMIT)):
            value = config.get(name, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= limit
            ):
                raise SourceConfigError(f"'{name}' must be a whole number from 1 to {limit}")

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        count = config.get("count", DEFAULT_COUNT)
        max_pages = config.get("max_pages", DEFAULT_MAX_PAGES)
        batch = PostingBatch()
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self._get(client, count, cursor, batch)
            if data is None:
                return batch
            for job in data["jobs"]:
                posting = to_posting(job)
                if posting is None or posting.external_id in seen:
                    continue
                seen.add(posting.external_id or "")
                batch.append(posting)
            if not (data.get("hasMore") and data.get("nextCursor")):
                break
            cursor = str(data["nextCursor"])
        return batch

    async def _get(self, client: Any, count: int, cursor: str | None,
                   batch: PostingBatch) -> dict | None:
        url = f"{API_URL}?count={count}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, retries=1, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            log.warning("jobicy_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach Jobicy; try again later") from exc
        if r.status_code != 200:
            log.warning("jobicy_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                batch.partial_error = message
                return None
            raise AdapterError(message)
        try:
            data = r.json()
            if not isinstance(data["jobs"], list):
                raise TypeError("jobs")
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(
                "Jobicy sent a reply Kestrel could not read; the format may have changed"
            ) from exc
        return data
