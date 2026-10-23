"""Rippling public job-board adapter (SPEC.md Appendix A, verified live 2026-10-21).

List:   GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
        -> 200, a bare JSON array, no auth, no pagination (the whole board is one response).
Detail: GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}
        -> the description and the posted date, which the LIST does NOT carry.

GOTCHAS this adapter handles:
  - The job title is `name`, and it carries leading and trailing spaces; it is trimmed.
  - A posting open in several places is ONE LIST ROW PER LOCATION with the same `uuid`. Rows are
    merged by `uuid` (first-seen order) and the location labels are joined with "; ".
  - The LIST has no description and no date. The detail's `description.role` (HTML) is the body;
    `description.company` is the employer boilerplate and is left out. `createdOn` is the posted
    date. A failing detail skips only that posting; a 429 anywhere makes the whole run partial.
  - `payRangeDetails` was empty in every sampled posting, so no compensation is read: comp_* stay
    None (unknown, never zero).
  - Remote is only inferred from a location label that starts with "Remote"; otherwise it stays
    None (hard rule 4: uncertainty escalates, it never rejects).
  - The board slug is the path segment of the public page (ats.rippling.com/{slug}/jobs).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from kestrel.ingest.base import AdapterError, RawPosting, SourceConfigError

_BASE = "https://api.rippling.com/platform/api/ats/v1/board"
_DEFAULT_MAX_RESULTS = 200
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_RATE = 1.0  # requests/second; one detail request per posting


class RipplingAdapter:
    kind = "rippling"

    def validate_config(self, config: dict) -> None:
        slug = config.get("slug")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise SourceConfigError(
                "rippling source requires a 'slug' (the board name in "
                "ats.rippling.com/{slug}/jobs: letters, digits, '-', '_' or '.')"
            )
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("rippling 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        slug = config["slug"]
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        rows = await self._fetch_list(slug, client)

        postings: list[RawPosting] = []
        for uuid, row in list(rows.items())[:max_results]:
            detail = await self._fetch_detail(slug, uuid, client)
            if detail is None:
                continue  # a single failing DETAIL skips only this posting
            postings.append(self._to_posting(row, detail, config))
        return postings

    async def _fetch_list(self, slug: str, client: Any) -> dict[str, dict]:
        """List rows merged by uuid: {uuid: {"row": first row, "locations": [labels]}}."""
        try:
            data = await client.get_json(f"{_BASE}/{slug}/jobs", rate_per_sec=_RATE)
        except httpx.HTTPError as exc:
            raise _failure("list", slug, exc) from exc
        except ValueError as exc:
            raise AdapterError(f"Rippling list for {slug} was not valid JSON") from exc
        if not isinstance(data, list):
            raise AdapterError(f"Rippling list for {slug} had an unexpected shape")
        merged: dict[str, dict] = {}
        for row in data:
            if not isinstance(row, dict) or not _str(row.get("uuid")):
                continue
            entry = merged.setdefault(str(row["uuid"]), {"row": row, "locations": []})
            label = _label(row.get("workLocation"))
            if label and label not in entry["locations"]:
                entry["locations"].append(label)
        return merged

    async def _fetch_detail(self, slug: str, uuid: str, client: Any) -> dict | None:
        """The parsed detail, or None on a non-429 failure so the caller skips the posting."""
        try:
            detail = await client.get_json(f"{_BASE}/{slug}/jobs/{uuid}", rate_per_sec=_RATE)
        except httpx.HTTPError as exc:
            if _is_429(exc):
                raise _failure("detail", slug, exc) from exc
            return None
        except ValueError:
            return None
        return detail if isinstance(detail, dict) else {}

    def _to_posting(self, merged: dict, detail: dict, config: dict) -> RawPosting:
        row: dict = merged["row"]
        locations: list[str] = merged["locations"] or [
            s for s in (detail.get("workLocations") or []) if isinstance(s, str) and s
        ]
        description = detail.get("description")
        role = _str(description.get("role")) if isinstance(description, dict) else None
        board = detail.get("board") if isinstance(detail.get("board"), dict) else {}
        remote = any(loc.strip().lower().startswith("remote") for loc in locations)
        return RawPosting(
            external_id=_str(row.get("uuid")),
            url=_str(detail.get("url")) or _str(row.get("url")),
            title=_strip(detail.get("name")) or _strip(row.get("name")),
            org_name=(
                _str(config.get("company_name"))
                or _str(detail.get("companyName"))
                or _str(board.get("companyName"))
                or config["slug"]
            ),
            location_raw="; ".join(locations) or None,
            description_html=role,
            employment_type=_employment_type(detail.get("employmentType")),
            is_remote=True if remote else None,
            remote_hint="Remote" if remote else None,
            date_posted=_str(detail.get("createdOn")),
            raw={**row, **detail},
        )


def _is_429(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _failure(part: str, slug: str, exc: httpx.HTTPError) -> AdapterError:
    if _is_429(exc):
        return AdapterError(f"Rippling rate limit reached while reading {part} for {slug}")
    return AdapterError(f"Rippling {part} fetch failed for {slug}: {exc}")


def _label(value: Any) -> str | None:
    if isinstance(value, dict):
        return _strip(value.get("label")) or _strip(value.get("id"))
    return _strip(value)


def _employment_type(value: Any) -> str | None:
    if isinstance(value, dict):
        return _strip(value.get("id")) or _strip(value.get("label"))
    return _strip(value)


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
