"""Breezy HR company job board adapter (SPEC.md Appendix A, verified live 2026-10-29).

List:   GET https://{slug}.breezy.hr/json -> 200 `application/json`, a bare array, keyless. Each
        entry has `id`, `friendly_id`, `name` (the title), `url`, `published_date`, `type`
        ({id, name}), `location` and `locations[]`, `department`, `salary` (display text) and
        `company` ({name, friendly_id}). There is NO description. An unknown {slug} answers 404
        with an HTML page; a board with no openings answers `[]`.
Detail: GET https://{slug}.breezy.hr/p/{friendly_id} -> 200 HTML. The description is the
        `<div class="description">` of the page (a schema.org JobPosting in `ld+json` is read as
        the fallback when a page has one).

GOTCHAS this adapter handles:
  - The description needs one page request per role, so it is capped at `max_results` pages a run
    (default 100). Roles past the cap are still kept, without a body, and the run is `partial`
    with a sentence saying how many. A page that fails (not a 429) leaves that role without a
    body; a 429 on a page stops reading pages and keeps every role; a 429 on the list ends the
    run with nothing.
  - The page URL is built from `friendly_id` on `{slug}.breezy.hr`, never taken from the payload,
    so a payload cannot point the adapter at another host.
  - `location` is `{country: {name, id}, state: {id, name}, city, is_remote, name}`; the place is
    "city, state name, country name" (the payload's own `name` has no country). `is_remote` is an
    explicit boolean and is the only thing that sets the remote flag; `null` stays unknown.
  - `locations[]` is empty for a one-place role and holds every place (primary first) otherwise.
  - `salary` is display text ("$1 – $5 / year") and is not parsed into numbers; it stays in
    `raw`.
  - Both `{slug}.breezy.hr/robots.txt` rules we saw disallow only static asset folders.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest import failure_code
from kestrel.ingest.base import (
    AdapterError,
    NoticeKind,
    PostingBatch,
    RawPosting,
    ShapeError,
    SourceConfigError,
    Unread,
    capped,
    failure_kind,
    rate_limited,
)

_RATE = 1.0  # requests/second; one detail request per role
_DEFAULT_MAX_RESULTS = 100
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_FRIENDLY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")


class BreezyAdapter:
    kind = "breezy"

    def validate_config(self, config: dict) -> None:
        slug = config.get("slug")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise SourceConfigError(
                "breezy source requires a 'slug' (the name in {slug}.breezy.hr: letters, digits "
                "and '-')"
            )
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("breezy 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        slug = config["slug"]
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        batch = PostingBatch()
        rows = await self._list(slug, client, batch)
        if rows is None:
            return batch

        pages_read = 0
        over_cap = 0  # roles beyond the pages Kestrel reads in one run
        slowed = False
        unread = Unread()  # role pages that failed, by the kind each failed with
        for row in rows:
            posting = _to_posting(row, config)
            if posting is None:
                continue
            if pages_read >= max_results:
                over_cap += 1
            elif slowed:  # a page it would have read, held back by the 429
                pages_read += 1
                unread.add(NoticeKind.RATE_LIMITED)
            else:
                pages_read += 1
                body = await _read_page(slug, str(row["friendly_id"]), client)
                if isinstance(body, NoticeKind):
                    unread.add(body)
                    slowed = body is NoticeKind.RATE_LIMITED
                else:
                    posting.description_html = body
            batch.append(posting)
        for notice in unread.notices("Breezy HR", slug, kept=True):
            batch.add(notice)
        if over_cap:
            batch.add(capped("Breezy HR", slug, over_cap, max_results))
        return batch

    async def _list(self, slug: str, client: Any, batch: PostingBatch) -> list[dict] | None:
        try:
            data = await client.get_json(
                f"https://{slug}.breezy.hr/json", rate_per_sec=_RATE, check_robots=True
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                batch.add(rate_limited("Breezy HR", slug))
                return None
            if code == 404:
                raise AdapterError(
                    f"Breezy HR board not found: {slug}", code=failure_code.NOT_FOUND
                ) from exc
            raise AdapterError(f"Breezy HR fetch failed for {slug}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"Breezy HR fetch failed for {slug}: {exc}") from exc
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except ValueError as exc:
            raise AdapterError(f"Breezy HR reply for {slug} was not valid JSON") from exc
        if not isinstance(data, list):
            raise ShapeError(f"Breezy HR reply for {slug} had an unexpected shape")
        return [r for r in data if isinstance(r, dict)]


async def _read_page(slug: str, friendly_id: str, client: Any) -> str | None | NoticeKind:
    """The role's description HTML, None when the page has none, or the kind of failure when it
    could not be read (a 429, robots.txt, anything else)."""
    try:
        html = await client.get_text(
            f"https://{slug}.breezy.hr/p/{friendly_id}", rate_per_sec=_RATE, check_robots=True
        )
    except (httpx.HTTPError, PermissionError) as exc:
        return failure_kind(exc)
    return _description(html)


def _description(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("div.description")
    if node is not None:
        body = node.decode_contents().strip()
        if node.get_text(strip=True):
            return body
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            ld = json.loads(tag.string or "")
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
            return _strip(ld.get("description"))
    return None


def _to_posting(row: dict, config: dict) -> RawPosting | None:
    friendly_id = row.get("friendly_id")
    if not isinstance(friendly_id, str) or not _FRIENDLY_ID.fullmatch(friendly_id):
        return None
    slug = config["slug"]
    page = f"https://{slug}.breezy.hr/p/{friendly_id}"
    url = _strip(row.get("url"))
    if url is None or urlsplit(url).hostname != f"{slug}.breezy.hr":
        url = page  # only an address on the board's own host is trusted
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    places = row.get("locations") if isinstance(row.get("locations"), list) else []
    labels: list[str] = []
    for place in [p for p in places if isinstance(p, dict)] or ([location] if location else []):
        label = _place(place)
        if label and label not in labels:
            labels.append(label)
    remote = location.get("is_remote")
    company = row.get("company") if isinstance(row.get("company"), dict) else {}
    kind = row.get("type") if isinstance(row.get("type"), dict) else {}
    return RawPosting(
        external_id=str(row.get("id") or friendly_id),
        url=url,
        title=_strip(row.get("name")),
        org_name=_strip(config.get("company_name")) or _strip(company.get("name")) or slug,
        location_raw="; ".join(labels) or None,
        employment_type=_strip(kind.get("name")),
        is_remote=True if remote is True else None,
        remote_hint="Remote" if remote is True else None,
        date_posted=_strip(row.get("published_date")),
        raw=row,
    )


def _place(place: dict) -> str | None:
    def name(value: Any) -> str | None:
        return _strip(value.get("name")) if isinstance(value, dict) else _strip(value)

    parts = [_strip(place.get("city")), name(place.get("state")), name(place.get("country"))]
    return ", ".join(p for p in parts if p) or _strip(place.get("name"))


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
