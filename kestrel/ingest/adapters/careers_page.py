"""Generic careers-page reader: schema.org JobPosting data in a page (B4, D-SRC-15, D-UI-102).

The operator pastes a careers page link. Many employers (and sites on iCIMS, Taleo, Paycor, UKG or
a custom build) embed each opening as JSON-LD for search engines; this adapter reads those and
nothing else. Nothing is scraped from the visible page and no model is involved.

How a run goes (one polite request at a time, 1/s, robots.txt checked, public addresses only, 2 MB
a page, via the shared client's `public_only` path):
  1. A link that is already a hiring system Kestrel has an adapter for (Greenhouse, Lever, ...) is
     handed to that adapter instead (board_discovery.board_from_url). Nothing is fetched here.
  2. The page is fetched once. If its markup names such a system (a Recruitee address, the
     Teamtailor career-site script; board_discovery.board_from_markup), that adapter reads it.
  3. Otherwise every JobPosting in the page is mapped. A posting without a description that
     points at its own page on the same site is read from that page (one level, no deeper).
  4. A page with no JobPosting at all but links that look like job pages is followed one level:
     at most MAX_DETAIL_PAGES detail requests a run. Links left unread are said in the run
     message, never dropped silently.
  5. A page that yields nothing is told plainly: "This page lists its jobs with JavaScript, which
     Kestrel cannot read yet." The run is `partial` with that sentence, never a silent zero.

Expired roles (`validThrough` before today) are skipped. Every role then goes through the normal
ingest path, so the D-SRC-13 area and title drop applies."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from kestrel.ingest import board_discovery
from kestrel.ingest import jobposting as jp
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.ingest.http import UnsafeURLError

log = structlog.get_logger()

_RATE = 1.0  # requests/second to the one site
MAX_DETAIL_PAGES = 25  # job pages followed from a list page per run; the run says when it bites
JS_ONLY = "This page lists its jobs with JavaScript, which Kestrel cannot read yet."
# A link is a likely job page when a path segment names one of these and something follows it
# (/job/1202337/title, /jobs/view/abc), or the query carries a job id. Category pages such as
# /careers/job-search or /c/legal-jobs name a job word but nothing follows, so they are not links
# to a job.
_JOB_WORDS = {
    "job", "jobs", "position", "positions", "opening", "openings", "vacancy", "vacancies",
    "posting", "postings", "requisition", "role", "roles", "opportunity", "opportunities",
}
_SKIP_SUFFIXES = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico", ".xml",
    ".zip", ".doc", ".docx",
)


def _clean(url: object) -> str:
    return url.strip() if isinstance(url, str) else ""


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def job_links(html: str, page_url: str) -> list[str]:
    """Links on the page that look like job detail pages on the same site: a path segment names a
    job (job, position, opening, ...) with something after it, or the query carries a job id."""
    here = urlsplit(page_url)
    here_path = here.path.rstrip("/")
    out: list[str] = []
    for link in jp.page_links(html, page_url):
        parts = urlsplit(link)
        path = parts.path.rstrip("/")
        if _site(link) != _site(page_url) or path.lower().endswith(_SKIP_SUFFIXES):
            continue
        if path == here_path and parts.query == here.query:
            continue
        segs = [seg.lower() for seg in path.split("/") if seg]
        named = any(seg in _JOB_WORDS and i < len(segs) - 1 for i, seg in enumerate(segs))
        by_query = any(k in parts.query.lower() for k in ("jobid=", "job_id=", "reqid=", "gh_jid="))
        if named or by_query:
            out.append(link)
    return out


class CareersPageAdapter:
    kind = "careers_page"

    def validate_config(self, config: dict) -> None:
        url = _clean(config.get("url"))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
            raise SourceConfigError(
                "careers_page source requires a 'url' (a careers page link starting with https://)"
            )

    async def _get(self, url: str, client: Any):
        """The page's HTML, or None when this one URL cannot be read. The first (list) page
        raises through `_get_list` instead."""
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, check_robots=True, retries=1, public_only=True
            )
        except (PermissionError, httpx.HTTPError, UnsafeURLError) as exc:
            log.debug("careers_page.detail_unreadable", error=type(exc).__name__)
            return None
        return r.text if r.status_code == 200 else None

    async def _get_list(self, url: str, client: Any) -> tuple[str | None, str | None]:
        """(html, partial_error) for the page the operator pasted."""
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, check_robots=True, retries=1, public_only=True
            )
        except PermissionError as exc:
            raise AdapterError(
                f"{_site(url)} asks not to be read automatically (robots.txt), so Kestrel "
                "will not read this page."
            ) from exc
        except UnsafeURLError as exc:
            raise AdapterError(f"Kestrel cannot read that link: {exc}") from exc
        except httpx.HTTPStatusError as exc:  # the client retried 429/403/503 and gave up
            if exc.response.status_code == 429:
                return None, f"{_site(url)} is rate limiting requests. Kestrel will try again."
            raise AdapterError(f"The careers page answered {exc.response.status_code}.") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"The careers page could not be reached: {exc}") from exc
        if r.status_code == 429:
            return None, f"{_site(url)} is rate limiting requests. Kestrel will try again."
        if r.status_code in (404, 410):
            raise AdapterError("The careers page was not found. Check the link.")
        if r.status_code != 200:
            raise AdapterError(f"The careers page answered {r.status_code}.")
        return r.text, None

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        url = _clean(config.get("url"))
        ref = board_discovery.board_from_url(url)
        if ref is not None:
            return await self._delegate(ref, config, client)
        html, stopped = await self._get_list(url, client)
        batch = PostingBatch()
        if html is None:
            batch.partial_error = stopped
            return batch
        return await self.read_html(html, config, client)

    async def read_html(self, html: str, config: dict, client: Any) -> PostingBatch:
        """Read a list page already in hand (the Sources link check fetches it once to look for a
        hiring system, then hands it here so the page is not requested twice)."""
        url = _clean(config.get("url"))
        ref = board_discovery.board_from_markup(html, url)
        if ref is not None:
            return await self._delegate(ref, config, client)
        batch = PostingBatch()
        seen: set[str] = set()
        follow: list[str] = []
        for obj in jp.job_postings(html, url):
            posting = jp.to_raw_posting(obj, url, org_fallback=_org(config))
            if posting is not None:
                self._add(batch, posting, seen)
                continue
            link = _own_page(obj, url)
            if link and not jp.is_expired(obj):
                follow.append(link)  # a posting stub (no description): its own page has the rest
        if not batch and not follow:
            follow = job_links(html, url)  # a list page that only links to its job pages
        todo = [u for u in dict.fromkeys(follow) if u not in seen and u != url]
        for detail in todo[:MAX_DETAIL_PAGES]:
            page = await self._get(detail, client)
            if page is None:
                continue
            for obj in jp.job_postings(page, detail):
                posting = jp.to_raw_posting(obj, detail, org_fallback=_org(config))
                if posting is not None:
                    self._add(batch, posting, seen)
        left = len(todo) - MAX_DETAIL_PAGES
        if left > 0:
            batch.partial_error = (
                f"This page links to {len(todo)} job pages. Kestrel reads at most "
                f"{MAX_DETAIL_PAGES} a run, so {left} were left for later."
            )
        elif not batch:
            batch.partial_error = JS_ONLY
        return batch

    @staticmethod
    def _add(batch: PostingBatch, posting: RawPosting, seen: set[str]) -> None:
        key = posting.external_id or ""
        if key and key in seen:
            return
        seen.add(key)
        batch.append(posting)

    async def _delegate(self, ref: board_discovery.BoardRef, config: dict, client: Any):
        """The hiring system's own adapter reads the board (it is richer than the page)."""
        from kestrel.ingest.adapters import get_adapter

        adapter = get_adapter(ref.kind)
        if adapter is None:
            raise AdapterError(f"No reader for {ref.kind}.")
        board_config = dict(ref.config)
        if _org(config) and ref.kind in board_discovery._NAMED_BY_COMPANY_NAME:
            board_config["company_name"] = _org(config)
        return await adapter.fetch(board_config, client)


def _org(config: dict) -> str | None:
    name = config.get("company_name")
    return name.strip() or None if isinstance(name, str) else None


def _own_page(obj: dict, page_url: str) -> str | None:
    """The posting's own page when the list page only carries a stub of it: its `url`, on the
    same site, and not the list page itself."""
    from urllib.parse import urldefrag, urljoin

    raw = obj.get("url")
    if not isinstance(raw, str) or not raw.strip():
        return None
    link = urldefrag(urljoin(page_url, raw.strip()))[0]
    if _site(link) != _site(page_url) or link == page_url:
        return None
    return link
