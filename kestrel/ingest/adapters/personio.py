"""Personio job board adapter (SPEC.md Appendix A, verified live 2026-10-29).

List:   GET https://{slug}.jobs.personio.de/xml (accounts on the other domain use
        https://{slug}.jobs.personio.com/xml) -> 200 `text/xml`, `<workzag-jobs>` holding one
        `<position>` per role, keyless. An unknown {slug} answers 404 with an HTML page.
Detail: GET https://{slug}.jobs.personio.{tld}/job/{id} -> 200 HTML with one schema.org
        JobPosting in `<script type="application/ld+json">` (description as HTML, and
        `jobLocation.address` with `addressLocality` and `addressCountry` as an ISO code).

GOTCHAS this adapter handles:
  - Each position has `id`, `name`, `office` (a place name, no country), optional `subcompany`,
    `department`, `recruitingCategory`, `employmentType`, `seniority`, `schedule`, `createdAt`,
    optional `salaryInformation` and `jobDescriptions`. The feed names no employer: `company_name`
    or the slug is used.
  - DESCRIPTIONS ARE OFTEN EMPTY in the feed (`<jobDescriptions/>` or blank values). A role with a
    description in the feed costs no extra request; one without gets its job page read, up to
    `max_results` pages a run (default 100). Roles past that cap are still kept, without a body,
    and the run says how many in a partial sentence. A page that fails (not a 429) leaves that
    role without a body; it is never dropped.
  - When the page is read, its place (`addressLocality`, country name) replaces the feed's bare
    office name, so the D-SRC-13 area rule can see the country. Without a page the office name
    is all there is and the country is unknown, which the area rule keeps.
  - Personio rate-limits hard: requests are 2 seconds apart, and a 429 anywhere makes the run
    `partial`: on the list it stops the run, on a job page it stops reading pages and keeps the
    roles already collected.
  - `office` can say "Remote" or "(hybrid)"; only those words set the remote hint.
  - `salaryInformation` is `{min, max, currencyCode, type}` with `type` yearly, monthly, daily or
    hourly. `additionalOffices/office` (documented, not seen on the verified boards) are joined.
  - The feed is parsed with ElementTree after refusing any DOCTYPE or ENTITY declaration.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from xml.etree import ElementTree

import httpx

from kestrel.errors import ROBOTS_SENTENCE
from kestrel.ingest.base import AdapterError, PostingBatch, RawPosting, SourceConfigError
from kestrel.ingest.country_names import country_name

_RATE = 0.5  # requests/second: Personio rate-limits hard
_DEFAULT_MAX_RESULTS = 100
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_TLDS = ("de", "com")
_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_INTERVALS = {"yearly": "year", "monthly": "month", "daily": "day", "hourly": "hour"}
_REMOTE_WORD = re.compile(r"\bremote\b|\bhome\s*office\b", re.IGNORECASE)
_HYBRID_WORD = re.compile(r"\bhybrid\b", re.IGNORECASE)


class PersonioAdapter:
    kind = "personio"

    def validate_config(self, config: dict) -> None:
        slug = config.get("slug")
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise SourceConfigError(
                "personio source requires a 'slug' (the name in {slug}.jobs.personio.de: letters, "
                "digits and '-')"
            )
        tld = config.get("tld")
        if tld is not None and tld not in _TLDS:
            raise SourceConfigError("personio 'tld' must be \"de\" or \"com\"")
        max_results = config.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1
        ):
            raise SourceConfigError("personio 'max_results' must be a positive whole number")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        slug = config["slug"]
        base = f"https://{slug}.jobs.personio.{config.get('tld') or 'de'}"
        max_results = config.get("max_results") or _DEFAULT_MAX_RESULTS
        batch = PostingBatch()
        try:
            text = await client.get_text(f"{base}/xml", rate_per_sec=_RATE, check_robots=True)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                batch.partial_error = _rate_limited(slug)
                return batch
            if code == 404:
                raise AdapterError(
                    f"Personio board not found: {slug} (is the name right, and is the XML feed on?)"
                ) from exc
            raise AdapterError(f"Personio fetch failed for {slug}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"Personio fetch failed for {slug}: {exc}") from exc
        except PermissionError as exc:
            raise AdapterError(ROBOTS_SENTENCE) from exc
        positions = _positions(text, slug)

        pages_read = 0
        without_body = 0
        rate_limited = False
        for position in positions:
            posting = _to_posting(position, config, base)
            if posting is None:
                continue
            if posting.description_html is None:
                if rate_limited or pages_read >= max_results:
                    without_body += 1
                else:
                    pages_read += 1
                    page = await _read_page(base, posting.external_id or "", client)
                    if page is _RATE_LIMITED:
                        rate_limited = True
                        without_body += 1
                    elif isinstance(page, dict):
                        _apply_page(posting, page)
            batch.append(posting)
        if rate_limited:
            batch.partial_error = _rate_limited(slug, kept=without_body)
        elif without_body:
            batch.partial_error = (
                f"Personio gave no description for {without_body} roles at {slug} beyond the "
                f"{max_results} job pages Kestrel reads in one run. They were added without one. "
                "Raise max_results on the source to read more."
            )
        return batch


_RATE_LIMITED = object()


def _rate_limited(slug: str, kept: int = 0) -> str:
    tail = f" {kept} roles were added without a description." if kept else ""
    return f"Personio is rate limiting requests for {slug}.{tail} Kestrel will try again next run."


def _positions(text: str, slug: str) -> list[ElementTree.Element]:
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise AdapterError(f"Personio feed for {slug} was refused (it declares entities)")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise AdapterError(f"Personio feed for {slug} was unreadable") from exc
    if root.tag != "workzag-jobs":
        raise AdapterError(f"Personio feed for {slug} had an unexpected shape")
    return root.findall("position")


async def _read_page(base: str, job_id: str, client: Any) -> Any:
    """The job page's JobPosting dict ({} when the page has none), _RATE_LIMITED on a 429, or None
    on any other failure (the role keeps what the feed gave)."""
    try:
        html = await client.get_text(f"{base}/job/{job_id}", rate_per_sec=_RATE, check_robots=True)
    except httpx.HTTPStatusError as exc:
        return _RATE_LIMITED if exc.response.status_code == 429 else None
    except (httpx.HTTPError, PermissionError):
        return None
    for block in _LD_JSON.findall(html):
        try:
            ld = json.loads(block)
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
            return ld
    return {}


def _apply_page(posting: RawPosting, ld: dict) -> None:
    """Fill the body from a job page, and its place, which has a country the feed lacks."""
    posting.description_html = _strip(ld.get("description"))
    place = ld.get("jobLocation")
    place = place[0] if isinstance(place, list) and place else place
    address = place.get("address") if isinstance(place, dict) else None
    if isinstance(address, dict):
        parts = [_strip(address.get("addressLocality")), country_name(
            _strip(address.get("addressCountry")))]
        label = ", ".join(p for p in parts if p)
        if label:
            posting.location_raw = label
    posting.raw = {**posting.raw, "page": {k: v for k, v in ld.items() if k != "@context"}}


def _to_posting(position: ElementTree.Element, config: dict, base: str) -> RawPosting | None:
    job_id = _text(position, "id")
    if job_id is None:
        return None
    offices = [o for o in [_text(position, "office")] if o]
    for extra in position.findall("additionalOffices/office"):
        label = _strip(extra.text)
        if label and label not in offices:
            offices.append(label)
    office_text = "; ".join(offices)
    remote = bool(_REMOTE_WORD.search(office_text))
    hybrid = bool(_HYBRID_WORD.search(office_text))
    salary = position.find("salaryInformation")
    comp_min = _number(_text(salary, "min")) if salary is not None else None
    comp_max = _number(_text(salary, "max")) if salary is not None else None
    has_comp = comp_min is not None or comp_max is not None
    return RawPosting(
        external_id=job_id,
        url=f"{base}/job/{job_id}",
        title=_text(position, "name"),
        org_name=_strip(config.get("company_name")) or config["slug"],
        location_raw=office_text or None,
        description_html=_body(position),
        employment_type=_text(position, "schedule") or _text(position, "employmentType"),
        is_remote=True if remote else None,
        remote_hint="Remote" if remote else ("Hybrid" if hybrid else None),
        comp_min=comp_min,
        comp_max=comp_max,
        comp_currency=_text(salary, "currencyCode") if has_comp else None,
        comp_interval=_INTERVALS.get((_text(salary, "type") or "").lower()) if has_comp else None,
        date_posted=_text(position, "createdAt"),
        raw=_raw(position),
    )


def _body(position: ElementTree.Element) -> str | None:
    """The feed's description sections as HTML, each under its own heading when it has one; None
    when every section is blank (the common case the job page has to fill)."""
    parts: list[str] = []
    for section in position.findall("jobDescriptions/jobDescription"):
        value = _strip(section.findtext("value"))
        if not value:
            continue
        name = _strip(section.findtext("name"))
        parts.append(f"<h3>{html_lib.escape(name)}</h3>\n{value}" if name else value)
    return "\n".join(parts) or None


def _raw(position: ElementTree.Element) -> dict:
    return {
        child.tag: (child.text or "").strip()
        for child in position
        if child.tag not in ("jobDescriptions", "salaryInformation", "additionalOffices")
        and len(child) == 0
    }


def _text(parent: ElementTree.Element | None, tag: str) -> str | None:
    return _strip(parent.findtext(tag)) if parent is not None else None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _strip(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
