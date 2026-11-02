"""Teamtailor career site adapter (SPEC.md Appendix A, verified live 2026-10-29).

Feed: GET https://{domain}/jobs.json -> 200 JSON Feed 1.1 (`application/feed+json`), keyless:
      {"version", "title", "home_page_url", "feed_url", "items": [...]}. Each item has `id` (a
      UUID), `title`, `url`, `date_published`, `content_html` (the full description) and a
      `_jobposting` object (schema.org JobPosting: `hiringOrganization`, `jobLocation[]`).
Fallback: GET https://{domain}/jobs.rss -> RSS 2.0 with the same roles, plus `remoteStatus`
      ("none", "hybrid", "remote"), a `tt:` namespace with `location` (city, country as a name)
      and `department`. Used only when `jobs.json` is not there.

GOTCHAS this adapter handles:
  - {domain} is the career site's own host: {name}.teamtailor.com, or the employer's own domain
    (career.example.org) when it has one. Teamtailor has no shared API host, so the domain is the
    board's identity.
  - Some sites switch the feeds off, or a firewall refuses them. That is not a failure of the
    source: the run is `partial` with a plain sentence saying the feed is not available, and no
    request is repeated.
  - The JSON feed's location is `_jobposting.jobLocation[].address` with `addressLocality` and
    `addressCountry` as an ISO code. `addressRegion` is a Teamtailor region ("North America"), not
    a state, and is ignored. The ISO code is turned into the country's name, because a bare "CA"
    would read as California.
  - The JSON feed has no remote flag. A `jobLocationType` of TELECOMMUTE is remote; otherwise
    remote stays unknown. The RSS `remoteStatus` is read when the RSS is the source.
  - The employer name is `hiringOrganization.name` (RSS: the channel title).
  - The domain is supplied by the operator, so every request is the guarded kind: public
    addresses only, robots.txt checked, 2 MB at most.
  - Pagination is not used: the captured feed was a single document with no `next_url`.
"""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.ingest.country_names import country_name
from kestrel.ingest.http import UnsafeURLError

_RATE = 1.0  # requests/second; one or two requests per scan
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TT = "{https://teamtailor.com/locations}"
_GONE = (404, 410)
_REFUSED = (401, 403)


def feed_off_message(domain: str) -> str:
    return (
        f"{domain} has its public job feed turned off, so Kestrel cannot read its roles. "
        "Open the roles you want and add each with One posting."
    )


