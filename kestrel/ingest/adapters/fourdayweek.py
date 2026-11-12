"""4 Day Week (free, no key): board of employers with four-day or reduced-hour schedules.

GET https://4dayweek.io/api/v2/jobs?page=N&limit=M[&schedule=...][&q=...] returns 200 JSON:
  {data: [...], page, limit, total, has_more}
This is the site's documented public API (https://4dayweek.io/developers); its terms forbid other
automated access and ask that the board is credited with a link, and its robots.txt allows /api/v2.
Each job carries its full Markdown `description`, its posting page `url`, a work arrangement,
locations, and pay in CENTS when the employer gave it. 60 requests a minute is the published limit;
Kestrel sends far fewer. 429/5xx are a partial run.
"""

from __future__ import annotations

import math
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

API_URL = "https://4dayweek.io/api/v2/jobs"
SOURCE_NAME = "4 Day Week"
SOURCE_URL = "https://4dayweek.io"
DEFAULT_COUNT = 100
MAX_COUNT = 100  # the documented maximum for `limit`
DEFAULT_MAX_PAGES = 2
MAX_PAGES_LIMIT = 5
MAX_KEYWORD = 100
_RATE = 1.0

# A filter value in the documented API: lower-case words, digits, underscores, hyphens; a
# comma-separated list of them. It is checked with fullmatch before it reaches a URL.
_FILTER = re.compile(r"[a-z0-9_-]{1,40}(,[a-z0-9_-]{1,40}){0,9}")
_FILTERS = ("schedule", "category", "level", "work_arrangement")

_PERIOD_MAP = {
    "year": "year", "month": "month", "week": "week", "day": "day", "hour": "hour",
    "annual": "year", "annually": "year", "yearly": "year", "monthly": "month",
    "weekly": "week", "daily": "day", "hourly": "hour",
}
_REMOTE = {"remote": True, "onsite": False, "hybrid": False}


