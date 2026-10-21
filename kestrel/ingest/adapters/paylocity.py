"""Paylocity Recruiting adapter (SPEC.md Appendix A, verified live 2026-10-21).

List:   GET https://recruiting.paylocity.com/recruiting/jobs/All/{board_id}
        -> 200 HTML. The board is JSON assigned to `window.pageData = {...};` in an inline script
        ({"ModuleTitle", "Jobs": [...], ...}). No login, no script challenge, no pagination.
Detail: GET https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}
        -> 200 HTML carrying one schema.org JobPosting in `<script type="application/ld+json">`.

GOTCHAS this adapter handles:
  - There is no JSON API for the list (the `v2/api/feed/jobs/{id}` feed answers an empty list for
    boards that have postings), so the page's own data is read: locate `window.pageData = ` and
    decode ONE JSON value from there. The JSON is never cut out with a regex.
  - The list's `Description` is a ~110 character snippet, not the body. The body is the posting
    page's ld+json `description` (HTML).
  - The posting page's ld+json `datePosted` has a WRONG UTC OFFSET (the local clock time relabelled
    `-05:00` after adding five hours). The list's `PublishedDate` is correct and is used instead.
  - An unknown board id answers 200 with a "Job Not Found" page that has no page data; that is an
    AdapterError ("no job list"), not an empty board. A real empty board has `Jobs: []`.
  - `IsInternal` postings are skipped (not public). `ShouldDisplayLocation: false` means the
    employer hides the place, so location_raw stays None. `IsRemote` is an explicit boolean.
  - The employer name is `ModuleTitle`; the posting link carries only `JobId`.
  - A failing detail skips only that posting; a detail page with no ld+json keeps the posting
    without a body; a 429 anywhere makes the whole run partial.
  - recruiting.paylocity.com/robots.txt is a 404 (no rules); requests stay at 1/second.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_LIST_URL = "https://recruiting.paylocity.com/recruiting/jobs/All"
_DETAIL_URL = "https://recruiting.paylocity.com/Recruiting/Jobs/Details"
_DEFAULT_MAX_RESULTS = 200
_RATE = 1.0  # requests/second; one detail request per posting
_BOARD_ID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_PAGE_DATA = "window.pageData = "
_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)


class PaylocityAdapter:
    kind = "paylocity"

    def validate_config(self, config: dict) -> None:
        board_id = config.get("board_id")
        if not isinstance(board_id, str) or not _BOARD_ID.match(board_id):
            raise SourceConfigError(
                "paylocity source requires a 'board_id' (the GUID in the careers link: "
                "recruiting.paylocity.com/recruiting/jobs/All/{board_id})"
            )
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("paylocity 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        board_id = config["board_id"]
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        data = await self._fetch_board(board_id, client)
        rows = [r for r in data["Jobs"] if _is_public_row(r)]
        postings: list[RawPosting] = []
        for row in rows[:max_results]:
            detail = await self._fetch_detail(board_id, str(row["JobId"]), client)
            if detail is None:
                continue  # a single failing DETAIL skips only this posting
            postings.append(self._to_posting(row, detail, data, config))
        return postings

    async def _fetch_board(self, board_id: str, client: Any) -> dict:
        try:
            html = await client.get_text(f"{_LIST_URL}/{board_id}", rate_per_sec=_RATE)
        except httpx.HTTPError as exc:
            raise _failure("list", board_id, exc) from exc
        start = html.find(_PAGE_DATA)
        if start < 0:
            raise AdapterError(
                f"Paylocity page for {board_id} has no job list (is the board id right?)"
            )
        try:
            data, _ = json.JSONDecoder().raw_decode(html[start + len(_PAGE_DATA) :])
        except ValueError as exc:
            raise AdapterError(f"Paylocity page data for {board_id} was unreadable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("Jobs"), list):
            raise AdapterError(f"Paylocity page data for {board_id} had an unexpected shape")
        return data

    async def _fetch_detail(self, board_id: str, job_id: str, client: Any) -> dict | None:
        """The posting's ld+json JobPosting ({} when the page has none), or None on a non-429
        failure so the caller skips the posting."""
        try:
            html = await client.get_text(f"{_DETAIL_URL}/{job_id}", rate_per_sec=_RATE)
        except httpx.HTTPError as exc:
            if _is_429(exc):
                raise _failure("detail", board_id, exc) from exc
            return None
        for block in _LD_JSON.findall(html):
            try:
                ld = json.loads(block)
            except ValueError:
                continue
            if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
                return ld
        return {}

    def _to_posting(self, row: dict, detail: dict, data: dict, config: dict) -> RawPosting:
        job_id = str(row["JobId"])
        show_location = row.get("ShouldDisplayLocation") is not False
        remote = row.get("IsRemote")
        return RawPosting(
            external_id=job_id,
            url=f"{_DETAIL_URL}/{job_id}",
            title=_strip(row.get("JobTitle")) or _strip(detail.get("title")),
            org_name=_strip(config.get("company_name")) or _strip(data.get("ModuleTitle")),
            location_raw=_strip(row.get("LocationName")) if show_location else None,
            description_html=_strip(detail.get("description")),
            is_remote=remote if isinstance(remote, bool) else None,
            remote_hint="Remote" if remote is True else None,
            date_posted=_strip(row.get("PublishedDate")),  # NOT the posting page's datePosted
            raw={**row, **{k: v for k, v in detail.items() if k != "@context"}},
        )


def _is_public_row(row: Any) -> bool:
    """A listed posting with an id; an internal-only one is not public."""
    return (
        isinstance(row, dict)
        and row.get("JobId") not in (None, "")
        and row.get("IsInternal") is not True
    )


def _is_429(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _failure(part: str, board_id: str, exc: httpx.HTTPError) -> AdapterError:
    if _is_429(exc):
        return AdapterError(f"Paylocity rate limit reached while reading {part} for {board_id}")
    return AdapterError(f"Paylocity {part} fetch failed for {board_id}: {exc}")


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