class TeamtailorAdapter:
    kind = "teamtailor"

    def validate_config(self, config: dict) -> None:
        domain = config.get("domain")
        if not isinstance(domain, str) or not _DOMAIN.match(domain):
            raise SourceConfigError(
                "teamtailor source requires a 'domain' (the career site's address without "
                "https://, such as career.example.org or example.teamtailor.com)"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        domain = config["domain"]
        batch = PostingBatch()
        outcome = await self._get(f"https://{domain}/jobs.json", client, domain, batch)
        if batch.partial_error:
            return batch
        if outcome is not None:
            feed = outcome
            if not isinstance(feed, dict) or not isinstance(feed.get("items"), list):
                raise AdapterError(f"Teamtailor feed for {domain} had an unexpected shape")
            for item in feed["items"]:
                posting = _from_json(item, config) if isinstance(item, dict) else None
                if posting is not None:
                    batch.append(posting)
            return batch
        # No JSON feed: try the RSS once before saying the feeds are off.
        text = await self._get_text(f"https://{domain}/jobs.rss", client, domain, batch)
        if batch.partial_error:
            return batch
        if text is None:
            batch.partial_error = feed_off_message(domain)
            return batch
        batch.extend(_from_rss(text, config))
        return batch

    async def _get(self, url: str, client: Any, domain: str, batch: PostingBatch) -> Any:
        """The JSON feed parsed, or None when this site has none (missing, gone or not JSON).
        A 429, a refusal or robots.txt sets batch.partial_error and returns None."""
        try:
            return await client.get_json(
                url, rate_per_sec=_RATE, check_robots=True, public_only=True
            )
        except ValueError as exc:
            if isinstance(exc, UnsafeURLError):
                raise AdapterError(f"Teamtailor domain {domain} cannot be read: {exc}") from exc
            return None  # a 200 that is not JSON (a page, not the feed)
        except httpx.HTTPStatusError as exc:
            return self._status(exc, domain, batch)
        except httpx.HTTPError as exc:
            raise AdapterError(f"Teamtailor feed fetch failed for {domain}: {exc}") from exc
        except PermissionError:
            batch.partial_error = _robots_message(domain)
            return None

    async def _get_text(
        self, url: str, client: Any, domain: str, batch: PostingBatch
    ) -> str | None:
        try:
            return await client.get_text(
                url, rate_per_sec=_RATE, check_robots=True, public_only=True
            )
        except UnsafeURLError as exc:
            raise AdapterError(f"Teamtailor domain {domain} cannot be read: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            self._status(exc, domain, batch)
            return None
        except httpx.HTTPError as exc:
            raise AdapterError(f"Teamtailor feed fetch failed for {domain}: {exc}") from exc
        except PermissionError:
            batch.partial_error = _robots_message(domain)
            return None

    @staticmethod
    def _status(exc: httpx.HTTPStatusError, domain: str, batch: PostingBatch) -> None:
        code = exc.response.status_code
        if code in _GONE:
            return None  # not here: the caller tries the next feed, then says it is off
        if code == 429:
            batch.partial_error = (
                f"{domain} is rate limiting requests. Kestrel will try again next run."
            )
        elif code in _REFUSED:
            batch.partial_error = (
                f"{domain} refuses automated reads of its job feed, so Kestrel cannot read its "
                "roles. Add roles you want with One posting."
            )
        else:
            raise AdapterError(f"Teamtailor feed fetch failed for {domain}: {exc}") from exc
        return None


def _robots_message(domain: str) -> str:
    """The authored robots sentence; the site is never named in it."""
    return ROBOTS_SENTENCE


# ----------------------------------------------------------------------------------- JSON Feed
def _from_json(item: dict, config: dict) -> RawPosting | None:
    ident = _strip(item.get("id"))
    if ident is None:
        return None
    jp = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
    org = jp.get("hiringOrganization")
    org_name = _strip(org.get("name")) if isinstance(org, dict) else None
    places = jp.get("jobLocation")
    labels: list[str] = []
    for place in places if isinstance(places, list) else [places]:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            label = _label(_strip(address.get("addressLocality")), country_name(
                _strip(address.get("addressCountry"))))
            if label and label not in labels:
                labels.append(label)
    remote = _strip(jp.get("jobLocationType")) == "TELECOMMUTE"
    return RawPosting(
        external_id=ident,
        url=_strip(item.get("url")),
        title=_strip(item.get("title")) or _strip(jp.get("title")),
        org_name=_strip(config.get("company_name")) or org_name or config["domain"],
        location_raw="; ".join(labels) or None,
        description_html=_strip(item.get("content_html")) or _strip(jp.get("description")),
        employment_type=_strip(jp.get("employmentType")),
        is_remote=True if remote else None,
        remote_hint="Remote" if remote else None,
        date_posted=_strip(item.get("date_published")) or _strip(jp.get("datePosted")),
        raw=item,
    )


# ------------------------------------------------------------------------------------------ RSS
def _from_rss(text: str, config: dict) -> list[RawPosting]:
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise AdapterError(
            f"Teamtailor RSS for {config['domain']} was refused (it declares entities)"
        )
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise AdapterError(f"Teamtailor RSS for {config['domain']} was unreadable") from exc
    channel = root.find("channel")
    if channel is None:
        raise AdapterError(f"Teamtailor RSS for {config['domain']} had an unexpected shape")
    channel_title = _strip(channel.findtext("title"))
    out: list[RawPosting] = []
    for item in channel.findall("item"):
        ident = _strip(item.findtext("guid")) or _strip(item.findtext("link"))
        if ident is None:
            continue
        labels: list[str] = []
        for loc in item.findall(f"{_TT}locations/{_TT}location"):
            label = _label(
                _strip(loc.findtext(f"{_TT}city")), _strip(loc.findtext(f"{_TT}country"))
            )
            if label and label not in labels:
                labels.append(label)
        status = (_strip(item.findtext("remoteStatus")) or "").lower()
        out.append(
            RawPosting(
                external_id=ident,
                url=_strip(item.findtext("link")),
                title=_strip(item.findtext("title")),
                org_name=_strip(config.get("company_name")) or channel_title or config["domain"],
                location_raw="; ".join(labels) or None,
                description_html=_strip(item.findtext("description")),
                is_remote=True if status == "remote" else None,
                remote_hint={"remote": "Remote", "hybrid": "Hybrid"}.get(status),
                date_posted=_rfc2822(item.findtext("pubDate")),
                raw={
                    "guid": ident,
                    "department": _strip(item.findtext(f"{_TT}department")),
                    "remoteStatus": status or None,
                },
            )
        )
    return out


def _rfc2822(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError):
        return None


def _label(city: str | None, country: str | None) -> str | None:
    return ", ".join(p for p in (city, country) if p) or None


def _strip(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value.strip() or None if isinstance(value, str) else None
