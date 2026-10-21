"""Workable public job-board adapter (SPEC.md Appendix A, captured 2026-10-21 by the maintainer).

List:   POST https://apply.workable.com/api/v3/accounts/{slug}/jobs, body `{}`
        -> 200 `{"results": [...], "total": N}`, no auth.
Detail: GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}
        -> the same fields plus `description`, `requirements` and `benefits` (HTML).

GOTCHAS this adapter handles:
  - The LIST has no description, so one detail request is made per posting. A failing detail
    skips only that posting; a 429 anywhere makes the whole run partial.
  - Results with `isInternal` true or a `state` other than "published" are skipped.
  - The location text is built from `location.city`, `region` and `country`; `city` can be ""
    and `region` null, and empty parts are dropped.
  - The body is `description`, `requirements` and `benefits` joined with newlines, blank
    sections skipped. The payload names no employer: `company_name` or the slug is used.
  - PAGINATION IS UNVERIFIED. The captured board had total == len(results). If a response
    carries `nextPage`, the next request POSTs `{"token": <nextPage>}`, up to _MAX_PAGES. If
    `total` exceeds what was collected and there is no token, the adapter returns what it has
    and logs a warning; it never raises for that.
  - Only the API on apply.workable.com is called (its robots.txt allows everything). The
    www.workable.com `/j/` pages, which its robots.txt disallows, are never fetched.
  - The board slug is the first path segment of the public page (apply.workable.com/{slug}/).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

_BASE = "https://apply.workable.com"
_DEFAULT_MAX_RESULTS = 200
_MAX_PAGES = 20
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SHORTCODE = re.compile(r"^[A-Za-z0-9]{1,40}$")
_RATE = 1.0  # requests/second; one detail request per posting


class WorkableAdapter:
    kind = "workable"

    def validate_config(self, config: dict) -> None:
        slug = config.get("slug")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise SourceConfigError(
                "workable source requires a 'slug' (the board name in "
                "apply.workable.com/{slug}/: letters, digits, '-', '_' or '.')"
            )
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("workable 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        slug = config["slug"]
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        rows = await self._fetch_list(slug, client)

        postings: list[RawPosting] = []
        for row in rows[:max_results]:
            detail = await self._fetch_detail(slug, row["shortcode"], client)
            if detail is None:
                continue  # a single failing DETAIL skips only this posting
            postings.append(self._to_posting(row, detail, config))
        return postings

    async def _fetch_list(self, slug: str, client: Any) -> list[dict]:
        """Published, non-internal rows with a shortcode, in board order, across any pages."""
        url = f"{_BASE}/api/v3/accounts/{slug}/jobs"
        body: dict = {}
        rows: list[dict] = []
        seen = 0
        total: Any = None
        token: str | None = None
        for _page in range(_MAX_PAGES):
            data = await self._post_page(url, body, slug, client)
            results = data.get("results")
            if not isinstance(results, list):
                raise AdapterError(f"Workable list for {slug} had an unexpected shape")
            seen += len(results)
            total = data.get("total", total)
            for row in results:
                if _wanted(row):
                    rows.append(row)
            token = _str(data.get("nextPage"))
            if token is None:
                break
            body = {"token": token}
        if token is None and isinstance(total, int) and total > seen:
            log.warning("workable.list_incomplete", slug=slug, total=total, collected=seen)
        elif token is not None:
            log.warning("workable.page_cap_reached", slug=slug, pages=_MAX_PAGES)
        return rows

    async def _post_page(self, url: str, body: dict, slug: str, client: Any) -> dict:
        try:
            data = await client.post_json(url, json=body, rate_per_sec=_RATE, check_robots=True)
        except (httpx.HTTPError, PermissionError) as exc:
            raise _failure("list", slug, exc) from exc
        except ValueError as exc:
            raise AdapterError(f"Workable list for {slug} was not valid JSON") from exc
        if not isinstance(data, dict):
            raise AdapterError(f"Workable list for {slug} had an unexpected shape")
        return data

    async def _fetch_detail(self, slug: str, shortcode: str, client: Any) -> dict | None:
        """The parsed detail, or None on a non-429 failure so the caller skips the posting."""
        url = f"{_BASE}/api/v2/accounts/{slug}/jobs/{shortcode}"
        try:
            detail = await client.get_json(url, rate_per_sec=_RATE, check_robots=True)
        except httpx.HTTPError as exc:
            if _is_429(exc):
                raise _failure("detail", slug, exc) from exc
            return None
        except (PermissionError, ValueError):
            return None
        return detail if isinstance(detail, dict) else {}

    def _to_posting(self, row: dict, detail: dict, config: dict) -> RawPosting:
        slug = config["slug"]
        shortcode = row["shortcode"]
        merged = {**row, **detail}
        workplace = _str(merged.get("workplace"))
        remote = merged.get("remote") is True or (workplace or "").lower() == "remote"
        return RawPosting(
            external_id=shortcode,
            url=f"{_BASE}/{slug}/j/{shortcode}/",
            title=_strip(merged.get("title")),
            org_name=_str(config.get("company_name")) or slug,
            location_raw=_location(merged.get("location")),
            description_html=_description(detail),
            employment_type=_str(merged.get("type")),
            is_remote=True if remote else None,
            remote_hint="Remote" if remote else None,
            date_posted=_str(merged.get("published")),
            raw=merged,
        )


def _wanted(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("shortcode"), str)
        and bool(_SHORTCODE.match(row["shortcode"]))
        and row.get("isInternal") is not True
        and row.get("state") == "published"
    )


def _description(detail: dict) -> str | None:
    parts = [
        v.strip() for k in ("description", "requirements", "benefits")
        if isinstance(v := detail.get(k), str) and v.strip()
    ]
    return "\n".join(parts) or None


def _location(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [_strip(value.get(k)) for k in ("city", "region", "country")]
    return ", ".join(p for p in parts if p) or None


def _is_429(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _failure(part: str, slug: str, exc: Exception) -> AdapterError:
    if _is_429(exc):
        return AdapterError(f"Workable rate limit reached while reading {part} for {slug}")
    return AdapterError(f"Workable {part} fetch failed for {slug}: {exc}")


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
