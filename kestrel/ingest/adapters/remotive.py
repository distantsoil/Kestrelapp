"""Remotive remote jobs adapter (SPEC.md Appendix A, verified live 2026-11-03; D-SRC-15).

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

Rules that bind this module (owner, D-SRC-15, 2026-11-04):
  - NAMED EXCEPTION to the robots.txt rule (hard rule 7), the same shape as RemoteRocketship
    (D-SRC-14): Remotive's robots.txt has `Disallow: /api/*`, a crawler rule, but Remotive
    publishes this API for scripts and its legal notice sets the terms. No robots check is made
    on this one documented API call, because it would refuse every read. The exception never
    covers Remotive's pages; the posting links are only stored, never fetched.
  - At most 4 fetches per local day, enforced by the runner from a count stored in the database
    (`daily_fetch_cap`, kestrel.ingest.fetch_cap), so a restart cannot reset it. A 5th is
    refused with a partial result and `CAP_SENTENCE`.
  - Each job records Remotive as its source (`raw["via"]`, `raw["via_url"]`) and keeps Remotive's
    own posting link as `url`, where the person applies.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

_BASE = "https://remotive.com/api/remote-jobs"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_RATE = 1.0  # requests/second
DAILY_FETCH_CAP = 4
SOURCE_NAME = "Remotive"
SOURCE_URL = "https://remotive.com"
CAP_SENTENCE = (
    "Remotive asks apps to fetch at most 4 times a day, and today's 4 are used. "
    "Kestrel will fetch again tomorrow."
)


class RemotiveAdapter:
    kind = "remotive"
    daily_fetch_cap = DAILY_FETCH_CAP  # enforced by the runner, from the stored count
    cap_sentence = CAP_SENTENCE

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
            # D-SRC-15: no robots check; it would refuse this documented API (see the docstring).
            r = await client.request("GET", url, rate_per_sec=_RATE, retries=1)
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except httpx.HTTPError as exc:
            log.warning("remotive_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach Remotive; try again later") from exc

        if r.status_code != 200:
            log.warning("remotive_error", status=r.status_code)
            message = _problem(r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                batch = PostingBatch()
                batch.partial_error = message
                return batch
            raise AdapterError(message)

        try:
            data = r.json()
            jobs = data["jobs"]
            if not isinstance(jobs, list):
                raise TypeError("jobs")
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("remotive_bad_shape", error=type(exc).__name__)
            batch = PostingBatch()
            batch.partial_error = (
                "Remotive sent a reply Kestrel could not read; the format may have changed."
            )
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


def _problem(status: int) -> str:
    if status in (403, 429):
        return (
            "Remotive's rate limit was reached (it advises at most 4 fetches a day); "
            "the next check tries again"
        )
    if status >= 500:
        return "Remotive had a problem on their side; the next check tries again"
    if status in (400, 404):
        return "Remotive rejected the request; the API may have moved"
    return f"Remotive answered with an unexpected status ({status})"


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
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL},
    )


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
