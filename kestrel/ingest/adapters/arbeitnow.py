"""Arbeitnow (free, no key): a board-wide JSON API of jobs, remote and in person (issue #134).

`GET https://www.arbeitnow.com/api/job-board-api?page=N` (300 jobs a page; free, no key). Reply:
`{"data": [...], "links": {"next": <url or null>}, "meta": {...}}`, `links.next` null on the last
page. Each job: `slug`, `company_name`, `title`, `description` (HTML), `remote` (bool), `url`,
`tags`, `job_types`, `location`, `created_at` (epoch seconds). No compensation fields.

Verified live 2026-11-03 (shape, 300 a page, `links.next`) and `robots.txt` (allows the API).
Their terms ask that requests stay infrequent and send a descriptive User-Agent, so Kestrel sends
at most one request a second, at most `max_pages` requests a check (default 2), stops at
`links.next` null, and keeps the posting's own `url` so the operator applies there themselves. A
429 or 5xx makes the run partial; it never aborts the batch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"
SOURCE_NAME = "Arbeitnow"
SOURCE_URL = "https://www.arbeitnow.com"
PER_PAGE = 300
DEFAULT_MAX_PAGES = 2
MAX_PAGES_LIMIT = 5
_RATE = 1.0


def _text(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _bool(val: Any) -> bool | None:
    return val if isinstance(val, bool) else None


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def _employment(types: Any) -> str | None:
    if isinstance(types, list):
        parts = [p for p in (_text(t) for t in types) if p]
        return ", ".join(parts) if parts else None
    return _text(types)


def to_posting(job: dict) -> RawPosting | None:
    """One `data` entry as a RawPosting; None for a malformed entry (no id and no link)."""
    if not isinstance(job, dict):
        return None
    slug, url = _text(job.get("slug")), _text(job.get("url"))
    if not slug and not url:
        return None
    return RawPosting(
        external_id=slug or url,
        url=url,
        title=_text(job.get("title")),
        org_name=_text(job.get("company_name")),
        location_raw=_text(job.get("location")),
        description_html=_text(job.get("description")),
        is_remote=_bool(job.get("remote")),
        employment_type=_employment(job.get("job_types")),
        date_posted=_iso(job.get("created_at")),
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL,
             "tags": job.get("tags") or []},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "Arbeitnow asked Kestrel to slow down; the rest is fetched on the next check"
    if status >= 500:
        return "Arbeitnow had a problem on their side; the rest is fetched on the next check"
    if status in (400, 404):
        return "Arbeitnow rejected the request; the job board may have moved"
    return f"Arbeitnow answered with an unexpected status ({status})"


class ArbeitnowAdapter:
    kind = "arbeitnow"

    def validate_config(self, config: dict) -> None:
        value = config.get("max_pages", DEFAULT_MAX_PAGES)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_PAGES_LIMIT
        ):
            raise SourceConfigError(
                f"'max_pages' must be a whole number from 1 to {MAX_PAGES_LIMIT}"
            )

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        max_pages = config.get("max_pages", DEFAULT_MAX_PAGES)
        batch = PostingBatch()
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            data = await self._get(client, page, batch)
            if data is None:
                return batch
            jobs = data["data"]
            for job in jobs:
                posting = to_posting(job)
                if posting is None or posting.external_id in seen:
                    continue
                seen.add(posting.external_id or "")
                batch.append(posting)
            links = data.get("links")
            if not (isinstance(links, dict) and links.get("next")) or len(jobs) < PER_PAGE:
                break
        return batch

    async def _get(self, client: Any, page: int, batch: PostingBatch) -> dict | None:
        url = f"{API_URL}?page={page}"
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, retries=1, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            log.warning("arbeitnow_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach Arbeitnow; try again later") from exc
        if r.status_code != 200:
            log.warning("arbeitnow_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                batch.partial_error = message
                return None
            raise AdapterError(message)
        try:
            data = r.json()
            if not isinstance(data["data"], list):
                raise TypeError("data")
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(
                "Arbeitnow sent a reply Kestrel could not read; the format may have changed"
            ) from exc
        return data