def _text(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _cents(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
        return None
    try:
        out = val / 100.0  # the API states pay in cents (8000000 = 80,000)
    except OverflowError:  # an integer too large for a float
        return None
    return out if math.isfinite(out) else None


def _iso(val: Any) -> datetime | None:
    if not isinstance(val, str) or len(val) < 10:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def _location(locations: Any) -> str | None:
    """The primary location as "City, State, Country"; a remote-only job names its country."""
    if not isinstance(locations, list):
        return None
    rows = [x for x in locations if isinstance(x, dict)]
    rows.sort(key=lambda x: not x.get("is_primary"))
    for row in rows:
        parts = [p for p in (_text(row.get("city")), _text(row.get("state")),
                             _text(row.get("country"))) if p]
        if parts:
            return ", ".join(parts)
    return None


def to_posting(job: dict, now: float | None = None) -> RawPosting | None:
    """One `data` entry as a RawPosting; None for a malformed entry (no id and no link) or a job
    past its `expires_at`."""
    if not isinstance(job, dict):
        return None
    slug = _text(job.get("slug"))
    url = _text(job.get("url"))
    raw_id = _text(job.get("id")) or slug
    if not raw_id or not (url or slug):
        return None
    expires = _iso(job.get("expires_at"))
    try:
        if expires is not None and expires.timestamp() < (time.time() if now is None else now):
            return None
    except (ValueError, OverflowError, OSError):
        pass  # a "never" sentinel such as year 1: no usable expiry, so the posting is kept
    found = job.get("company")
    company: dict[str, Any] = found if isinstance(found, dict) else {}
    arrangement = _text(job.get("work_arrangement"))
    given = str(job.get("salary_period") or "").strip().lower()
    period = _PERIOD_MAP.get(given)
    lo, hi = _cents(job.get("salary_min")), _cents(job.get("salary_max"))
    if given and period is None:
        lo = hi = None  # a pay period Kestrel does not know: no pay, never a guessed "per year"
    posted = _iso(job.get("posted_at"))
    link = url or f"{SOURCE_URL}/job/{slug}"
    return RawPosting(
        external_id=raw_id,
        url=link,
        apply_url=link,
        title=_text(job.get("title")),
        org_name=_text(company.get("name")),
        org_url=_text(company.get("website")),
        location_raw=_location(job.get("locations")),
        description_md=_text(job.get("description")),
        is_remote=_REMOTE.get((arrangement or "").lower()),
        remote_hint=arrangement,
        employment_type=_text(job.get("contract_type")),
        comp_min=lo,
        comp_max=hi,
        comp_currency=_text(job.get("salary_currency")) if (lo or hi) else None,
        comp_interval=period if (lo or hi) else None,
        date_posted=posted.date().isoformat() if posted else None,
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL,
             "schedule_type": _text(job.get("schedule_type")),
             "hours_per_week_min": job.get("hours_per_week_min"),
             "hours_per_week_max": job.get("hours_per_week_max"),
             "category": _text(job.get("category")),
             "level": _text(job.get("level")),
             "slug": slug},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "4 Day Week asked Kestrel to slow down; the rest is fetched on the next check"
    if status >= 500:
        return "4 Day Week had a problem on their side; the rest is fetched on the next check"
    if status in (400, 404):
        return "4 Day Week rejected the request; the job feed may have moved"
    return f"4 Day Week answered with an unexpected status ({status})"


class FourDayWeekAdapter:
    kind = "fourdayweek"

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
        for name in _FILTERS:
            value = config.get(name)
            if value is not None and not (
                isinstance(value, str) and _FILTER.fullmatch(value)
            ):
                raise SourceConfigError(
                    f"'{name}' must be lower-case words separated by commas, such as "
                    "'4_day_week,9_day_fortnight'"
                )
        keyword = config.get("keyword")
        if keyword is not None and not (
            isinstance(keyword, str) and len(keyword) <= MAX_KEYWORD
        ):
            raise SourceConfigError(f"'keyword' must be text of at most {MAX_KEYWORD} characters")

    def _query(self, config: dict, page: int) -> str:
        params = {"page": page, "limit": config.get("count", DEFAULT_COUNT)}
        for name in _FILTERS:
            if config.get(name):
                params[name] = config[name]
        if str(config.get("keyword") or "").strip():
            params["q"] = str(config["keyword"]).strip()
        return urllib.parse.urlencode(params, safe=",")

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        max_pages = config.get("max_pages", DEFAULT_MAX_PAGES)
        batch = PostingBatch()
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            data = await self._get(client, f"{API_URL}?{self._query(config, page)}", batch)
            if data is None:
                return batch
            for job in data["data"]:
                try:
                    posting = to_posting(job)
                except (ValueError, TypeError, OverflowError, OSError, AttributeError) as exc:
                    log.warning("fourdayweek_record_skipped", error=type(exc).__name__)
                    continue  # one odd record must not lose the other ninety-nine
                if posting is None or posting.external_id in seen:
                    continue
                seen.add(posting.external_id or "")
                batch.append(posting)
            if not data.get("has_more"):
                break
        return batch

    async def _get(self, client: Any, url: str, batch: PostingBatch) -> dict | None:
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, retries=1, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            log.warning("fourdayweek_unreachable", error=type(exc).__name__)
            if batch:  # a later page: keep what the earlier pages gave
                batch.partial_error = (
                    "Could not reach 4 Day Week; the rest is fetched on the next check")
                return None
            raise AdapterError("Could not reach 4 Day Week; try again later") from exc
        if r.status_code != 200:
            log.warning("fourdayweek_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                batch.partial_error = message
                return None
            if batch:  # a later page: keep what the earlier pages gave
                batch.partial_error = message
                return None
            raise AdapterError(message)
        try:
            data = r.json()
            if not isinstance(data["data"], list):
                raise TypeError("data")
        except (ValueError, KeyError, TypeError) as exc:
            unreadable = (
                "4 Day Week sent a reply Kestrel could not read; the format may have changed")
            if batch:
                batch.partial_error = unreadable
                return None
            raise AdapterError(unreadable) from exc
        return data
