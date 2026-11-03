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
fetches at most `limit` jobs per run (default 50) at most 1 request/second.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from kestrel.ingest.base import PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

_BASE = "https://remotive.com/api/remote-jobs"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_RATE = 1.0  # requests/second


class RemotiveAdapter:
    kind = "remotive"

    def validate_config(self, config: dict) -> None:
        limit = config.get("limit", _DEFAULT_LIMIT)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_LIMIT
        ):
            raise SourceConfigError(
                f"'limit' must be a whole number from 1 to {_MAX_LIMIT}"
            )

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        limit = config.get("limit", _DEFAULT_LIMIT)
        url = f"{_BASE}?limit={limit}"
        log.info("remotive_fetch", url=url, limit=limit)

        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, retries=1, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError:
            batch = PostingBatch()
            batch.partial_error = "Remotive's robots.txt disallows this read."
            return batch
        except httpx.HTTPError as exc:
            log.warning("remotive_unreachable", error=type(exc).__name__)
            batch = PostingBatch()
            batch.partial_error = "Could not reach Remotive; try again later."
            return batch

        if r.status_code == 429:
            log.warning("remotive_rate_limited")
            batch = PostingBatch()
            batch.partial_error = (
                "Remotive rate limit reached (the API advises at most 4 fetches a day)."
            )
            return batch
        if r.status_code != 200:
            log.warning("remotive_error", status=r.status_code)
            batch = PostingBatch()
            batch.partial_error = f"Remotive returned HTTP {r.status_code}."
            return batch

        data = r.json()
        jobs = data.get("jobs", [])
        if not isinstance(jobs, list):
            log.warning("remotive_bad_shape", type=type(jobs).__name__)
            batch = PostingBatch()
            batch.partial_error = "Remotive response did not include a jobs list."
            return batch

        batch = PostingBatch()
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            posting = _map_job(job)
            if posting is None:
                continue
            key = posting.external_id or ""
            if key in seen:
                continue
            seen.add(key)
            batch.append(posting)

        log.info("remotive_done", count=len(batch))
        return batch


def _map_job(job: dict[str, Any]) -> RawPosting | None:
    title = _str(job.get("title"))
    url = _str(job.get("url"))
    if not title or not url:
        return None

    company = _str(job.get("company_name"))
    location = _str(job.get("candidate_required_location"))
    description = job.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    # Use the job id for dedup; fall back to the url if id is missing.
    raw_id = job.get("id")
    external_id = str(raw_id) if raw_id is not None else url

    return RawPosting(
        external_id=external_id,
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
