"""Turn the employer links of a discovery-only source into postings (D-SRC-14).

A link source (RemoteRocketship) hands over nothing but employer URLs. Each one takes the path the
job searches already use (D-SRC-7): a recognized company board (or a grnh.se short link) goes to
`board_discovery.discover_boards`, which adds the board as a found source read by its own adapter;
any other link is fetched once from the employer's public page, politely (robots.txt checked,
redirects not followed, `MAX_PAGES_PER_RUN` a run) and read for schema.org `JobPosting` data. A
link nothing can read is dropped and counted as `unreadable`; it is never stored."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Source
from kestrel.ingest import board_discovery
from kestrel.ingest.base import RawPosting
from kestrel.ingest.employer_page import MAX_PAGE_BYTES, parse_job_posting
from kestrel.ingest.http import HttpClient

log = structlog.get_logger()

MAX_PAGES_PER_RUN = 20
_PAGE_RATE = 1.0


# The provider's own site is never fetched (its terms forbid scraping it, section 5): a link on it
# is dropped and counted as unreadable.
_OWN_HOST = "remoterocketship.com"


def _is_provider_link(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == _OWN_HOST or host.endswith("." + _OWN_HOST)


def _is_board_link(url: str) -> bool:
    return board_discovery.board_from_url(url) is not None or board_discovery._is_short_link(url)


async def _read_page(url: str, http: HttpClient) -> RawPosting | None:
    """The posting an employer page publishes, or None. Any failure (robots, network, status,
    redirect, no data) is None, never an error."""
    try:
        if urlsplit(url).scheme not in ("http", "https"):
            return None
        r = await http.request(
            "GET", url, check_robots=True, follow_redirects=False, retries=1,
            rate_per_sec=_PAGE_RATE,
        )
    except (PermissionError, httpx.HTTPError) as exc:
        log.debug("employer_link.unreadable", error=type(exc).__name__)
        return None
    if r.status_code != 200 or len(r.content) > MAX_PAGE_BYTES:
        return None
    return parse_job_posting(r.text, url)


async def resolve(
    session: AsyncSession, links: list[RawPosting], *, source: Source, http: HttpClient
) -> tuple[list[RawPosting], dict]:
    """(postings read from employer pages, counts). Counts: links, board_links, boards_found,
    pages_read, unreadable. Flushes, never commits; the caller owns the transaction."""
    urls = [p.url for p in links if p.url]
    own = [u for u in urls if _is_provider_link(u)]
    urls_kept = [u for u in urls if not _is_provider_link(u)]
    board_urls = [u for u in urls_kept if _is_board_link(u)]
    other = [u for u in urls_kept if not _is_board_link(u)]
    found = 0
    if board_urls:
        found = await board_discovery.discover_boards(
            session, [RawPosting(url=u) for u in board_urls], search=source, http=http
        )
    postings: list[RawPosting] = []
    unreadable = len(own) + max(0, len(other) - MAX_PAGES_PER_RUN)  # own site, or over the cap
    for url in other[:MAX_PAGES_PER_RUN]:
        posting = await _read_page(url, http)
        if posting is None:
            unreadable += 1
        else:
            postings.append(posting)
    counts = {
        "links": len(urls),
        "board_links": len(board_urls),
        "boards_found": found,
        "pages_read": len(postings),
        "unreadable": unreadable,
    }
    return postings, counts
