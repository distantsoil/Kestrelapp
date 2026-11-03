"""Remotive remote jobs adapter (SPEC.md Appendix A, verified live 2026-11-03).

Board-wide source for Remotive's public remote jobs API. The API is keyless and
returns a JSON envelope with `00-warning`, `0-legal-notice`, `job-count`,
`total-job-count`, and `jobs`. Each job has `id`, `url`, `title`,
`company_name`, `company_logo`, `category`, `tags`, `job_type`,
`publication_date`, `candidate_required_location`, `salary` (free text), and
`description` (HTML).

Terms (confirmed 2026-11-03): the API permits developers to "share our jobs
further," requires linking back to Remotive and naming it as the source,
prohibits submitting to third-party job sites, prohibits using the data to
collect signups/email addresses, and advises at most 4 fetches per day.

Kestrel enforces the 4-per-day cap in the scheduler (not here); this adapter
fetches at most `max_pages` pages per run (default 1) and at most 1
request/second.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

_BASE = "https://remotive.com/api/remote-jobs"
_DEFAULT_LIMIT = 50
_MAX_PAGES = 3  # at most 3 pages per run (50 jobs/page = 150 jobs max)
_RATE = 1.0  # requests/second


class RemotiveAdapter:
    kind = "remotive"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        limit = self.config.get("limit", _DEFAULT_LIMIT)
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise SourceConfigError("Remotive: 'limit' must be an integer 1-100")
        self.limit = limit
        self.max_pages = min(int(self.config.get("max_pages", _MAX_PAGES)), _MAX_PAGES)

    async def run(self, http: httpx.AsyncClient) -> list[RawPosting]:
        url = f"{_BASE}?limit={self.limit}"
        log.info("remotive_fetch", url=url, limit=self.limit)
        try:
            resp = await http.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise AdapterError("Remotive rate limit reached") from exc
            raise AdapterError(f"Remotive fetch failed: {exc}") from exc

        data = resp.json()
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            raise AdapterError("Remotive: 'jobs' is not a list")

        postings: list[RawPosting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            posting = _map_job(job)
            if posting:
                postings.append(posting)

        log.info("remotive_done", count=len(postings))
        return postings


def _map_job(job: dict[str, Any]) -> RawPosting | None:
    title = _str(job.get("title"))
    url = _str(job.get("url"))
    if not title or not url:
        return None

    company = _str(job.get("company_name"))
    location = _str(job.get("candidate_required_location"))
    description = _strip_html(job.get("description"))

    return RawPosting(
        external_id=str(job.get("id", "")),
        title=title,
        org_name=company,
        location_raw=location,
        url=url,
        description_html=description,
        is_remote=True,
        date_posted=_str(job.get("publication_date")),
    )


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _strip_html(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Strip HTML tags (simple regex, not a full parser)
    text = re.sub(r"<[^>]+>", "", value)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#x27;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return text.strip() or None
