"""Jobvite XML job feed adapter (SPEC.md Appendix A, verified live 2026-11-10).

Feed:  GET https://app.jobvite.com/CompanyJobs/Xml.aspx?c={companyEId} -> 200 XML `<result>` of
       `<job>` elements (`id`, `title`, `category`, `jobtype`, `location`, `date` M/D/YYYY,
       `detail-url`, `apply-url`, `description` (entity-escaped HTML)). Keyless; a big board is
       several MB. An empty tenant answers `<result />`; a retired tenant redirects.
Board: GET https://jobs.jobvite.com/{slug} -> HTML whose inline script holds `companyEId: '...'`.

GOTCHAS this adapter handles:
  - The feed keys on an opaque companyEId that is not in the careers URL. Order: `company_eid`
    in the config, else the board page of `slug`.
  - Redirects are never followed: a redirect from the feed means the board is gone.
  - Both hosts are fixed. detail-url and apply-url are display-only and may be on the employer's
    own domain; an http address on a Jobvite host is shown as https.
  - Invalid XML characters are removed first, and a reply that declares a DOCTYPE or ENTITY is
    refused (stdlib XML has no entity-expansion hardening).
  - `hiring_x0020_team` is a person's name and is never read.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree
from xml.parsers import expat

import httpx
import structlog

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError

log = structlog.get_logger(__name__)

_FEED = "https://app.jobvite.com/CompanyJobs/Xml.aspx?c={eid}"
_BOARD = "https://jobs.jobvite.com/{slug}"
_JOBVITE_HOSTS = {"app.jobvite.com", "jobs.jobvite.com"}
_RATE = 1.0
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_EID = re.compile(r"[A-Za-z0-9]{4,32}")
_EID_IN_PAGE = re.compile(r"companyEId:\s*'([A-Za-z0-9]+)'")
_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_BAD_REFS = re.compile(
    r"&#(?:x0*(?:[0-8bcef]|1[0-9a-f])|0*(?:[0-8]|11|12|1[4-9]|2[0-9]|30|31));", re.IGNORECASE)


class _Declared(Exception):
    """The reply declares a DOCTYPE or an entity."""


def _refuse_declaration(*_args: Any) -> None:
    raise _Declared


class _Slow(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JobviteAdapter:
    kind = "jobvite"

    def validate_config(self, config: dict) -> None:
        eid, slug = config.get("company_eid"), config.get("slug")
        if eid is None and slug is None:
            raise SourceConfigError(
                "jobvite source requires a 'slug' (the name in jobs.jobvite.com/{slug}) or a "
                "'company_eid'"
            )
        if eid is not None and not (isinstance(eid, str) and _EID.fullmatch(eid)):
            raise SourceConfigError("jobvite 'company_eid' must be 4 to 32 letters and digits")
        if slug is not None and not (isinstance(slug, str) and _SLUG.fullmatch(slug)):
            raise SourceConfigError(
                "jobvite 'slug' must be the name in jobs.jobvite.com/{slug}: letters, digits, "
                "'-' and '_'"
            )

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        batch = PostingBatch()
        label = config.get("slug") or config.get("company_eid")
        try:
            eid = config.get("company_eid")
            if eid is None:
                eid = await self._resolve(config["slug"], client)
            text = await self._get(client, _FEED.format(eid=eid), f"the feed for {label}")
        except _Slow as slow:
            batch.partial_error = slow.message
            return batch
        root = _parse(text, label)
        for job in root.findall("job"):
            posting = _to_posting(job, eid, config)
            if posting is not None:
                batch.append(posting)
        return batch

    async def _resolve(self, slug: str, client: Any) -> str:
        page = await self._get(client, _BOARD.format(slug=slug), f"the board {slug}")
        found = _EID_IN_PAGE.search(page)
        if not found or not _EID.fullmatch(found.group(1)):
            raise AdapterError(f"Jobvite board not found: {slug}")
        return found.group(1)

    async def _get(self, client: Any, url: str, what: str) -> str:
        try:
            r = await client.request(
                "GET", url, rate_per_sec=_RATE, retries=1, check_robots=True,
                follow_redirects=False,
            )
        except httpx.HTTPStatusError as exc:
            r = exc.response
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"Jobvite fetch failed for {what}: {type(exc).__name__}") from exc
        if 300 <= r.status_code < 400:
            raise AdapterError(f"Jobvite board not found: {what} (it redirected away)")
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise _Slow(
                f"Jobvite is rate limiting or unavailable for {what}. Kestrel will try again "
                "next run."
            )
        if r.status_code != 200:
            raise AdapterError(f"Jobvite fetch failed for {what}: status {r.status_code}")
        return r.text


def _parse(text: str, label: Any) -> ElementTree.Element:
    # Clean first, then let the parser itself say whether a DOCTYPE or ENTITY is declared: a text
    # search on the raw reply is bypassed by a control character inside the keyword and refuses a
    # description that merely mentions "<!DOCTYPE html>" inside CDATA.
    cleaned = _BAD_REFS.sub("", _BAD_CHARS.sub("", text))
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _refuse_declaration
    guard.EntityDeclHandler = _refuse_declaration
    try:
        guard.Parse(cleaned, True)
    except _Declared as exc:
        raise AdapterError(f"Jobvite reply for {label} declared a DOCTYPE; refused") from exc
    except expat.ExpatError:
        pass  # not well formed: the real parse below says so in plain words
    try:
        root = ElementTree.fromstring(cleaned)
    except ElementTree.ParseError as exc:
        raise AdapterError(f"Jobvite reply for {label} was not valid XML") from exc
    if root.tag != "result":
        raise AdapterError(f"Jobvite reply for {label} had an unexpected shape")
    return root


def _text(job: ElementTree.Element, tag: str) -> str | None:
    value = job.findtext(tag)
    return value.strip() or None if value else None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _link(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:  # such as an unclosed bracket: use the built link, keep the job
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname.lower() in _JOBVITE_HOSTS:
        return "https" + value[4:]
    return value


def _to_posting(job: ElementTree.Element, eid: str, config: dict) -> RawPosting | None:
    job_id = _text(job, "id")
    if not job_id:
        return None
    safe = quote(job_id, safe="")
    detail = _link(_text(job, "detail-url")) or (
        f"https://app.jobvite.com/CompanyJobs/Job.aspx?c={eid}&j={safe}")
    location = _text(job, "location")
    return RawPosting(
        external_id=job_id,
        url=detail,
        apply_url=_link(_text(job, "apply-url")) or detail,
        title=_text(job, "title"),
        # Never None: the nameless organization is shared by every other nameless posting, and
        # roles of two companies with alike titles would merge into one duplicate group.
        org_name=config.get("company_name") or config.get("slug") or f"Jobvite company {eid}",
        location_raw=location,
        description_html=_text(job, "description"),
        employment_type=_text(job, "jobtype"),
        is_remote=True if location and re.search(r"\bremote\b", location, re.I) else None,
        date_posted=_date(_text(job, "date")),
        raw={"category": _text(job, "category"), "region": _text(job, "region"),
             "requisition_id": _text(job, "requisitionid")},
    )
