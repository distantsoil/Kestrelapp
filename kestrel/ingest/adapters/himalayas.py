"""Himalayas remote jobs board (SPEC.md 5.1 and Appendix A, D-SRC-15, step B3). No key.

`GET https://himalayas.app/jobs/api/search?q=<words>&country=US&sort=recent&page=N` (documented at
himalayas.app/docs/remote-jobs-api; verified live 2026-10-29 for the shape). Twenty jobs a page.
`country=US` returns the roles open to applicants in the US, worldwide roles included (Region is US
only, D-UI-109); the filter is the board's own, so every role comes back already open to the user.

Himalayas' terms for API users (himalayas.app/terms, clauses 113 and 30A) allow this use, a personal
integration in an app the user runs, on three conditions this adapter keeps:
  (a) the apply link is the API's `applicationLink`, never a direct employer link: `apply_url` is
      exactly that, and `url` is the Himalayas job page (`guid`);
  (b) Himalayas is named as the source, with a link: the source is named "<search> on Himalayas",
      so the posting tab reads "Posted on <search> on Himalayas" beside the Himalayas link;
  (c)/(d) jobs are shown only in the user's own app, and a job past its `expiryDate` is dropped.
Kestrel requests only the API (never the job pages, which Himalayas protects against automation),
at 1 request a second, at most MAX_REQUESTS a run; the data refreshes daily, so the default check is
daily. robots.txt allows /jobs/api and is checked. A 429 or 5xx is a `partial` run that keeps what
was collected; a refusal is an AdapterError in Kestrel's own words.

Mapping: `locationRestrictions` containing the United States reads "United States"; empty
(worldwide) reads "Remote (worldwide)"; anything else is passed on as written so the D-SRC-13 area
check can judge it. Pay carries its `currency` and `salaryPeriod` (hourly, weekly, fortnightly,
monthly, annual); a salary with no currency is not guessed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.pipeline.profile import load_profile

log = structlog.get_logger(__name__)

API_URL = "https://himalayas.app/jobs/api/search"
SOURCE_NAME = "Himalayas"
SOURCE_URL = "https://himalayas.app"
PER_PAGE = 20
DEFAULT_MAX_PAGES = 3
MAX_PAGES_LIMIT = 10
MAX_TITLES = 5
MAX_REQUESTS = 12
_RATE = 1.0

_PERIOD = {  # salaryPeriod -> (interval, divisor to that interval)
    "annual": ("year", 1), "monthly": ("month", 1), "weekly": ("week", 1),
    "fortnightly": ("week", 2), "hourly": ("hour", 1),
}


def _profile_titles() -> list[str]:
    try:
        prof = load_profile("employment")
    except (OSError, ValueError, AttributeError):
        return []
    terms = getattr(prof, "employment_terms", None) if prof is not None else None
    raw = getattr(terms, "target_titles", None) or []
    titles = [" ".join(str(t).split()) for t in raw if str(t).strip()]
    return titles[:MAX_TITLES]


def _text(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _num(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val) if val > 0 else None


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def _location(job: dict) -> str:
    places = [p for p in (_text(x) for x in (job.get("locationRestrictions") or [])) if p]
    if not places:
        return "Remote (worldwide)"
    if any(p.lower() in ("united states", "usa", "us") for p in places):
        return "United States"
    return places[0]


def _pay(job: dict) -> tuple[float | None, float | None, str | None, str | None]:
    lo, hi = _num(job.get("minSalary")), _num(job.get("maxSalary"))
    currency = _text(job.get("currency"))
    interval, div = _PERIOD.get(str(job.get("salaryPeriod") or "annual").lower(), (None, 1))
    if not (lo or hi) or not currency or interval is None:
        return None, None, None, None
    lo, hi = (lo or hi) / div, (hi or lo) / div
    return lo, hi, currency.upper(), interval


def to_posting(job: dict, *, now: float | None = None) -> RawPosting | None:
    """One `jobs` entry as a RawPosting; None for a malformed entry or one past its expiry."""
    if not isinstance(job, dict):
        return None
    title, guid = _text(job.get("title")), _text(job.get("guid"))
    apply_link = _text(job.get("applicationLink"))
    if not title and not guid:
        return None
    expiry = job.get("expiryDate")
    if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry > 0:
        if expiry < (time.time() if now is None else now):
            return None  # the terms ask that expired jobs are removed
    lo, hi, cur, interval = _pay(job)
    return RawPosting(
        external_id=guid or apply_link,
        url=guid or apply_link,
        apply_url=apply_link or guid,  # the terms: Apply always uses the API's applicationLink
        title=title,
        org_name=_text(job.get("companyName")),
        location_raw=_location(job),
        description_html=_text(job.get("description")),
        employment_type=_text(job.get("employmentType")),
        is_remote=True,
        remote_hint="Remote",
        comp_min=lo, comp_max=hi, comp_currency=cur, comp_interval=interval,
        date_posted=_iso(job.get("pubDate")),
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL,
             "seniority": job.get("seniority") or [],
             "closes": _iso(job.get("expiryDate"))},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "Himalayas asked Kestrel to slow down; the rest is fetched on the next check"
    if status >= 500:
        return "Himalayas had a problem on their side; the rest is fetched on the next check"
    if status in (400, 404):
        return "Himalayas rejected the search. Check the search words"
    return f"Himalayas answered with an unexpected status ({status})"


class HimalayasAdapter:
    kind = "himalayas"

    def validate_config(self, config: dict) -> None:
        if config.get("keyword") is not None and not isinstance(config["keyword"], str):
            raise SourceConfigError("'keyword' must be text")
        value = config.get("max_pages", DEFAULT_MAX_PAGES)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (
                MAX_PAGES_LIMIT):
            raise SourceConfigError(
                f"'max_pages' must be a whole number from 1 to {MAX_PAGES_LIMIT}"
            )

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        batch = PostingBatch()
        keyword = (config.get("keyword") or "").strip()
        terms: list[str | None] = [keyword] if keyword else list(_profile_titles()) or [None]
        max_pages = config.get("max_pages", DEFAULT_MAX_PAGES)
        seen: set[str] = set()
        requests = 0
        for term in terms:
            for page in range(1, max_pages + 1):
                if requests >= MAX_REQUESTS:
                    batch.partial_error = batch.partial_error or (
                        "Himalayas: stopped at the limit of requests per check; the rest is "
                        "fetched on the next check"
                    )
                    return batch
                requests += 1
                params = {"country": "US", "sort": "recent", "page": str(page)}
                if term:
                    params["q"] = term
                data = await self._get(client, params, batch)
                if data is None:
                    return batch
                jobs = data["jobs"]
                for job in jobs:
                    posting = to_posting(job)
                    if posting is None or posting.external_id in seen:
                        continue
                    seen.add(posting.external_id or "")
                    batch.append(posting)
                if len(jobs) < PER_PAGE:
                    break
        return batch

    async def _get(self, client: Any, params: dict, batch: PostingBatch) -> dict | None:
        try:
            r = await client.request(
                "GET", f"{API_URL}?{urlencode(params)}", rate_per_sec=_RATE, retries=1,
                check_robots=True,
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            log.warning("himalayas_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach Himalayas; try again later") from exc
        if r.status_code != 200:
            log.warning("himalayas_error", status=r.status_code)
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
                "Himalayas sent a reply Kestrel could not read; the format may have changed"
            ) from exc
        return data

