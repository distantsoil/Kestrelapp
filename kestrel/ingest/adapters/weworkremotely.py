"""We Work Remotely job feed (SPEC.md 5.1 and Appendix A, D-SRC-15, step B3). No key.

`GET https://weworkremotely.com/remote-jobs.rss` (the public RSS feed of recent remote jobs, about
a hundred items; category feeds follow `https://weworkremotely.com/categories/remote-<name>-jobs.rss`).
Verified live 2026-10-29 for the shape and for robots.txt (which allows the feeds). The site's terms
(updated 2026-10-23) were read by the owner on 2026-10-30 (D-SRC-16): a limited right to access and
use the site's materials, nothing against reading the public feed the site links from its footer.
What this adapter does keeps to the feed's evident purpose: a public feed, read once per check by a
personal app, each role shown with the board's name and a link back to the role's page there, which
is also where the user applies.

This is a JOB feed adapter. The existing `rss` source kind is a consulting-track signal source (a
feed of news that points at companies) and is deliberately not reused.

Item shape: `<title>` is "Company: Job title"; `<region>` ("Anywhere in the World", "North America
Only", "USA Only", "Europe Only", ...), `<country>`, `<state>`, `<skills>`, `<category>`, `<type>`
("Full-Time", "Contract", ...), `<description>` (HTML, escaped inside the XML), `<pubDate>`,
`<expires_at>`, `<guid>` and `<link>` (the same page).

Rules that bind this module:
  - One request per check, 1 request/second, robots.txt checked, never retried. A 403 (the site's
    bot check) or 429 or 5xx is a `partial` run; nothing else of the site is ever requested.
  - A DOCTYPE or ENTITY in the feed is refused (the stdlib parser has no entity-expansion guard).
  - A role whose region is limited to somewhere other than the US ("Europe Only") is not passed on:
    it is the board's own explicit statement, the same filter Himalayas applies with `country=US`.
    Anything unclear ("Anywhere in the World", "North America Only", no region) is kept (hard
    rule 4). A role past its `expires_at` is dropped.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx
import structlog

from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

FEED_URL = "https://weworkremotely.com/remote-jobs.rss"
CATEGORY_URL = "https://weworkremotely.com/categories/{slug}.rss"
SOURCE_NAME = "We Work Remotely"
SOURCE_URL = "https://weworkremotely.com"
_RATE = 1.0
_SLUG = re.compile(r"^remote-[a-z0-9-]{1,60}-jobs$")
_DOCTYPE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_US_WORDS = re.compile(
    r"anywhere|world|worldwide|north america|americas|\busa?\b|united states|u\.s\.", re.I)


def _text(el: ElementTree.Element, tag: str) -> str | None:
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    s = child.text.strip()
    return s or None


def _when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _open_to_us(region: str | None) -> bool:
    """False only for a region the board itself limits to somewhere else ("Europe Only")."""
    if not region or _US_WORDS.search(region):
        return True
    return "only" not in region.lower()


def _location(region: str | None) -> str:
    """Where the role may be worked from. `country` and `state` in the feed say where the employer
    is based, not who may apply, so they are kept in `raw` and not used here."""
    if region and re.search(r"\busa?\b|united states", region, re.I) and "north america" not in (
            region.lower()):
        return "United States"
    if region and "anywhere" not in region.lower() and "world" not in region.lower():
        return f"Remote ({region.replace(' Only', '')})"
    return "Remote (worldwide)"


def to_posting(item: ElementTree.Element, *, now: datetime | None = None) -> RawPosting | None:
    """One feed `<item>` as a RawPosting; None for an empty one, an expired one, or one the board
    limits to a region without the US."""
    full = _text(item, "title")
    link = _text(item, "link") or _text(item, "guid")
    if not full and not link:
        return None
    expires = _when(_text(item, "expires_at"))
    if expires is not None and expires < (now or datetime.now(UTC)):
        return None
    region, country, state = _text(item, "region"), _text(item, "country"), _text(item, "state")
    if not _open_to_us(region):
        return None
    org, title = None, full
    if full and ": " in full:
        org, title = (p.strip() for p in full.split(": ", 1))
    posted = _when(_text(item, "pubDate"))
    return RawPosting(
        external_id=_text(item, "guid") or link,
        url=link,
        apply_url=link,  # the role's page on the board, where its apply button is
        title=title or None,
        org_name=org or None,
        location_raw=_location(region),
        description_html=_text(item, "description"),
        employment_type=_text(item, "type"),
        is_remote=True,
        remote_hint="Remote",
        date_posted=posted.date().isoformat() if posted else None,
        raw={"via": SOURCE_NAME, "via_url": SOURCE_URL, "region": region,
             "employer_country": country, "employer_state": state,
             "category": _text(item, "category"), "skills": _text(item, "skills")},
    )


def _problem(status: int) -> str:
    if status in (403, 429):
        return "We Work Remotely asked Kestrel to slow down; it is fetched on the next check"
    if status >= 500:
        return "We Work Remotely had a problem on their side; it is fetched on the next check"
    return f"We Work Remotely answered with an unexpected status ({status})"


class WeWorkRemotelyAdapter:
    kind = "weworkremotely"

    def validate_config(self, config: dict) -> None:
        cat = config.get("category")
        if cat is not None and (not isinstance(cat, str) or not _SLUG.match(cat)):
            raise SourceConfigError(
                "'category' must look like remote-programming-jobs (a We Work Remotely category)"
            )

    async def fetch(self, config: dict, client: Any) -> PostingBatch:
        self.validate_config(config)
        batch = PostingBatch()
        cat = config.get("category")
        url = CATEGORY_URL.format(slug=cat) if cat else FEED_URL
        try:
            r = await client.request("GET", url, rate_per_sec=_RATE, retries=1, check_robots=True)
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(
                "We Work Remotely's robots.txt does not allow this request, so Kestrel skipped it"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("wwr_unreachable", error=type(exc).__name__)
            raise AdapterError("Could not reach We Work Remotely; try again later") from exc
        if r.status_code != 200:
            log.warning("wwr_error", status=r.status_code)
            if r.status_code in (403, 429) or r.status_code >= 500:
                batch.partial_error = _problem(r.status_code)
                return batch
            raise AdapterError(_problem(r.status_code))
        text = r.text
        try:
            if _DOCTYPE.search(text[:4096]):
                raise ValueError("DOCTYPE")
            root = ElementTree.fromstring(text)
            items = root.findall("./channel/item")
        except (ValueError, ElementTree.ParseError) as exc:
            raise AdapterError(
                "We Work Remotely sent a feed Kestrel could not read; the format may have changed"
            ) from exc
        seen: set[str] = set()
        now = datetime.now(UTC)
        for item in items:
            posting = to_posting(item, now=now)
            if posting is None or posting.external_id in seen:
                continue
            seen.add(posting.external_id or "")
            batch.append(posting)
        return batch
